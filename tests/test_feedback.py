# -*- coding: utf-8 -*-
"""反馈评分对抗测试（模块 D）—— **零成本**，不调用任何付费 API

前置：后端已启动（uvicorn src.backend.main:app --port 8000）
跑法：python tests/test_feedback.py

对抗式设计（不是 happy path）：重点测「绕过尝试」与「边界」——
  ① 静态守卫：评分链路不得触碰坏 case 池（R10，靠源码文本断言而非自觉）
  ② 入参白名单：trace_id 格式 / rating / reason 枚举 / bool 混入 / 超长 comment
  ③ 幂等：改主意是更新不是新增（且不产生第二行）
  ④ 并发幂等：两个首次提交同时到达
  ⑤ 关联：评分能回溯到 trace（trace 缺失是正常态，不是错误）
  ⑥ 鉴权：后台接口的 admin 边界
  ⑦ 限流隔离：打满 feedback 桶**不得**影响 read 桶（这正是它独立成桶的理由）

⚠️ 本脚本只写/删**自己固定构造的测试数据**（trace_id 以 a1b2c3d4 开头），
   不碰任何真实用户数据。收尾会清理。
"""

import sys
import os
import json
import time
import threading
import urllib.request
import urllib.error

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(os.path.join(_project_root, ".env"), encoding="utf-8")

BASE = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")

# 测试专用 trace_id（固定值 = 可重复跑 + 收尾能精确清理；16 位小写 hex 与生成规则一致）
TID = "a1b2c3d4e5f60001"
TID_NO_TRACE = "a1b2c3d4e5f60002"
TID_CONCURRENT = "a1b2c3d4e5f60003"
TID_REVISED_OLD = "a1b2c3d4e5f60004"
TID_REVISED_NEW = "a1b2c3d4e5f60005"
TID_UNKNOWN = "a1b2c3d4e5f6ffff"
ALL_TIDS = (TID, TID_NO_TRACE, TID_CONCURRENT, TID_REVISED_OLD, TID_REVISED_NEW)

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
            try:
                return resp.status, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, raw
    except urllib.error.URLError as e:
        print(f"❌ 无法连接后端 {BASE}：{e}")
        raise SystemExit(2)


def _db():
    from src.backend.db import get_conn
    return get_conn()


def _exec(sql, params=()):
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
    finally:
        from src.backend.db import close_conn
        close_conn(conn)


def _query_one(sql, params=()):
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        from src.backend.db import close_conn
        close_conn(conn)


def wait_for_window():
    """等限流窗口（10s）滑过去。

    ⚠️ 为什么测试里要主动等：**被拒绝的请求同样计入限流桶**（`_limit_feedback` 是
    Depends，在 handler 之前执行，400 的请求照样占一个名额）。这是刻意的设计——
    不计数的话，攻击者可以用「大量非法请求」免费探测接口。
    代价是测试里前面那些「故意发非法入参」的用例会把桶吃掉，
    后面的正常用例就可能被 429 挤掉。**等一个窗口比调大阈值更干净**：
    阈值调大就不是在测真实配置了。
    """
    time.sleep(11)


# ═══════════════════════════════════════════════════════════════
# 0. 前置：造一条测试 trace（真实 trace 要跑 LLM = 花钱；这里直接插库，
#    测的是「关联能不能成立」，不是「trace 怎么产生的」——那是 test_stream 的事）
# ═══════════════════════════════════════════════════════════════

