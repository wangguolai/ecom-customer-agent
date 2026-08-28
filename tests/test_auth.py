# -*- coding: utf-8 -*-
"""JWT 鉴权对抗测试（模块 8）—— 零 API 成本

分两段：
  单元段：直接测 src/backend/auth.py 的签发/校验/密码哈希，不需要后端在跑
  集成段：打真实后端接口，验证 review/execute 的鉴权真的挂上了；后端没起就 SKIP

对抗式，不是 happy path 自证——每条都是「攻击者会怎么绕」：
  伪造算法（alg=none）、改签名、换密钥、过期、越权 role、篡改 payload、畸形 token。

用法：python tests/test_auth.py
"""

import sys
import os
import json
import time
import base64

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 单元段自给密钥（不依赖外部环境），且刻意不用 DEV_SECRET，避免和集成段串味
os.environ.setdefault("AUTH_SECRET", "unit-test-secret-啊")

import requests
from src.backend import auth

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name} {detail}")


def _forge(header: dict, payload: dict, sig: str = "") -> str:
    """手工拼一个 token（攻击者视角：header/payload 随便写，签名随便给）"""
    h = auth._b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = auth._b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{h}.{p}.{sig}"


# ═══════════════════════════════════════════════════════════════
# 单元段
# ═══════════════════════════════════════════════════════════════

def test_unit():
    print("=" * 70)
    print("单元段：auth.py 签发 / 校验 / 密码哈希")
    print("-" * 70)

    # 1. 正常签发 + 校验
    tok = auth.create_token("admin", "admin")
    try:
        payload = auth.decode_token(tok)
        check("正常 token 校验通过", payload["sub"] == "admin" and payload["role"] == "admin")
    except auth.AuthError as e:
        check("正常 token 校验通过", False, f"意外 AuthError: {e}")

    # 2. 🔴 alg=none 攻击：header 改 none + 空签名
    forged = _forge({"alg": "none", "typ": "JWT"}, {"sub": "admin", "role": "admin", "exp": int(time.time()) + 999})
    check("alg=none 伪造被拒", _rejects(forged))

    # 3. alg 降级到 HS1 之类的未知算法
    check("未知 alg 被拒", _rejects(_forge({"alg": "HS1"}, {"role": "admin", "exp": int(time.time()) + 999})))

    # 4. header 不是 dict（["HS256"] 之类）
    h = auth._b64url_encode(json.dumps(["HS256"]).encode())
    p = auth._b64url_encode(json.dumps({"role": "admin", "exp": int(time.time()) + 999}).encode())
    check("header 非 dict 被拒", _rejects(f"{h}.{p}.xxx"))

    # 5. 篡改 payload（把 role 改成 admin）但不改签名
    good = auth.create_token("u1", "user")
    h2, p2, s2 = good.split(".")
    tampered_payload = auth._b64url_encode(json.dumps({"sub": "u1", "role": "admin", "exp": int(time.time()) + 999}).encode())
    check("篡改 payload 被签名挡住", _rejects(f"{h2}.{tampered_payload}.{s2}"))

    # 6. 换密钥签的 token（攻击者用自己的密钥签）
    real_secret = os.environ["AUTH_SECRET"]
    os.environ["AUTH_SECRET"] = "attacker-secret"
    evil = auth.create_token("admin", "admin")
    os.environ["AUTH_SECRET"] = real_secret
    check("错误密钥签发的 token 被拒", _rejects(evil))

    # 7. 过期 token
    check("过期 token 被拒", _rejects(auth.create_token("admin", "admin", ttl=-10)))

    # 8. exp 缺失 / 非整数（攻击者删掉 exp 想要永久 token）
    #    注意：要用真密钥重新签，否则会被签名挡住而不是被 exp 校验挡住——那就测不到 exp 这条
    h3 = auth._b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    p3 = auth._b64url_encode(json.dumps({"sub": "a", "role": "admin"}, separators=(",", ":"), sort_keys=True).encode())
    check("exp 缺失被拒（签名合法）", _rejects(f"{h3}.{p3}.{auth._sign(f'{h3}.{p3}')}"))

    # 9. 畸形 token：段数不对 / 空串 / 非字符串 / 非法 base64
    for bad, label in [("", "空串"), ("a.b", "两段"), ("a.b.c.d", "四段"), ("!!!.???.###", "非法 base64")]:
        check(f"畸形 token 被拒（{label}）", _rejects(bad))
    check("非字符串 token 被拒", _rejects(None))
    check("非字符串 token 被拒（int）", _rejects(12345))

    # 10. 密码哈希
    stored = auth.hash_password("正确密码 P@ss")
    check("密码哈希不含明文", "正确密码" not in stored and "P@ss" not in stored)
    check("正确密码校验通过", auth.verify_password("正确密码 P@ss", stored))
    check("错误密码校验失败", not auth.verify_password("错误密码", stored))
    check("同密码两次哈希不同（salt 生效）", auth.hash_password("abc") != auth.hash_password("abc"))
    for bad in ["", "$$$", "md5$1$aa$bb", None, "pbkdf2_sha256$x$y$z"]:
        check(f"脏 stored 不抛异常（{bad!r}）", auth.verify_password("abc", bad) is False)
    # 孤立代理（U+D800 非法码点）进密码/用户名 —— encode("utf-8") 会抛 UnicodeEncodeError，
    # 这是 fuzz 抓到的真 500：外部输入不该打崩鉴权。errors="replace" 后应返回 False 而不是抛异常。
    check("孤立代理密码不抛异常", auth.verify_password(chr(0xd800), stored) is False)
    check("孤立代理用户名不抛异常", auth._ct_equal(chr(0xd800), "admin") is False)
    # 非字符串 username（int/bool/dict）—— code-reviewer 审出 authenticate(123,...) 会 500
    check("非字符串用户名不抛异常", auth._ct_equal(123, "admin") is False)
    check("非字符串用户名(dict)不抛异常", auth._ct_equal({"a": 1}, "admin") is False)

    # 11. 密钥未配置 → fail-closed（不能悄悄用默认密钥）
    saved = os.environ.pop("AUTH_SECRET")
    try:
        auth.create_token("a", "admin")
        check("AUTH_SECRET 缺失时拒绝签发", False, "居然签发成功了")
    except RuntimeError:
        check("AUTH_SECRET 缺失时拒绝签发（fail-closed）", True)
    finally:
        os.environ["AUTH_SECRET"] = saved


