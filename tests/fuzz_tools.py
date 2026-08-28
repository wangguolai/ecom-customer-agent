# -*- coding: utf-8 -*-
"""工具层鲁棒性 fuzz（纯本地、零 LLM 成本）——直接调 src.tools 的工具函数，不走 LLM

对 search_orders / search_logistics / check_stock / refund_order / search_products /
get_return_policy / transfer_to_human 灌同一套脏参数（order_id/product_id/query/problem 都塞脏字符串）。

断言（任一失败 = bug）：
  1. 工具函数必须返回 str，绝不抛异常（工具抛异常会穿透 agent 循环炸掉整个回合）
  2. 返回串不含 Traceback / 异常类名泄露（InvalidURL / UnicodeEncodeError 等）

⚠️ 回归目标：src/tools.py 的 _http_request 已修 InvalidURL 穿透（except httpx.HTTPError 兜底 +
路径参数 quote()）。本脚本验证修好了——空格/换行/控制字符载荷应返回友好 str，不抛异常。
孤立代理对（surrogate）是另一个洞：quote() 和 httpx json 序列化都会抛 UnicodeEncodeError，
且不在 HTTPError/TransportError 兜底范围内——fuzz 要能抓到它（抓到 = 对抗式回归有效）。

注意：search_products / get_return_policy 会触发 embedding 模型加载（CPU/GPU），可能较慢，属正常。

用法：python tests/fuzz_tools.py
依赖：后端已在跑（工具走 HTTP 查后端）；embedding/rerank 模型已缓存（warmup_models 预热）。
"""

import sys
import os
import json
import asyncio
import time

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.tools import TOOL_MAP
from src.infra.warmup import warmup_models

SCRIPT = os.path.basename(__file__)
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results")
REPORT_PATH = os.path.join(RESULTS_DIR, "fuzz_report.json")

# 工具 → (参数名, 是否 RAG)。RAG 工具触发 embedding/rerank 模型加载，较慢。
TOOL_TARGETS = [
    ("search_orders", "order_id", False),
    ("search_logistics", "order_id", False),
    ("check_stock", "product_id", False),
    ("refund_order", "order_id", False),
    ("transfer_to_human", "problem", False),
    ("search_products", "query", True),
    ("get_return_policy", "query", True),
]

# 返回串里一旦出现这些就是异常类名/堆栈泄露（工具层边界不该把实现细节漏给调用方）
_LEAK_NAMES = (
    "Traceback", "InvalidURL", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
    "TimeoutError", "RemoteProtocolError", "HTTPError", "RequestError",
    "KeyError", "TypeError", "ValueError", "UnicodeEncodeError", "UnicodeDecodeError",
    "RuntimeError", "IndexError", "AttributeError", "NameError", "SyntaxError",
    "JSONDecodeError", "NotImplementedError", "OSError", "ConnectionError",
    "IntegrityError", "OperationalError", "PoolError", "AlreadyLocked",
)


def _build_payloads():
    """返回 [(label, payload)]。孤立代理对用 chr() 构造（避免源码里的转义歧义），
    chr(0xD800)+chr(0xdfff) 是非法 UTF-8 码点，正是要灌进去看工具层怎么死的。"""
    return [
        ("超长字符串-10KB", "a" * 10240),
        ("SQL注入-OR", "' OR '1'='1"),
        ("SQL注入-DROP", "1; DROP TABLE orders--"),
        ("SQL注入-UNION", "1' UNION SELECT * FROM orders--"),
        ("路径穿越-unix", "../../etc/passwd"),
        ("路径穿越-win", "..\\..\\windows\\system32\\drivers\\etc\\hosts"),
        ("null字节-嵌入", "abc\x00def"),
        ("null字节-纯", "\x00"),
        ("Unicode-emoji", "🐱🐶🚀😀" * 10),
        ("Unicode-零宽", "a​b‌c"),
        ("Unicode-RTL覆写", "abc‮evil‬def"),
        ("Unicode-孤立代理", chr(0xd800) + chr(0xdfff)),
        ("Unicode-孤立代理-单", chr(0xd800)),
        ("格式化串", "%s%n%x%n%s%n"),
        ("超大数", "9" * 100),
        ("负数", "-12345"),
        ("空串", ""),
        ("纯空格", " " * 50),
        ("纯制表符", "\t" * 10),
        ("XSS-script", "<script>alert(1)</script>"),
        ("XSS-img", "<img src=x onerror=alert(1)>"),
        ("命令注入-semicolon", "; ls -la"),
        ("命令注入-pipe", "| whoami"),
        ("命令注入-backtick", "`id`"),
        ("CRLF注入-字面量", "%0d%0aX-Injected: true"),
        ("CRLF注入-真换行", "\r\n\r\nX-Injected: true"),
        ("控制字符", "\x01\x02\x03\x04"),
        ("换行", "abc\ndef"),
        ("回车", "abc\rdef"),
        ("制表符", "abc\tdef"),
    ]


