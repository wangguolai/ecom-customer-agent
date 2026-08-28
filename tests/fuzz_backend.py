# -*- coding: utf-8 -*-
"""后端 HTTP 接口鲁棒性 fuzz（纯本地、零 LLM/embedding 成本）

对已启动的 FastAPI 后端（http://localhost:8000）灌脏载荷，验证 4 条断言（任一失败 = bug）：
  1. 不返回 5xx（4xx 是正则校验正确拦截）
  2. 不超时（每请求 5s）
  3. 响应体不原样回显载荷（防反射型 XSS）
  4. 跑完后 orders 表行数不变、表仍在（证明 SQL 注入没打穿）

靶点：GET /orders/{id}、GET /logistics/{id}、GET /products/{id}、
      POST /refund、POST /auth/token（body 也塞脏数据）。

依赖：后端已在跑（http://localhost:8000）；容器 MySQL 映射宿主机 3307
（用 VERIFY_MYSQL_PORT 覆盖；密码从 .env 的 MYSQL_PASSWORD 读）。

用法：python tests/fuzz_backend.py
"""

import sys
import os
import json
import time
from urllib.parse import quote

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
import pymysql
from dotenv import load_dotenv

load_dotenv(os.path.join(_project_root, ".env"))

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
REQUEST_TIMEOUT = 5.0  # 每个请求 5s 超时，超时即失败
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_results")
REPORT_PATH = os.path.join(RESULTS_DIR, "fuzz_report.json")

# MySQL 必须连容器实例（宿主机 3307），不能连宿主机 3306——那是另一个同名 ecommerce 库，
# 连错了照样查得通、断言照样「通过」，是最危险的一类静默错误（对齐 test_auth.py 的 _find_ticket 规范）。
MYSQL_PORT = int(os.environ.get("VERIFY_MYSQL_PORT", "3307"))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB = os.environ.get("MYSQL_DB", "ecommerce")

SCRIPT = os.path.basename(__file__)


# ═══════════════════════════════════════════════════════════════
# 载荷集（每类若干变体）
# ═══════════════════════════════════════════════════════════════

def _build_payloads():
    """返回 [(label, payload)] 列表。孤立代理对 \\ud800 无法编码成合法 UTF-8，
    正是要灌进去看服务端/工具层怎么死的（这里是 HTTP 层，用 surrogatepass 百分号编码发）。
    """
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


def _url_component(payload: str) -> str:
    """把载荷安全编码成 URL path 片段。

    用 surrogatepass 编码后再 quote：正常字符串走 UTF-8 百分号编码；孤立代理对
    （无法编码成合法 UTF-8）保留原始字节百分号编码，测试服务端怎么处理非法字节。
    """
    raw = payload.encode("utf-8", "surrogatepass")
    return quote(raw, safe="")


# ═══════════════════════════════════════════════════════════════
# 基础设施：MySQL 快照 / 清限流 / 清故障注入
# ═══════════════════════════════════════════════════════════════

def _count_orders():
    """orders 表行数（跑前/跑后快照，证明 SQL 注入没打穿）"""
    conn = pymysql.connect(
        host="127.0.0.1", port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DB, connect_timeout=3,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders")
        return cur.fetchone()[0]
    finally:
        conn.close()


def _orders_table_exists():
    """orders 表是否还在（被 DROP 就是灾难）"""
    conn = pymysql.connect(
        host="127.0.0.1", port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
        database=MYSQL_DB, connect_timeout=3,
    )
    try:
        cur = conn.cursor()
        cur.execute("SHOW TABLES LIKE 'orders'")
        return cur.fetchone() is not None
    finally:
        conn.close()


def _clear_ratelimit(bucket):
    """清后端限流桶（rl:{bucket}:demo），让 fuzz 载荷真打到校验逻辑，不被 429 挡掉。

    429 本身是 4xx（断言照样过），但清掉才能测到「每个载荷都过了校验层」，
    对齐 test_auth.py 的清 auth 桶规范。Redis 不可用则忽略（429 会作为 4xx 正常处理）。
    """
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)
        keys = list(r.scan_iter(f"rl:{bucket}:*"))
        if keys:
            r.delete(*keys)
    except Exception:
        pass


def _clear_faults():
    """清故障注入残留：上轮评测可能留了 timeout/dirty 注入，不清会污染本轮断言。"""
    try:
        requests.post(f"{BACKEND_URL}/debug/fault/clear", json={}, timeout=2.0)
    except requests.RequestException:
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


def _write_report(failures, passed, failed, count_before, count_after):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    merged = _load_existing_failures(exclude_script=SCRIPT) + failures
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {"passed": passed, "failed": len(merged)},
        "orders_before": count_before,
        "orders_after": count_after,
        "failures": merged,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=True, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# 单请求：发送 + 断言
# ═══════════════════════════════════════════════════════════════