def seed_trace():
    print("\n【前置】自造一条测试 trace（零成本，不跑 LLM）")
    _exec("DELETE FROM traces WHERE trace_id=%s", (TID,))
    _exec(
        "INSERT INTO traces (trace_id, session_id, user_id, query, answer, route_source, "
        "end_reason, total_sec, total_tokens, cache_hit, cache_miss, llm_steps, "
        "tool_calls, retrieved_ids, prompt_version, kb_version, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (TID, "sess-test", "user-test", "狗粮有没有推荐的", "有的，皇家…", "规则", "正常",
         1.23, 456, 100, 356, 2,
         json.dumps([{"name": "search_products", "elapsed": 0.03, "empty": False}], ensure_ascii=False),
         json.dumps(["products:P001"], ensure_ascii=False),
         "deadbeef", "cafebabe", time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    check("测试 trace 已就绪", _query_one("SELECT trace_id FROM traces WHERE trace_id=%s", (TID,)) is not None)


# ═══════════════════════════════════════════════════════════════
# 1. 静态守卫（R10）——「低分不进池」不能靠自觉
# ═══════════════════════════════════════════════════════════════

def test_no_pool_wiring():
    print("\n【静态守卫】评分链路不得触碰坏 case 池")
    # 注：本函数不发请求，不消耗限流桶，所以放在最前面
    # 为什么用**源码文本断言**而不是行为断言：`POST /api/feedback` 根本碰不到池
    # （regression.py 的入口只被两个 eval 脚本调用），所以「提交差评 → 池无变化」
    # 是个**恒真的同义反复**，测了等于没测。真正会出错的是**将来有人接上去**，
    # 而那种改动一定会在源码里留下 import —— 盯住源码才是可执行的守卫。
    for rel in ("src/backend/main.py", "src/backend/feedback_store.py"):
        with open(os.path.join(_project_root, rel), encoding="utf-8") as f:
            src = f.read()
        # 排除注释里提到 regression 的情况：只看 import 语句
        bad = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith(("import ", "from ")) and "regression" in ln]
        check(f"{rel} 不 import regression", not bad, f"→ {bad}")


# ═══════════════════════════════════════════════════════════════
# 2. 入参白名单与边界
# ═══════════════════════════════════════════════════════════════

def test_validation():
    print("\n【校验】匿名可写接口的入参白名单（服务端才是权威边界）")

    cases = [
        ("trace_id 为空", {"trace_id": "", "rating": 1}, 400),
        ("trace_id 非 hex", {"trace_id": "zzzzzzzzzzzzzzzz", "rating": 1}, 400),
        ("trace_id 长度不符", {"trace_id": "abc", "rating": 1}, 400),
        ("trace_id 大写 hex（生成侧是小写，大写应当拒绝）", {"trace_id": "A1B2C3D4E5F60001", "rating": 1}, 400),
        ("trace_id 非字符串", {"trace_id": 123, "rating": 1}, 400),
        ("rating=0", {"trace_id": TID, "rating": 0}, 400),
        ("rating=2", {"trace_id": TID, "rating": 2}, 400),
        ("rating='1' 字符串", {"trace_id": TID, "rating": "1"}, 400),
        ("rating=True（bool 是 int 子类，最容易漏的脏输入）", {"trace_id": TID, "rating": True}, 400),
        # `1.0 in (1, -1)` 也是 True —— 白名单的包含判断**不等于类型校验**。
        # 只挡 bool 会从这里漏浮点进去（JSON 里 `1.0` 完全合法）。
        ("rating=1.0 浮点", {"trace_id": TID, "rating": 1.0}, 400),
        ("rating=-1.0 浮点", {"trace_id": TID, "rating": -1.0}, 400),
        ("rating=None", {"trace_id": TID, "rating": None}, 400),
        ("差评缺 reason", {"trace_id": TID, "rating": -1}, 400),
        ("差评 reason 不在枚举", {"trace_id": TID, "rating": -1, "reason": "心情不好"}, 400),
    ]
    for name, body, want in cases:
        code, _ = call("POST", "/api/feedback", body)
        check(name + f" → {want}", code == want, f"→ {code}")

    # 好评携带 reason 不报错，但要被清空（避免「好评 + 答非所问」这种自相矛盾的落库）
    call("POST", "/api/feedback", {"trace_id": TID, "rating": 1, "reason": "答非所问"})
    row = _query_one("SELECT reason FROM feedback WHERE feedback_id=%s", (TID,))
    check("好评携带 reason → 落库被清空", row is not None and (row[0] or "") == "", f"→ {row}")

    # 超长 comment 必须被截断而不是「Data too long → 整条丢」
    long_comment = "长" * 600
    code, _ = call("POST", "/api/feedback", {"trace_id": TID, "rating": -1, "reason": "其他", "comment": long_comment})
    check("超长 comment 提交成功（截断而非报错）", code == 200, f"→ {code}")
    row = _query_one("SELECT comment FROM feedback WHERE feedback_id=%s", (TID,))
    check("comment 已截断到 512", row is not None and len(row[0]) <= 512, f"→ len={len(row[0]) if row else 'N/A'}")


# ═══════════════════════════════════════════════════════════════
# 3. 幂等：改主意是更新不是新增
# ═══════════════════════════════════════════════════════════════

