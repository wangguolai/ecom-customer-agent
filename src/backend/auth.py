# -*- coding: utf-8 -*-
"""JWT 鉴权 + 密码哈希 —— 手写 HS256（模块 8 网络安全）

为什么手写不装 PyJWT：
  1. 零新依赖（后端镜像最小化，requirements-backend.txt 的既定规范）
  2. JWT 三段结构（header.payload.signature）+ HMAC 签名 + base64url 是必要点，手写过才讲得透
  3. 只做 HS256 签发/校验，不做 alg 协商 / JWK / RS256，攻击面可控

手写的代价 —— 下面三条是 PyJWT 帮你挡、自己写就必须显式挡的：
  ① alg=none 攻击：攻击者把 header 改成 {"alg":"none"} 并删掉签名段。
     防法：硬校验 header["alg"] == "HS256"，绝不信任 token 自称的算法。
     （历史上多个 JWT 库栽在「按 token 说的算法去验」——等于让攻击者选锁。）
  ② 时序攻击：逐字节比较签名，靠响应时间差可以一个字节一个字节爆破出正确签名。
     防法：hmac.compare_digest 常数时间比较，不用 ==。
  ③ 过期不校验：签发了就永久有效。防法：显式校验 exp。

密码存储：pbkdf2_hmac(sha256, pw, salt, 200000)，标准库实现，不引 bcrypt/argon2。
  为什么不能裸 sha256 → 彩虹表 + GPU 每秒百亿次
  为什么要 salt      → 相同密码产生不同哈希，彩虹表失效，防「破一个通杀全网」
  为什么要慢         → 正常登录多花 ~100ms 无感，爆破成本被放大 20 万倍
"""

import sys
import os
import json
import time
import hmac
import base64
import hashlib
import secrets

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import Header, HTTPException

ALG = "HS256"
TOKEN_TTL = 3600  # 秒。短 TTL 是 JWT「无法主动失效」的主要缓解手段之一
PBKDF2_ROUNDS = 200_000
# 编排注入的 demo 密钥。代码不给默认值（fail-closed），但要能认出这个值并告警——
# 「有默认密钥」等于「没有密钥」，签名可被任何拿到源码的人伪造。
DEV_SECRET = "dev-only-secret-change-me"


class AuthError(Exception):
    """token 校验失败（签名/算法/过期/格式）。统一转 401，不向外泄露具体哪一步失败。"""


# ═══════════════════════════════════════════════════════════════
# base64url（JWT 规范：去掉 padding 的 base64，且 +/ 换成 -_）
# ═══════════════════════════════════════════════════════════════

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # 补回 padding：base64 长度必须是 4 的倍数
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ═══════════════════════════════════════════════════════════════
# 密钥
# ═══════════════════════════════════════════════════════════════

def _secret() -> bytes:
    """读 AUTH_SECRET。缺失直接抛（fail-closed）——绝不回落到默认密钥。

    懒读不在 import 时抛：让不碰鉴权的脚本（fuzz/压测）能正常 import 后端模块。
    启动期的 fail-fast 由 check_auth_config() 在 lifespan 里做。
    """
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        raise RuntimeError("AUTH_SECRET 未配置，拒绝启动鉴权（fail-closed）。请在环境变量注入。")
    return secret.encode("utf-8")


def check_auth_config():
    """启动期自检（lifespan 调用）：密钥必须存在；是 demo 值则刺眼告警。"""
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        raise RuntimeError("AUTH_SECRET 未配置，后端拒绝启动（fail-closed）。")
    if secret == DEV_SECRET:
        print("🔴 警告：正在使用 demo 默认 AUTH_SECRET，任何拿到源码的人都能伪造 token。生产必须替换！")
    if not _admin_credential():
        print("⚠️ 未配置 ADMIN_USER/ADMIN_PASSWORD，/auth/token 将无法签发任何 token（审批接口不可用）")


# ═══════════════════════════════════════════════════════════════
# 密码哈希
# ═══════════════════════════════════════════════════════════════

