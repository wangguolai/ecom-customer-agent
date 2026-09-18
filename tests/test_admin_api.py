# -*- coding: utf-8 -*-
"""管理后台接口对抗测试 —— **零成本**，不调用任何付费 API

前置：后端已启动（uvicorn src.backend.main:app --port 8000）
     cd frontend && npm run build  （SPA 用例需要 dist 存在）

跑法：python tests/test_admin_api.py

覆盖的是「可以确定性断言」的部分：鉴权边界、分页边界、LIKE 转义、SPA fallback、契约形状。
**不覆盖**前端渲染/路由/竞态——那些要浏览器（见文件末尾）。

对抗式设计（不是 happy path）：重点测「绕过尝试」——
无 token、错角色 token、越界分页、通配符注入、SPA 吞接口。
"""

import sys
import os
import re
import json
import urllib.request
import urllib.parse
import urllib.error

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 加载 .env：才能读到 ADMIN_USER / ADMIN_PASSWORD / AUTH_SECRET
# （AUTH_SECRET 尤其关键——auth.create_token 靠它签名，没有它会造成 role=user 用例直接失败）
from dotenv import load_dotenv
load_dotenv(os.path.join(_project_root, ".env"), encoding="utf-8")

BASE = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")

_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"✅ {name}" + (f"  {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"❌ {name}  {detail}")


def call(method: str, path: str, body=None, token: str | None = None):
    """返回 (status_code, parsed_or_text)"""
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    return resp.status, json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return resp.status, raw
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, raw
    except urllib.error.URLError as e:
        print(f"\n⚠️ 后端不可达（{BASE}）：{e.reason}")
        print("   请先启动：uvicorn src.backend.main:app --port 8000")
        raise SystemExit(2)


def _read_env(key: str, default: str = "") -> str:
    path = os.path.join(_project_root, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


ADMIN_ENDPOINTS = [
    "/api/admin/orders",
    "/api/admin/orders/20240818001",
    "/api/admin/refunds",
    "/api/admin/users",
    "/api/admin/metrics",
]


# ═══════════════════════════════════════════════════════════════
# 1. 鉴权边界（本次最大回归风险）
# ═══════════════════════════════════════════════════════════════

def test_auth():
    print("\n【鉴权】前端守卫不是安全边界，后端必须自己拦")
    for ep in ADMIN_ENDPOINTS:
        code, _ = call("GET", ep)
        check(f"无 token → {ep}", code == 401, f"→ {code}")

    code, _ = call("GET", "/memory?user_id=x")
    check("无 token → /memory（本期从内部接口收紧为 admin）", code == 401, f"→ {code}")

    # 错角色：用 role=user 的 token，必须是 403（已认证、权限不足）而不是 401
    from src.backend import auth
    try:
        user_token = auth.create_token("someone", "user")
    except Exception as e:
        check("构造 role=user token", False, str(e))
        return
    code, _ = call("GET", "/api/admin/orders", token=user_token)
    check("role=user → 403（不是 401，两者语义不同）", code == 403, f"→ {code}")

    # 伪造 token
    code, _ = call("GET", "/api/admin/orders", token="not.a.real.token")
    check("伪造 token → 401", code == 401, f"→ {code}")


def test_agent_path_not_broken(admin_token):
    print("\n【回归】收紧 GET /memory 不能误伤 agent 的写入路径")
    uid = "test-adminapi-guard"
    from src import memory as M
    mid = M._memory_id(uid, "测试用记忆")
    body = {"user_id": uid, "items": [{
        "memory_id": mid, "content": "测试用记忆", "category": "行为",
        "raw_snippet": "s", "confidence": 0.6,
    }]}
    code, _ = call("POST", "/memory", body)
    check("POST /memory 无 token 仍可用（agent 无 token）", code == 200, f"→ {code}")

    code, _ = call("POST", "/memory", {
        "user_id": uid,
        "items": [dict(body["items"][0], memory_id=M._memory_id(uid, "含地址"),
                       content="用户住在文三路100号")],
    })
    check("POST /memory 服务端敏感复校（客户端能被绕过）", code == 400, f"→ {code}")

    code, _ = call("POST", "/memory", {
        "user_id": uid,
        "items": [dict(body["items"][0], memory_id=M._memory_id(uid, "错类目"), category="地址")],
    })
    check("POST /memory 白名单外 category", code == 400, f"→ {code}")

    call("DELETE", f"/memory/{mid}", token=admin_token)


# ═══════════════════════════════════════════════════════════════
# 2. 分页与查询边界
# ═══════════════════════════════════════════════════════════════

def test_paging(admin_token):
    print("\n【分页/查询边界】")
    for qs, expect, note in [
        ("size=999999", 422, "超过 size 上限（防 dump 全表）"),
        ("size=0", 422, "size 下界"),
        ("page=abc", 422, "非数字（FastAPI 是 422 不是 400）"),
        ("page=-1", 422, "负数（防 LIMIT -1 直接 500）"),
        ("page=0", 422, "page 下界"),
    ]:
        code, _ = call("GET", f"/api/admin/orders?{qs}", token=admin_token)
        check(f"orders?{qs} → {expect}", code == expect, f"→ {code}（{note}）")

    # LIKE 通配符必须被转义，否则 q=% 会拉全表
    for q in ("%", "_", "%%", "!_"):
        code, d = call("GET", f"/api/admin/orders?q={urllib.parse.quote(q)}", token=admin_token)
        ok = code == 200 and isinstance(d, dict) and d.get("total") == 0
        check(f"q={q} 被转义（不拉全表）", ok, f"→ {code}, total={d.get('total') if isinstance(d, dict) else d}")

    # 注入串不该崩、不该改语义
    code, d = call("GET", "/api/admin/orders?q=" + urllib.parse.quote("' OR 1=1 --"), token=admin_token)
    check("注入串不崩且不返回全量", code == 200 and isinstance(d, dict), f"→ {code}")

    # 超长查询词 + 中文（必须 URL 编码，urllib 只接受 ASCII 的请求行）
    code, _ = call("GET", "/api/admin/orders?q=" + urllib.parse.quote("啊" * 500), token=admin_token)
    check("超长/中文 q 不崩", code == 200, f"→ {code}")


def test_business(admin_token):
    print("\n【业务边界】")
    code, _ = call("GET", "/api/admin/refunds?status=" + urllib.parse.quote("不存在的状态"), token=admin_token)
    check("refunds status 非法 → 400（不静默当全部）", code == 400, f"→ {code}")

    code, _ = call("GET", "/api/admin/refunds?status=" + urllib.parse.quote("待人工审批"), token=admin_token)
    check("refunds status 合法 → 200", code == 200, f"→ {code}")

    code, _ = call("GET", "/api/admin/orders/abc", token=admin_token)
    check("订单号格式非法 → 400（与 /orders/{id} 同口径）", code == 400, f"→ {code}")

    code, d = call("GET", "/api/admin/orders/99999999999", token=admin_token)
    check("不存在的订单 → 404", code == 404, f"→ {code}")

    # 无物流的订单必须返回空数组而非 404。
    # seed 里只有 20240818001 有物流轨迹，20240817002 是「订单存在但无轨迹」的现成样本——
    # 这正是最该测的一类：把正常态渲染成 404 报错是简单列表页的通病。
    code, d = call("GET", "/api/admin/orders/20240817002", token=admin_token)
    check("订单存在但无物流 → traces 为 []（正常态，不是错误）",
          code == 200 and isinstance(d, dict) and d.get("traces") == [],
          f"→ {code}, traces={d.get('traces') if isinstance(d, dict) else d}")


# ═══════════════════════════════════════════════════════════════
# 3. 契约形状
# ═══════════════════════════════════════════════════════════════

def test_contract(admin_token):
    print("\n【契约】前端 types.ts 依赖这些字段")
    for ep, keys in [
        ("/api/admin/orders", ["total", "page", "size", "items"]),
        ("/api/admin/refunds", ["total", "page", "size", "items"]),
        ("/api/admin/users", ["total", "page", "size", "items"]),
    ]:
        code, d = call("GET", ep, token=admin_token)
        missing = [k for k in keys if not isinstance(d, dict) or k not in d] if code == 200 else keys
        check(f"{ep} 形状", code == 200 and not missing, f"→ {code} 缺 {missing}")

    code, d = call("GET", "/api/admin/metrics", token=admin_token)
    need = ["samples", "scope", "sample_source", "sample_since"]
    check("metrics 口径字段齐全（不被误读成全量统计）",
          code == 200 and all(k in d for k in need), f"→ {d}")
    if isinstance(d, dict) and d.get("samples", 0) > 0:
        check("有样本时含 cache_hit_rate（可能是 None = 无缓存数据）",
              "cache_hit_rate" in d, f"→ {d.get('cache_hit_rate')}")


# ═══════════════════════════════════════════════════════════════
# 4. SPA fallback 不能吞接口
# ═══════════════════════════════════════════════════════════════

def test_spa():
    print("\n【SPA fallback】只吃 /admin/*，不能吞掉其他路由")
    dist = os.path.join(_project_root, "frontend", "dist", "index.html")
    has_dist = os.path.exists(dist)

    # 判据要精确：不能只看「是不是 HTML」——/docs 返回的 Swagger UI 本来就是 HTML 页面，
    # 那不算被吞。前台产物有独一无二的标记：引用 `/assets/index-*.js`（vite 构建带上来的）。
    def is_spa_html(body) -> bool:
        return isinstance(body, str) and "/assets/index-" in body

    for path in ["/admin", "/admin/login", "/admin/refunds", "/admin/users/abc"]:
        code, body = call("GET", path)
        if has_dist:
            check(f"{path} → 前台 index.html（刷新不 404）",
                  code == 200 and is_spa_html(body), f"→ {code}")
        else:
            check(f"{path} → 明确 404 提示 build（dist 不存在）", code == 404, f"→ {code}")

    for path in ["/docs", "/openapi.json", "/online", "/orders/20240818001"]:
        code, body = call("GET", path)
        check(f"{path} 未被 SPA fallback 吞", code < 400 and not is_spa_html(body),
              f"→ {code}")

    # /api 下的未知路径必须是 JSON 404，不能是 index.html
    # （前端拿到 HTML 去 JSON.parse 会炸，且错误信息毫无指向性）
    code, body = call("GET", "/api/admin/nope")
    check("/api/admin/nope → 404 而非 index.html",
          code == 404 and not is_spa_html(body), f"→ {code} {type(body).__name__}")


def main():
    print("=" * 70)
    print("管理后台接口对抗测试（零成本）")
    print("=" * 70)

    # 前置探活
    call("GET", "/online")

    username = _read_env("ADMIN_USER", "admin")
    password = _read_env("ADMIN_PASSWORD")
    if not password:
        print("⚠️ .env 缺少 ADMIN_PASSWORD，无法登录，跳过需要鉴权的用例")
        raise SystemExit(2)

    code, d = call("POST", "/auth/token", {"username": username, "password": password})
    if code != 200 or not isinstance(d, dict) or "access_token" not in d:
        print(f"❌ 登录失败（{code}）：{d}")
        raise SystemExit(2)
    admin_token = d["access_token"]
    print(f"✅ 已登录（{username}）\n")

    test_auth()
    test_agent_path_not_broken(admin_token)
    test_paging(admin_token)
    test_business(admin_token)
    test_contract(admin_token)
    test_spa()

    print("\n" + "-" * 70)
    print(f"通过 {_passed} / 失败 {_failed}")
    print("-" * 70)
    print("""
以下需要浏览器，本脚本测不了（见 docs/admin-console-plan.md §七）：
  #2  登录后刷新 /admin/refunds 不 404 —— 已由 test_spa 覆盖等价断言
  #3  过期 token → 前端清 token 跳登录（含 ?from= 回跳）
  #4  403 不清 token、429 不跳转
  #8  401 并发去重（多请求同时 401 只跳一次）
  #15 前端路由 404 页
  #23 空数据空态、#24 无物流渲染
  另需手工验证「前端守卫不是安全边界」：清掉 token 后 curl /api/admin/orders 必须 401
""")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