def _send(method, url, json_body):
    """发请求。返回 (ok, status, exception, body_text, content_type)。
    ok=False 且 exception 非 None = 客户端异常（超时/连接失败/URL 非法）"""
    try:
        resp = requests.request(method, url, json=json_body, timeout=REQUEST_TIMEOUT)
        return True, resp.status_code, None, resp.text, resp.headers.get("content-type", "")
    except requests.exceptions.Timeout:
        return False, None, "Timeout", "", ""
    except requests.exceptions.RequestException as e:
        return False, None, type(e).__name__, str(e)[:200], ""


def _assert_ok(send_ok, status, exc, body, payload, label, target, method, check_echo=True, content_type=""):
    """对单次请求跑断言，返回问题列表（空 = 通过）。

    - send_ok=False：请求本身异常（超时/连接失败/URL 非法）→ 失败
    - status>=500：后端 5xx → 失败（4xx 是正则校验正确拦截，通过）
    - check_echo 且 content_type 是 text/html 且 payload 原样出现在响应体 → 失败（反射型 XSS）

    回显判定只对 HTML 生效：反射型 XSS 的前提是「响应被浏览器当 HTML 解析」。
    FastAPI 的 400 错误是 `{"detail":"订单号格式非法：<载荷>"}`，Content-Type 是
    application/json —— 浏览器不会执行 JSON，且 payload 会被 JSON 正确转义。
    这种「错误信息里回显坏输入」是**正确行为**（告诉客户端哪里错），不是漏洞。
    把 JSON 400 的回显当失败，是拿「防御式设计」当「看到自己的输入就报警」的过度反应。
    """
    issues = []
    if not send_ok:
        issues.append(f"请求异常({exc})")
        return issues
    if status >= 500:
        issues.append(f"5xx({status})")
    if check_echo and payload and payload in body and "text/html" in content_type.lower():
        issues.append("回显载荷(HTML)")
    return issues