def hash_password(password: str, salt: bytes = None) -> str:
    """返回 "pbkdf2_sha256$轮数$salt_hex$hash_hex"（自描述格式，换算法可平滑迁移）"""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比对。格式非法一律返回 False，不抛异常（登录路径不能被脏数据打崩）。"""
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        # errors="replace"：password 带孤立代理时 encode("utf-8") 抛 UnicodeEncodeError（不是返回），
        # 会打崩 /auth/token。replace 降级成 ?，与 hash_password 的存储侧同一策略规范一致。
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8", "replace"), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, AttributeError, TypeError):
        return False
    # compare_digest 要求两边等长，否则抛 ValueError。生产数据由 hash_password 生成、恒 64 位 hex，
    # 但脏数据（stored 被篡改 / hash_hex 长度错）不能靠抛异常暴露——显式长度判定，不满足即 False。
    dk_hex = dk.hex()
    if len(dk_hex) != len(hash_hex):
        return False
    return hmac.compare_digest(dk_hex, hash_hex)


_admin_cache = None


def _admin_credential():
    """demo 管理员账号：从环境变量读，启动时算一次哈希存内存。

    生产当然是查用户表；这里的意义是演示「明文密码不落地、只存慢哈希」。
    """
    global _admin_cache
    if _admin_cache is None:
        user = os.environ.get("ADMIN_USER", "").strip()
        pw = os.environ.get("ADMIN_PASSWORD", "")
        if not user or not pw:
            return None
        _admin_cache = (user, hash_password(pw))
    return _admin_cache


def _ct_equal(a: str, b: str) -> bool:
    """常数时间比较，且**长度无关**。

    直接 hmac.compare_digest(a, b) 在 a/b 长度不同时会抛 ValueError（不是返回 False）——
    用「不存在用户名 + 错密码」一测就暴露（成了 500）。改成先各自哈希成固定长度摘要再
    compare_digest：长度无关 + 常数时间，两个属性都保住。
    """
    # errors="replace"：孤立代理（U+D800 这种非法码点）encode("utf-8") 会抛 UnicodeEncodeError，
    # 不是返回——外部输入（用户名/密码）里带它就能把鉴权打崩成 500。replace 把非法码点替换成 ?，
    # 比较两边同一策略、结果一致，且长度无关 + 常数时间的属性不变。
    # str(a or "")：a 可能是非字符串（int/bool/dict），直接 .encode 抛 AttributeError（code-reviewer
    # 审出：authenticate(123, ...) → 500）。str() 先强制成字符串，非字符串用户名天然不匹配、返回 False。
    ha = hashlib.sha256(str(a or "").encode("utf-8", "replace")).digest()
    hb = hashlib.sha256(str(b or "").encode("utf-8", "replace")).digest()
    return hmac.compare_digest(ha, hb)


def authenticate(username: str, password: str):
    """校验账号密码，成功返回 role，失败返回 None。

    防用户枚举的两点：
    1. 调用方对「用户不存在」和「密码错」返回同一个 401（看 main.py 的 /auth/token）
    2. 这里两条校验都跑完、不短路，且都是常数时间 + 长度无关比较，
       避免「用户名不存在返回更快」的时序侧信道。
    """
    cred = _admin_credential()
    if not cred:
        return None
    user, stored = cred
    user_ok = _ct_equal(username or "", user)
    pw_ok = verify_password(password or "", stored)
    return "admin" if (user_ok and pw_ok) else None


# ═══════════════════════════════════════════════════════════════
# JWT 签发 / 校验
# ═══════════════════════════════════════════════════════════════

def _sign(signing_input: str) -> str:
    return _b64url_encode(hmac.new(_secret(), signing_input.encode("ascii"), hashlib.sha256).digest())


def create_token(sub: str, role: str, ttl: int = TOKEN_TTL) -> str:
    """签发 HS256 token。ttl 可为负（测试过期用）。"""
    now = int(time.time())
    header = {"alg": ALG, "typ": "JWT"}
    payload = {"sub": sub, "role": role, "iat": now, "exp": now + ttl, "jti": secrets.token_hex(8)}
    h = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    p = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{h}.{p}.{_sign(f'{h}.{p}')}"


def decode_token(token: str) -> dict:
    """校验并返回 payload。任何一步不过 → AuthError。

    顺序刻意：先验签名再解 payload —— 签名没过的 token 里的内容一个字都不该被采信。
    """
    if not isinstance(token, str):
        raise AuthError("token 类型非法")
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("token 段数非法")
    h, p, sig = parts

    # ① alg 硬校验：绝不按 token 自称的算法去验（alg=none / alg 降级攻击）
    try:
        header = json.loads(_b64url_decode(h))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise AuthError("header 解析失败")
    if not isinstance(header, dict) or header.get("alg") != ALG:
        raise AuthError(f"算法非法：{header.get('alg') if isinstance(header, dict) else '?'}")

    # ② 常数时间比对签名，防时序攻击
    if not hmac.compare_digest(sig, _sign(f"{h}.{p}")):
        raise AuthError("签名校验失败")

    # ③ 签名过了才解 payload、才校验过期
    try:
        payload = json.loads(_b64url_decode(p))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise AuthError("payload 解析失败")
    if not isinstance(payload, dict):
        raise AuthError("payload 非法")
    exp = payload.get("exp")
    if not isinstance(exp, int) or int(time.time()) >= exp:
        raise AuthError("token 已过期")
    return payload


def require_role(role: str):
    """FastAPI 依赖工厂：校验 Authorization: Bearer <token> 且 role 匹配。

    401 = 没通过身份认证（authentication）；403 = 身份认了但权限不够（authorization）。
    这两个码分清楚是基本功——401 让客户端去登录，403 让它别白试。
    """
    def _dep(authorization: str = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少或格式非法的 Authorization 头")
        try:
            payload = decode_token(authorization[7:].strip())
        except (AuthError, RuntimeError):
            # AuthError 细节不外泄（不告诉攻击者是签名错还是过期）；RuntimeError=密钥没配，同样不外泄
            raise HTTPException(status_code=401, detail="token 无效")
        if payload.get("role") != role:
            raise HTTPException(status_code=403, detail="权限不足")
        return payload
    return _dep