def _rejects(token) -> bool:
    """decode_token 是否拒绝该 token（抛 AuthError 才算拒绝；抛别的异常算 bug）"""
    try:
        auth.decode_token(token)
        return False
    except auth.AuthError:
        return True
    except Exception as e:  # 非预期异常也是 bug（会变成 500 而不是 401）
        print(f"     ⚠️ 抛了非 AuthError 异常：{type(e).__name__}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 集成段（需要后端在跑）
# ═══════════════════════════════════════════════════════════════

def _backend_up() -> bool:
    try:
        return requests.get(f"{BACKEND_URL}/orders/20240818001", timeout=3.0).ok
    except requests.RequestException:
        return False


def _clear_auth_ratelimit():
    """清 auth 限流桶（只清 rl:auth:*，不动 read/write 桶）。

    为什么测试要清：auth 桶 5 次/60s 是防爆破的设计值，本测试光登录就 6+ 次，
    不清会撞到 429（这恰恰证明限流生效了，但测试要测的是「鉴权逻辑」，不是「限流」）。
    只清 auth 前缀，遵守「不用 FLUSHDB 连限流计数一起抹掉」的原则。
    """
    try:
        import redis
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_project_root, ".env"))
        r = redis.Redis(host="127.0.0.1", port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)
        keys = list(r.scan_iter("rl:auth:*"))
        if keys:
            r.delete(*keys)
    except Exception:
        pass  # Redis 不可用就跳过，429 会作为测试失败暴露出来