def _leak_detected(text) -> bool:
    """返回串是否泄露 Traceback / 异常类名。"""
    if not isinstance(text, str):
        return True  # 非字符串本身就是问题（断言 1 也拦，这里双保险）
    if "traceback" in text.lower():
        return True
    return any(name in text for name in _LEAK_NAMES)


def _clear_ratelimit(bucket):
    """清后端限流桶（rl:{bucket}:demo），让 refund_order（写桶 10/10s）等真打到后端校验，
    不被 429 挡掉。Redis 不可用则忽略（429 也返回 str，断言照样过）。"""
    try:
        import redis
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_project_root, ".env"))
        r = redis.Redis(host="127.0.0.1", port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)
        keys = list(r.scan_iter(f"rl:{bucket}:*"))
        if keys:
            r.delete(*keys)
    except Exception:
        pass


def _load_existing_failures(exclude_script=None):
    """读旧报告里别的脚本的失败项（两个 fuzz 脚本共用 fuzz_report.json，合并保留双方结果）"""
    if not os.path.exists(REPORT_PATH):
        return []
    try:
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
        return [x for x in old.get("failures", []) if x.get("script") != exclude_script]
    except Exception:
        return []


def _write_report(failures, passed, failed):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    merged = _load_existing_failures(exclude_script=SCRIPT) + failures
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {"passed": passed, "failed": len(merged)},
        "failures": merged,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=True, indent=2, default=str)


async def main():
    print("=" * 74)
    print("工具层鲁棒性 fuzz — 直接调 TOOL_MAP，不走 LLM")
    print("=" * 74)

    payloads = _build_payloads()
    passed = 0
    failed = 0
    failures = []

    _clear_ratelimit("read")
    _clear_ratelimit("write")

    total = len(payloads) * len(TOOL_TARGETS)
    i = 0

    for tool_name, arg_name, is_rag in TOOL_TARGETS:
        for label, payload in payloads:
            # refund_order 走写桶（10/10s）、其余 HTTP 工具走读桶（100/10s），
            # 30 个载荷连发会撞 429；每请求前清桶，保证每个载荷真打到后端校验逻辑
            if not is_rag:
                _clear_ratelimit("write" if tool_name == "refund_order" else "read")
            i += 1
            print(f"📍 [{i}/{total}] {tool_name} — 载荷[{label}]")
            kwargs = {arg_name: payload}
            exc_name = None
            try:
                result = await TOOL_MAP[tool_name](**kwargs)
            except Exception as e:
                exc_name = type(e).__name__
                result = ""

            if exc_name is not None:
                failed += 1
                failures.append({
                    "script": SCRIPT, "payload_label": label, "payload": payload,
                    "target": tool_name, "exception": exc_name, "return": "",
                })
                print(f"   ❌ 抛异常 {exc_name}")
                continue

            if not isinstance(result, str):
                failed += 1
                failures.append({
                    "script": SCRIPT, "payload_label": label, "payload": payload,
                    "target": tool_name, "exception": None, "return": f"<非str: {type(result).__name__}>",
                })
                print(f"   ❌ 返回非 str：{type(result).__name__}")
                continue

            if _leak_detected(result):
                failed += 1
                failures.append({
                    "script": SCRIPT, "payload_label": label, "payload": payload,
                    "target": tool_name, "exception": None, "return": result[:200],
                })
                print(f"   ❌ 返回串含异常类名/堆栈泄露：{result[:80]!r}")
                continue

            passed += 1
            # RAG 结果可能很长，截断展示
            preview = result if len(result) <= 60 else result[:60] + "..."
            print(f"   ✅ {preview}")

    print("=" * 74)
    print(f"结果：通过 {passed} / 失败 {failed}")
    _write_report(failures, passed, failed)
    print(f"报告：{REPORT_PATH}")
    print("=" * 74)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    # 主线程预热 embedding + rerank，避免 to_thread 里首次加载 CUDA 死锁（Windows 坑）
    try:
        warmup_models()
        print("📍 [预热] embedding + rerank 模型已预热")
    except Exception as e:
        print(f"⚠️ 模型预热失败（{type(e).__name__}），RAG 工具首次调用可能慢或失败：{e}")
    asyncio.run(main())