def _record(failures, payload_label, payload, target, method, issues, status, exc, body):
    failures.append({
        "script": SCRIPT,
        "payload_label": payload_label,
        "payload": payload,
        "target": target,
        "method": method,
        "issues": issues,
        "status": status,
        "exception": exc,
        "return": body[:200],
    })


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 74)
    print(f"后端 HTTP 接口鲁棒性 fuzz — {BACKEND_URL}")
    print("=" * 74)

    _clear_faults()
    _clear_ratelimit("read")
    _clear_ratelimit("write")
    _clear_ratelimit("auth")

    count_before = _count_orders()
    print(f"📍 [前置] orders 表行数快照 = {count_before}")

    payloads = _build_payloads()
    passed = 0
    failed = 0
    failures = []

    get_targets = [
        ("orders", "/orders/{payload}"),
        ("logistics", "/logistics/{payload}"),
        ("products", "/products/{payload}"),
    ]
    # 额外 body 形态（非对象/缺字段/多余字段），验证 body 校验不 5xx
    refund_body_shapes = [
        ("refund-body-非对象", "[]"),
        ("refund-body-null", "null"),
        ("refund-body-字符串", '"str"'),
        ("refund-body-数字", "123"),
        ("refund-body-订单号非字符串", '{"order_id": 12345}'),
        ("refund-body-注入amount", '{"order_id": "99999999", "amount": -999}'),
        ("refund-body-多余字段", '{"order_id": "99999999", "x": "<script>"}'),
    ]
    auth_body_shapes = [
        ("auth-body-非对象", "[]"),
        ("auth-body-null", "null"),
        ("auth-body-字符串", '"str"'),
        ("auth-body-数字", "123"),
        ("auth-body-缺password", '{"username": "admin"}'),
    ]

    total = (
        len(payloads) * len(get_targets)
        + len(payloads) * 2  # refund + auth 每载荷一个
        + len(refund_body_shapes) + len(auth_body_shapes)
    )
    i = 0

    # ── GET 靶点 ──
    for target, template in get_targets:
        _clear_ratelimit("read")
        for label, payload in payloads:
            i += 1
            url = BACKEND_URL + template.replace("{payload}", _url_component(payload))
            print(f"📍 [{i}/{total}] GET /{target}/{{id}} — 载荷[{label}]")
            send_ok, status, exc, body, ctype = _send("GET", url, None)
            issues = _assert_ok(send_ok, status, exc, body, payload, label, target, "GET", content_type=ctype)
            if issues:
                failed += 1
                _record(failures, label, payload, target, "GET", issues, status, exc, body)
                print(f"   ❌ {issues} (status={status} exc={exc})")
            else:
                passed += 1
                print(f"   ✅ status={status}")

    # ── POST /refund（每载荷一个 body，order_id 塞脏数据）──
    # 写桶 10/10s，30 个载荷连发必撞 429；每请求前清桶，保证每个载荷真打到校验逻辑
    for label, payload in payloads:
        _clear_ratelimit("write")
        i += 1
        print(f"📍 [{i}/{total}] POST /refund — 载荷[{label}]")
        send_ok, status, exc, body, ctype = _send("POST", f"{BACKEND_URL}/refund", {"order_id": payload})
        issues = _assert_ok(send_ok, status, exc, body, payload, label, "refund", "POST", content_type=ctype)
        if issues:
            failed += 1
            _record(failures, label, payload, "refund", "POST", issues, status, exc, body)
            print(f"   ❌ {issues} (status={status} exc={exc})")
        else:
            passed += 1
            print(f"   ✅ status={status}")

    # ── POST /refund（额外 body 形态）──
    _clear_ratelimit("write")
    for label, raw in refund_body_shapes:
        i += 1
        print(f"📍 [{i}/{total}] POST /refund — body形态[{label}]")
        send_ok, status, exc, body, ctype = _send_raw("POST", f"{BACKEND_URL}/refund", raw)
        # body 形态断言不查回显（它测的是「非对象 body 不 5xx」，不是反射）
        issues = _assert_ok(send_ok, status, exc, body, None, label, "refund", "POST", check_echo=False)
        if issues:
            failed += 1
            _record(failures, label, raw, "refund", "POST", issues, status, exc, body)
            print(f"   ❌ {issues} (status={status} exc={exc})")
        else:
            passed += 1
            print(f"   ✅ status={status}")

    # ── POST /auth/token（每载荷一个 body，username/password 塞脏数据）──
    # auth 桶 5/60s，30 个载荷连发必撞 429；每请求前清桶，保证每个载荷真打到校验逻辑
    for label, payload in payloads:
        _clear_ratelimit("auth")
        i += 1
        print(f"📍 [{i}/{total}] POST /auth/token — 载荷[{label}]")
        send_ok, status, exc, body, ctype = _send("POST", f"{BACKEND_URL}/auth/token", {"username": payload, "password": payload})
        issues = _assert_ok(send_ok, status, exc, body, payload, label, "auth/token", "POST", content_type=ctype)
        if issues:
            failed += 1
            _record(failures, label, payload, "auth/token", "POST", issues, status, exc, body)
            print(f"   ❌ {issues} (status={status} exc={exc})")
        else:
            passed += 1
            print(f"   ✅ status={status}")

    # ── POST /auth/token（额外 body 形态）──
    for label, raw in auth_body_shapes:
        _clear_ratelimit("auth")
        i += 1
        print(f"📍 [{i}/{total}] POST /auth/token — body形态[{label}]")
        send_ok, status, exc, body, ctype = _send_raw("POST", f"{BACKEND_URL}/auth/token", raw)
        issues = _assert_ok(send_ok, status, exc, body, None, label, "auth/token", "POST", check_echo=False)
        if issues:
            failed += 1
            _record(failures, label, raw, "auth/token", "POST", issues, status, exc, body)
            print(f"   ❌ {issues} (status={status} exc={exc})")
        else:
            passed += 1
            print(f"   ✅ status={status}")

    # ── 收尾：orders 表完整性断言 ──
    print("📍 [收尾] 复核 orders 表完整性...")
    count_after = _count_orders()
    table_ok = _orders_table_exists()
    sql_ok = table_ok and (count_after == count_before)
    if not table_ok:
        failed += 1
        failures.append({
            "script": SCRIPT, "payload_label": None, "payload": None,
            "target": "DB", "method": None, "issues": ["orders 表不存在（被 DROP）"],
            "status": None, "exception": None, "return": None,
        })
        print("   ❌ orders 表不存在（被 DROP）")
    elif count_after != count_before:
        failed += 1
        failures.append({
            "script": SCRIPT, "payload_label": None, "payload": None,
            "target": "DB", "method": None,
            "issues": [f"orders 表行数变化：{count_before} → {count_after}"],
            "status": None, "exception": None, "return": None,
        })
        print(f"   ❌ orders 表行数变化：{count_before} → {count_after}")
    else:
        passed += 1
        print(f"   ✅ orders 表行数不变（{count_before}）、表仍在")

    _clear_faults()

    # ── 落盘 + 汇总 ──
    _write_report(failures, passed, failed, count_before, count_after)

    print("=" * 74)
    print(f"结果：通过 {passed} / 失败 {failed}")
    print(f"报告：{REPORT_PATH}")
    print("=" * 74)
    sys.exit(1 if failed else 0)


def _send_raw(method, url, raw_body):
    """发送原始 JSON 字符串 body（用于非对象 body 形态，如 [] / null / 字符串）。
    返回结构和 _send 一致（5 元组），末位 content_type 供回显断言（body 形态类 check_echo=False 用不到）。"""
    try:
        resp = requests.request(method, url, data=raw_body, timeout=REQUEST_TIMEOUT,
                                headers={"Content-Type": "application/json"})
        return True, resp.status_code, None, resp.text, resp.headers.get("content-type", "")
    except requests.exceptions.Timeout:
        return False, None, "Timeout", "", ""
    except requests.exceptions.RequestException as e:
        return False, None, type(e).__name__, str(e)[:200], ""


if __name__ == "__main__":
    main()