def test_integration():
    print()
    print("=" * 70)
    print("集成段：真实后端接口鉴权")
    print("-" * 70)
    if not _backend_up():
        print("  ⏭️ 后端未启动，SKIP")
        return
    _clear_auth_ratelimit()

    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pw = os.environ.get("ADMIN_PASSWORD", "admin123")

    # 先造一个真实工单（走 /refund），拿 ticket_id 用于审批测试
    r = requests.post(f"{BACKEND_URL}/refund", json={"order_id": "20240818001"}, timeout=5.0)
    check("/refund 无需鉴权（agent 代客申请，低权操作）", r.status_code == 200, f"got {r.status_code} {r.text[:80]}")
    time.sleep(1.0)  # 等 MQ 消费者异步落库

    ticket_id = _find_ticket("20240818001")
    if not ticket_id:
        print("  ⚠️ 未取到工单号（MQ 消费可能延迟），审批类断言降级为「只验鉴权码」")
        ticket_id = "RF00000000"

    # 1. 无 token 调审批 → 401
    r = requests.post(f"{BACKEND_URL}/refund/{ticket_id}/review", json={"action": "approve"}, timeout=5.0)
    check("审批无 token → 401", r.status_code == 401, f"got {r.status_code}")

    # 2. 乱码 token → 401
    # 注意用 ASCII 乱码：HTTP header 是 latin-1 编码，中文 token 会在 requests 层就 UnicodeEncodeError，
    # 根本到不了后端——那测的是「client 拒绝发」，不是「后端拒绝收」。要打到后端就得用能进 header 的乱码。
    r = requests.post(f"{BACKEND_URL}/refund/{ticket_id}/review", json={"action": "approve"},
                      headers={"Authorization": "Bearer not.a.valid.jwt"}, timeout=5.0)
    check("审批乱码 token → 401", r.status_code == 401, f"got {r.status_code}")

    # 3. alg=none 伪造 → 401
    forged = _forge({"alg": "none", "typ": "JWT"}, {"sub": "admin", "role": "admin", "exp": int(time.time()) + 999})
    r = requests.post(f"{BACKEND_URL}/refund/{ticket_id}/review", json={"action": "approve"},
                      headers={"Authorization": f"Bearer {forged}"}, timeout=5.0)
    check("审批 alg=none 伪造 → 401", r.status_code == 401, f"got {r.status_code}")

    # 4. 执行接口同样要鉴权
    r = requests.post(f"{BACKEND_URL}/refund/{ticket_id}/execute", timeout=5.0)
    check("执行无 token → 401", r.status_code == 401, f"got {r.status_code}")

    # 5. 登录：错密码 → 401，且不泄露用户是否存在
    r = requests.post(f"{BACKEND_URL}/auth/token", json={"username": admin_user, "password": "错的"}, timeout=5.0)
    check("错密码登录 → 401", r.status_code == 401, f"got {r.status_code}")
    body = r.text
    check("错密码响应不泄露用户存在性", "不存在" not in body and "无此用户" not in body, body[:80])
    r2 = requests.post(f"{BACKEND_URL}/auth/token", json={"username": "根本没有这个人", "password": "错的"}, timeout=5.0)
    check("不存在用户与错密码返回同样的错误", r2.status_code == r.status_code and r2.text == body)

    # 6. 正确登录 → 拿 token
    r = requests.post(f"{BACKEND_URL}/auth/token", json={"username": admin_user, "password": admin_pw}, timeout=5.0)
    if r.status_code != 200:
        check("正确密码登录成功", False, f"got {r.status_code} {r.text[:100]}（检查 ADMIN_USER/ADMIN_PASSWORD 是否与后端一致）")
        return
    token = r.json().get("access_token", "")
    check("正确密码登录成功且返回 token", bool(token))

    # 7. 拿真 token 审批 → 应通过（200）或状态冲突（409），但绝不能是 401/403
    r = requests.post(f"{BACKEND_URL}/refund/{ticket_id}/review", json={"action": "approve"},
                      headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
    check("合法 admin token 通过鉴权（非 401/403）", r.status_code not in (401, 403), f"got {r.status_code} {r.text[:80]}")

    # 8. 垂直越权：role=user 的合法签名 token 调审批 → 403（不是 401，身份是真的，权限不够）
    #    需要和后端同一个 AUTH_SECRET 才签得出合法 token；不一致则跳过
    server_secret = os.environ.get("BACKEND_AUTH_SECRET") or os.environ.get("AUTH_SECRET_FOR_TEST")
    if server_secret:
        saved = os.environ.get("AUTH_SECRET")
        os.environ["AUTH_SECRET"] = server_secret
        user_token = auth.create_token("u1", "user")
        os.environ["AUTH_SECRET"] = saved
        r = requests.post(f"{BACKEND_URL}/refund/{ticket_id}/review", json={"action": "approve"},
                          headers={"Authorization": f"Bearer {user_token}"}, timeout=5.0)
        check("role=user 调审批 → 403（垂直越权拦截）", r.status_code == 403, f"got {r.status_code}")
    else:
        print("  ⏭️ 未提供 BACKEND_AUTH_SECRET，跳过 role=user 越权断言")


def _find_ticket(order_id: str):
    """从容器 MySQL（3307）查工单号。

    ⚠️ 必须显式打 3307：宿主机 .env 的 MYSQL_PORT=3306 指向的是本机 mysqld，
    那是另一个同名 ecommerce 库、表结构也几乎一样——连错了照样查得通、断言照样"通过"，
    是最危险的一类静默错误。
    """
    try:
        import pymysql
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_project_root, ".env"))
        conn = pymysql.connect(
            host="127.0.0.1", port=int(os.environ.get("VERIFY_MYSQL_PORT", "3307")),
            user=os.environ.get("MYSQL_USER", "root"), password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DB", "ecommerce"), connect_timeout=3,
        )
        try:
            cur = conn.cursor()
            cur.execute("SELECT ticket_id FROM refunds WHERE order_id=%s", (order_id,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        print(f"     ⚠️ 查工单失败：{type(e).__name__}: {str(e)[:60]}")
        return None


if __name__ == "__main__":
    test_unit()
    test_integration()
    print()
    print("=" * 70)
    print(f"结果：通过 {_passed} / 失败 {_failed}")
    print("=" * 70)
    sys.exit(1 if _failed else 0)