def test_idempotent():
    print("\n【幂等】用户改主意 = 更新，不是新增")
    _exec("DELETE FROM feedback WHERE feedback_id=%s", (TID,))

    code, d1 = call("POST", "/api/feedback",
                    {"trace_id": TID, "rating": -1, "reason": "答非所问", "comment": "第一次"})
    check("首次提交 → created", code == 200 and d1.get("status") == "created", f"→ {code} {d1}")

    time.sleep(1.1)  # 让 updated_at 与 created_at 可区分（秒级精度）
    code, d2 = call("POST", "/api/feedback",
                    {"trace_id": TID, "rating": 1, "comment": "改主意了"})
    check("二次提交 → updated", code == 200 and d2.get("status") == "updated", f"→ {code} {d2}")

    n = _query_one("SELECT COUNT(*) FROM feedback WHERE feedback_id=%s", (TID,))[0]
    check("库里仍只有 1 行", n == 1, f"→ {n} 行")

    row = _query_one("SELECT rating, reason, comment, created_at, updated_at FROM feedback WHERE feedback_id=%s", (TID,))
    check("评分已改为好评", row[0] == 1, f"→ {row[0]}")
    check("改为好评后 reason 被清空（不留上一次的差评标签）", (row[1] or "") == "", f"→ {row[1]}")
    check("comment 已更新", row[2] == "改主意了", f"→ {row[2]}")
    check("created_at 保留首次时间（时间趋势的基准不能丢）", row[3] != row[4], f"→ {row[3]} / {row[4]}")

    # ── 回归：同一秒内重复提交**完全相同**的评分必须 200 ──
    # 踩过的坑：MySQL 的 affected_rows 返回「实际改变的行数」而非「匹配的行数」，
    # 值没变（含秒级 updated_at 也一样）时 UPDATE 返回 0。若把它当「行不存在」抛错，
    # 正常的重复提交会全部 500 —— 真实用户连点两次就是这么挂的。
    codes = [call("POST", "/api/feedback", {"trace_id": TID, "rating": 1})[0] for _ in range(3)]
    check("连续 3 次完全相同的提交 → 全部 200（affected_rows=0 ≠ 行不存在）",
          codes == [200, 200, 200], f"→ {codes}")


def test_concurrent_first_submit():
    print("\n【并发】两个首次提交同时到达 → 仍只有一行")
    _exec("DELETE FROM feedback WHERE feedback_id=%s", (TID_CONCURRENT,))
    results = []
    lock = threading.Lock()

    def worker(body):
        code, d = call("POST", "/api/feedback", body)
        with lock:
            results.append((code, d.get("status") if isinstance(d, dict) else d))

    # 差评必须带 reason（白名单校验），否则请求根本到不了写库那一步 ——
    # 那就测不到「并发 INSERT 撞主键」这条路径了，测的成了参数校验。
    bodies = [{"trace_id": TID_CONCURRENT, "rating": 1},
              {"trace_id": TID_CONCURRENT, "rating": -1, "reason": "其他"}]
    ts = [threading.Thread(target=worker, args=(b,)) for b in bodies]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    n = _query_one("SELECT COUNT(*) FROM feedback WHERE feedback_id=%s", (TID_CONCURRENT,))[0]
    check("并发首次提交后仍是 1 行（IntegrityError 分支生效）", n == 1, f"→ {n} 行，结果={results}")
    check("两个请求都成功（没有 500）", all(c == 200 for c, _ in results), f"→ {results}")


# ═══════════════════════════════════════════════════════════════
# 4. 关联回溯：评分 ↔ trace
# ═══════════════════════════════════════════════════════════════

def test_trace_linkage(admin_token):
    print("\n【关联】评分能回溯到完整 trace —— 这条线的全部价值所在")
    call("POST", "/api/feedback", {"trace_id": TID, "rating": -1, "reason": "信息错误"})

    code, items = call("GET", "/api/admin/feedback?rating=-1&size=100", token=admin_token)
    hit = next((x for x in items.get("items", []) if x["trace_id"] == TID), None) if code == 200 else None
    check("差评筛选里能查到该条", hit is not None, f"→ {code}")
    check("列表带出 query（一眼看出评的是哪个问题）",
          hit is not None and hit.get("query") == "狗粮有没有推荐的", f"→ {hit.get('query') if hit else None}")
    check("has_trace=True", hit is not None and hit.get("has_trace") is True)

    code, detail = call("GET", f"/api/admin/feedback/{TID}", token=admin_token)
    tr = detail.get("trace") if code == 200 else None
    check("详情返回完整 trace", code == 200 and tr is not None, f"→ {code}")
    if tr:
        check("trace 含 prompt 版本", bool(tr.get("prompt_version")), f"→ {tr.get('prompt_version')}")
        check("trace 含知识库版本", bool(tr.get("kb_version")), f"→ {tr.get('kb_version')}")
        check("tool_calls 已解析成数组", isinstance(tr.get("tool_calls"), list), f"→ {type(tr.get('tool_calls')).__name__}")
        check("retrieved_ids 已解析成数组", tr.get("retrieved_ids") == ["products:P001"], f"→ {tr.get('retrieved_ids')}")

    # 好评筛选里不该出现这条（它现在是差评）
    code, good = call("GET", "/api/admin/feedback?rating=1&size=100", token=admin_token)
    check("好评筛选里不含该条", all(x["trace_id"] != TID for x in good.get("items", [])), f"→ {code}")

    # 非法筛选值
    code, _ = call("GET", "/api/admin/feedback?rating=5", token=admin_token)
    check("rating=5 → 400", code == 400, f"→ {code}")


