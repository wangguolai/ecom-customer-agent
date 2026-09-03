# -*- coding: utf-8 -*-
"""临时诊断：原始 socket 断开，确认 uvicorn 是否收到 http.disconnect（trace 日志看 connection_lost）"""
import sys
import os
import time
import socket
import subprocess
import threading
import urllib.request

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND_PORT = 8012
_log_lines = []
_lock = threading.Lock()


def _drain(pipe):
    for raw in iter(pipe.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        with _lock:
            _log_lines.append(line)
    pipe.close()


env = os.environ.copy()
env["FAKE_STREAM"] = "1"
env["AUTH_SECRET"] = "dev-only-secret-change-me"
env["PYTHONUNBUFFERED"] = "1"
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "src.backend.main:app",
     "--host", "127.0.0.1", "--port", str(BACKEND_PORT), "--log-level", "trace"],
    cwd=_project_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
)
threading.Thread(target=_drain, args=(proc.stdout,), daemon=True).start()

# 等就绪
ready = False
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{BACKEND_PORT}/online", timeout=1).read()
        ready = True
        break
    except Exception:
        time.sleep(0.3)
print("后端就绪" if ready else "后端未就绪")

# 原始 socket 发请求，读 3 段后直接 close（发 FIN）
body = b'{"message":"hi"}'
req = (b"POST /chat/stream HTTP/1.1\r\n"
       b"Host: 127.0.0.1\r\n"
       b"Content-Type: application/json\r\n"
       b"Content-Length: " + str(len(body)).encode() + b"\r\n"
       b"Connection: close\r\n"
       b"\r\n" + body)

s = socket.create_connection(("127.0.0.1", BACKEND_PORT), timeout=10)
s.sendall(req)
data = b""
count = 0
while count < 3:
    chunk = s.recv(4096)
    if not chunk:
        break
    data += chunk
    count = data.count(b"data:")
print(f"收到 {count} 段，客户端 socket 关闭（发 FIN）")
s.close()

time.sleep(3)
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

print("=" * 60)
print("后端日志（disconnect / connection lost / 客户端已断开 相关）：")
with _lock:
    for l in _log_lines:
        if any(k in l for k in ("connection lost", "disconnect", "客户端已断开", "Connection lost", "Closed", "closed")):
            print("  ", l)
print("-" * 60)
print("后端日志尾部 25 行：")
with _lock:
    for l in _log_lines[-25:]:
        print("  ", l)
