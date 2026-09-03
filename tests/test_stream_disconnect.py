# -*- coding: utf-8 -*-
"""流式断开检测端到端验证（零付费：FAKE_STREAM 假流式，不调 LLM）

验证：客户端断开后，服务端是否立即停止生成，而非把 500 段假流式跑完。

背景（2026-09-02 踩坑，2026-09-03 纠正）：断开检测由 Starlette 1.6 StreamingResponse
内置的 listen_for_disconnect 兜住（ASGI spec 2.3 < 2.4 走可靠分支），代码里 request.is_disconnected()
是死代码（非阻塞 receive 拿不到 http.disconnect）。这里验证「断开 → 服务端停止」这一
端到端行为成立。

流程：
  1. subprocess 起后端（FAKE_STREAM=1 + AUTH_SECRET 注入 + PYTHONUNBUFFERED=1）
  2. 原始 socket 发 POST /chat/stream，读 3 段后 s.close()（发 FIN，确定性的断开）
  3. 轮询后端日志：断言「客户端已断开」出现 且「假流式跑完 500 段」不出现

依赖：本机 MySQL 3306（lifespan DROP 重建 seed 表，属正常「重写真源」）。Redis 可缺（降级）。
"""

import sys
import os
import time
import socket
import subprocess
import threading
import urllib.request

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND_PORT = 8011
BASE = f"http://127.0.0.1:{BACKEND_PORT}"
DISCONNECT_MARK = "客户端已断开，停止流式输出"
FINISH_MARK = "假流式跑完 500 段"

_proc = None
_log_lines = []
_log_lock = threading.Lock()


def _drain_stdout(pipe):
    for raw in iter(pipe.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        with _log_lock:
            _log_lines.append(line)
    pipe.close()


def _start_backend():
    global _proc
    env = os.environ.copy()
    env["FAKE_STREAM"] = "1"
    env["AUTH_SECRET"] = "dev-only-secret-change-me"
    env["PYTHONUNBUFFERED"] = "1"
    _proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.backend.main:app",
         "--host", "127.0.0.1", "--port", str(BACKEND_PORT), "--log-level", "warning"],
        cwd=_project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    threading.Thread(target=_drain_stdout, args=(_proc.stdout,), daemon=True).start()


def _wait_backend(timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _proc.poll() is not None:
            raise RuntimeError("后端启动即退出，日志：\n" + "\n".join(_log_lines[-30:]))
        try:
            urllib.request.urlopen(f"{BASE}/online", timeout=1).read()
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("后端启动超时，日志：\n" + "\n".join(_log_lines[-30:]))


def _logs_contain(mark):
    with _log_lock:
        return any(mark in l for l in _log_lines)


def _request_and_disconnect(n_segments=3):
    """原始 socket 发 POST /chat/stream，读 n 段 SSE 后 close（发 FIN，确定性断开）"""
    body = b'{"message":"hi"}'
    req = (b"POST /chat/stream HTTP/1.1\r\n"
           b"Host: 127.0.0.1\r\n"
           b"Content-Type: application/json\r\n"
           b"Content-Length: " + str(len(body)).encode() + b"\r\n"
           b"\r\n" + body)
    s = socket.create_connection(("127.0.0.1", BACKEND_PORT), timeout=10)
    s.sendall(req)
    data = b""
    count = 0
    while count < n_segments:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
        count = data.count(b"data:")
    s.close()  # 发 FIN → uvicorn connection_lost → http.disconnect
    return count


def main():
    _start_backend()
    _wait_backend()
    print("后端已就绪，原始 socket 连 /chat/stream 读 3 段后断开...")

    received = _request_and_disconnect(3)
    disconnect_time = time.time()
    print(f"客户端已断开（收到 {received} 段）")

    deadline = time.time() + 5.0
    detected = False
    finished = False
    while time.time() < deadline:
        detected = _logs_contain(DISCONNECT_MARK)
        finished = _logs_contain(FINISH_MARK)
        if detected or finished:
            break
        time.sleep(0.05)

    elapsed = time.time() - disconnect_time

    if _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()

    print("-" * 60)
    print(f"收到段数：{received}")
    print(f"断开检测：{'✅ 触发' if detected else '❌ 未触发'}")
    print(f"跑完 500 段：{'❌ 跑完了（失败）' if finished else '✅ 未跑完（已停止）'}")
    print(f"断开 → 检测耗时：{elapsed:.3f}s")
    if detected and not finished:
        print("✅ 结论：客户端断开后服务端立即停止生成（Starlette 内置 listen_for_disconnect 兜住）")
        return 0
    print("❌ 结论：断开检测未生效")
    with _log_lock:
        print("后端日志尾部：")
        for l in _log_lines[-15:]:
            print("  ", l)
    return 1


if __name__ == "__main__":
    sys.exit(main())