def test_trace_missing(admin_token):
    print("\n【边界】trace 缺失是正常态，不是错误")
    # 场景：trace 落盘失败 / 请求在生成前就结束，但反馈照样收得到
    call("POST", "/api/feedback", {"trace_id": TID_NO_TRACE, "rating": -1, "reason": "没解决问题"})

    code, items = call("GET", "/api/admin/feedback?rating=-1&size=100", token=admin_token)
    hit = next((x for x in items.get("items", []) if x["trace_id"] == TID_NO_TRACE), None)
    check("列表里能看到该条", hit is not None, f"→ {code}")
    check("has_trace=False（前端据此渲染「该轮记录未保存」）",
          hit is not None and hit.get("has_trace") is False, f"→ {hit.get('has_trace') if hit else None}")

    code, detail = call("GET", f"/api/admin/feedback/{TID_NO_TRACE}", token=admin_token)
    check("详情仍返回 200（不是 404 —— 反馈本身存在）", code == 200, f"→ {code}")
    check("trace 字段为 null", isinstance(detail, dict) and detail.get("trace") is None)

    code, _ = call("GET", f"/api/admin/feedback/{TID_UNKNOWN}", token=admin_token)
    check("评分不存在 → 404", code == 404, f"→ {code}")
    code, _ = call("GET", "/api/admin/feedback/not-hex", token=admin_token)
    check("trace_id 格式非法 → 400", code == 400, f"→ {code}")


def test_revised_floats_up(admin_token):
    """把旧好评改成差评后，必须浮到队列顶部。

    **为什么这条值得单测**：列表按 `created_at` 排序时，「用户把好评改成差评」——
    最该被复盘的那个信号——会停在它**首次评分**的时间位置上，永远浮不上来。
    这条断言锁的是 `ORDER BY f.updated_at`，改回 created_at 会立刻变红。
    """
    print("\n【排序】改过主意的评分要浮到待复核队列顶部")
    for tid, ts in ((TID_REVISED_OLD, "2020-01-01 08:00:00"), (TID_REVISED_NEW, "2020-01-02 08:00:00")):
        _exec("DELETE FROM feedback WHERE feedback_id=%s", (tid,))
        _exec(
            "INSERT INTO feedback (feedback_id, trace_id, session_id, rating, reason, comment, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (tid, tid, "sess-old", 1, "", "", ts, ts),  # 两条都是 2020 年的旧好评
        )

    code, d = call("GET", "/api/admin/feedback?rating=1&size=100", token=admin_token)
    order = [x["trace_id"] for x in d.get("items", [])]
    check("两条旧行都在列表里", TID_REVISED_OLD in order and TID_REVISED_NEW in order, f"→ {code}")
    check("旧行按原有时间排（NE：新在前）",
          order.index(TID_REVISED_NEW) < order.index(TID_REVISED_OLD), f"→ {order[:4]}")

    # 把「更旧」的那条改成差评 —— 它现在应当跳到**整个列表的最前面**
    code, _ = call("POST", "/api/feedback",
                   {"trace_id": TID_REVISED_OLD, "rating": -1, "reason": "信息错误"})
    check("改评分提交成功", code == 200, f"→ {code}")

    code, d = call("GET", "/api/admin/feedback?size=100", token=admin_token)
    order = [x["trace_id"] for x in d.get("items", [])]
    check("改过主意的行浮到列表第一（ORDER BY updated_at 生效）",
          order and order[0] == TID_REVISED_OLD, f"→ 首位 {order[0] if order else None}")


# ═══════════════════════════════════════════════════════════════
# 5. 鉴权边界
# ═══════════════════════════════════════════════════════════════

def test_auth(admin_token):
    print("\n【鉴权】后台读评分必须 admin（写评分故意匿名，普通用户要能评）")
    for ep in ("/api/admin/feedback", f"/api/admin/feedback/{TID}"):
        code, _ = call("GET", ep)
        check(f"无 token → {ep} 401", code == 401, f"→ {code}")
        code, _ = call("GET", ep, token="not.a.real.token")
        check(f"伪造 token → {ep} 401", code == 401, f"→ {code}")

    from src.backend import auth
    user_token = auth.create_token("someone", "user")
    code, _ = call("GET", "/api/admin/feedback", token=user_token)
    check("role=user → 403（已认证但权限不足，不是 401）", code == 403, f"→ {code}")

    # 反向确认：写接口**不需要** token —— 这是刻意设计，不是漏了鉴权
    code, _ = call("POST", "/api/feedback", {"trace_id": TID, "rating": 1})
    check("POST /api/feedback 无需 token（普通用户要能评分）", code == 200, f"→ {code}")


# ═══════════════════════════════════════════════════════════════
# 6. 限流隔离（放最后：会打满 feedback 桶）
# ═══════════════════════════════════════════════════════════════

def test_ratelimit_isolation():
    print("\n【限流】打满 feedback 桶不得影响 read 桶 —— 这正是它独立成桶的理由")
    wait_for_window()  # 先拿到一个干净窗口，否则测不出「第几次开始 429」
    codes = []
    for _ in range(25):
        code, _ = call("POST", "/api/feedback", {"trace_id": TID, "rating": 1})
        codes.append(code)
    check("窗口内前 20 次放行", codes[:20] == [200] * 20, f"→ {codes[:5]}…")
    check("第 21 次起触发 429（阈值 20 精确生效）", codes[20] == 429, f"→ 第 21 次 {codes[20]}")

    # 关键断言：feedback 桶被打满，聊天/查询（read 桶）必须仍然可用。
    # 共用桶的话，匿名灌评分就能把整个聊天页打成 429 —— 攻击一个接口，瘫痪一片。
    # 用真实存在的订单号：404 也能证明「没被 429」，但 200 才是「业务确实还能跑」的证据。
    code, _ = call("GET", "/orders/20240818001")
    check("feedback 桶打满后 read 桶仍放行", code == 200, f"→ {code}")
    print("   （注：feedback 桶需等 10s 窗口过后才恢复）")


# ═══════════════════════════════════════════════════════════════
# 收尾：清理本脚本构造的测试数据
# ═══════════════════════════════════════════════════════════════

def cleanup():
    print("\n【收尾】清理测试数据（只删本脚本固定构造的 id）")
    for tid in ALL_TIDS:
        _exec("DELETE FROM feedback WHERE feedback_id=%s", (tid,))
        _exec("DELETE FROM traces WHERE trace_id=%s", (tid,))
    placeholders = ",".join(["%s"] * len(ALL_TIDS))
    left = _query_one(f"SELECT COUNT(*) FROM feedback WHERE feedback_id IN ({placeholders})", ALL_TIDS)[0]
    check("测试数据已清理干净", left == 0, f"→ 残留 {left} 行")


def main():
    print("=" * 70)
    print("反馈评分对抗测试（零成本）")
    print("=" * 70)

    call("GET", "/online")  # 前置探活
    seed_trace()

    username = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        print("⚠️ .env 缺少 ADMIN_PASSWORD，无法测鉴权与后台接口")
        raise SystemExit(2)
    code, d = call("POST", "/auth/token", {"username": username, "password": password})
    if code != 200 or not isinstance(d, dict) or "access_token" not in d:
        print(f"❌ 登录失败（{code}）：{d}")
        raise SystemExit(2)
    admin_token = d["access_token"]
    print(f"✅ 已登录（{username}）")

    test_no_pool_wiring()
    test_validation()
    # test_validation 发了十几个请求（含故意非法的），桶已接近满。
    # 后面测的是「写进去对不对」，不能再被限流干扰 —— 先等窗口滑过去。
    wait_for_window()
    test_idempotent()
    test_concurrent_first_submit()
    test_trace_linkage(admin_token)
    test_trace_missing(admin_token)
    test_revised_floats_up(admin_token)
    test_auth(admin_token)
    test_ratelimit_isolation()

    cleanup()

    print("\n" + "-" * 70)
    print(f"通过 {_passed} / 失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
