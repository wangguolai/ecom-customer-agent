# -*- coding: utf-8 -*-
"""伪菜单路由 + 会话压缩落盘的回归网（2026-09-20）

钉住三件事：
  ① **菜单意图的白名单边界**：只认 `rules.MENU_INTENTS` 里的 id，**传工具名必须被拒**
     （`menu_intent` 是客户端可控输入，放行工具名 = 开放任意工具调用入口）。
  ② **零 LLM 的缺参反问**：必须设 `route_source="菜单"` / `end_reason="正常"` 并回填 assistant
     （不设的话这一轮会被算进「LLM 决策」**且算成技术失败**，把两个看板指标一起拉歪）。
  ③ **会话历史的导出契约**：只放行「摘要 + 已登记轮次」（白名单），
     且**摘要消息不能被裁掉**（它丢了压缩就白做，且再也回不来）。

用法：python tests/test_menu_and_session.py
"""

import sys
import os
import asyncio
from unittest import mock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.agent as agent
from src.backend import session_store
from src.intent_router import route_by_menu
from src.config.rules import MENU_INTENTS
from src.config.prompts import (
    SUMMARY_PREFIX, MEMORY_INJECT_PREFIX, NO_TOOL_SYNTAX_HINT,
    GUIDE_PREFIX, ROUTED_TOOL_PREFIX,
)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


HIST = [
    {"role": "user", "content": "我的订单 20240818001 到哪了"},
    {"role": "assistant", "content": "订单 20240818001 已发货，正在派送中。"},
]


def test_menu_whitelist():
    print("\n【① 菜单白名单】")
    for mid in MENU_INTENTS:
        r = route_by_menu(mid, f"菜单文案 {mid}", HIST)
        check(f"{mid} 是白名单成员、有返回", r is not None, repr(r))

    print("  ── 越权输入必须全拒 ──")
    for bad in ["search_orders", "refund_order", "check_stock", "ORDERS", "orders ",
                "../orders", "", None, 123, ["orders"], {"intent": "orders"}]:
        r = route_by_menu(bad, "查订单", HIST)
        check(f"拒绝 {bad!r}", r is None, f"实际 {r!r}")

    print("  ── 客户端不能覆写规则层的安全判定（否定守卫）──")
    check("human + 「我要转人工」→ 执行",
          route_by_menu("human", "我要转人工", HIST)[0] == "tool")
    check("human + 「不用转人工」→ 拒绝",
          route_by_menu("human", "不用转人工", HIST) is None)


def test_menu_param_resolution():
    print("\n【① 参数解析：本轮优先 → 上下文补 → 固定反问】")
    r = route_by_menu("orders", "查订单 20240817002", HIST)
    check("本轮带号优先于历史", r == ("tool", "search_orders", {"order_id": "20240817002"}), repr(r))

    r = route_by_menu("orders", "查订单", HIST)
    check("本轮没号 → 从历史补", r == ("tool", "search_orders", {"order_id": "20240818001"}), repr(r))

    r = route_by_menu("logistics", "查物流", HIST)
    check("物流同规则", r == ("tool", "search_logistics", {"order_id": "20240818001"}), repr(r))

    for mid in ("orders", "logistics"):
        r = route_by_menu(mid, "查订单", [])
        check(f"{mid} 无号无历史 → 固定反问", r[0] == "ask" and bool(r[2]), repr(r))

    print("  ── stock/products：product_id 拿不到（不外露 + 工具结果不进历史）→ 只能反问 ──")
    for mid in ("stock", "products"):
        r = route_by_menu(mid, "查库存", HIST)
        check(f"{mid} → 固定反问（不做「猜商品」）", r[0] == "ask", repr(r))

    print("  ── 无必需参数的菜单：args 从本轮消息组装 ──")
    r = route_by_menu("policy", "退货政策", HIST)
    check("policy → get_return_policy(query=本轮消息)",
          r == ("tool", "get_return_policy", {"query": "退货政策"}), repr(r))
    r = route_by_menu("human", "我要转人工", HIST)
    check("human → transfer_to_human(problem=本轮消息)",
          r == ("tool", "transfer_to_human", {"problem": "我要转人工"}), repr(r))


async def test_zero_llm_ask():
    print("\n【② 零 LLM 缺参反问】")
    called = {"llm": 0}

    async def boom(*a, **kw):
        called["llm"] += 1
        raise AssertionError("缺参反问路径**不该**调用 LLM")

    # 无 user_id → 记忆链路整体不启用；历史短 → 不触发压缩。全程零 LLM。
    s = agent.AgentSession()
    with mock.patch.object(agent, "chat_with_usage", boom), \
         mock.patch.object(agent, "stream_events", boom):
        ans = await s.chat("查订单", menu_intent="orders")
    t = s.get_last_trace()

    check("没调用任何 LLM", called["llm"] == 0, f"调了 {called['llm']} 次")
    check("返回固定反问文案", "订单号" in ans, repr(ans))
    check('route_source = "菜单"（否则被算进 LLM 决策）', t.route_source == "菜单", t.route_source)
    check('end_reason = "正常"（否则被算成技术失败）',
          t.end_reason == "正常", t.end_reason)
    check("assistant 已回填（下一轮补参时上下文不断）",
          s.messages[-1]["content"] == ans, repr(s.messages[-1]))


def _turn(session, q, a):
    """模拟一轮真实的 `stream_chat`：append user → agent 产出 assistant → 登记。"""
    session.messages.append({"role": "user", "content": q})
    session.messages.append({"role": "assistant", "content": a})
    session.record_turn(q, a)


def test_session_history_whitelist():
    print("\n【③ session_history 白名单导出】")
    s = agent.AgentSession()
    s.messages.extend([
        {"role": "user", "content": f"{SUMMARY_PREFIX}以下是此前对话的摘要：用户查过订单 X"},
        {"role": "user", "content": f"{MEMORY_INJECT_PREFIX}，仅作个性化参考】\n用户养金毛"},
        {"role": "user", "content": f"{GUIDE_PREFIX}浏览】用户在逛，请介绍分类"},
        {"role": "user", "content": NO_TOOL_SYNTAX_HINT},
        {"role": "user", "content": f"{ROUTED_TOOL_PREFIX} search_orders，结果如下…】"},
        {"role": "tool", "tool_call_id": "x", "content": "工具结果"},
        {"role": "assistant", "tool_calls": [{"id": "x"}]},
    ])
    _turn(s, "我要查订单", "请把订单号发我～")

    out = s.session_history()
    text = " ".join(str(m.get("content", "")) for m in out)

    check("摘要消息被保留（压缩的唯一产物）", SUMMARY_PREFIX in text, repr(text[:80]))
    check("本轮真实问答都在", "我要查订单" in text and "请把订单号发我～" in text)
    check("画像注入被排除", MEMORY_INJECT_PREFIX not in text)
    check("路由引导被排除", GUIDE_PREFIX not in text)
    check("泄漏降级的格式要求被排除", NO_TOOL_SYNTAX_HINT[:12] not in text)
    check("工具回填被排除", ROUTED_TOOL_PREFIX not in text)
    check("tool 角色 / tool_calls 被排除",
          all(m["role"] in ("user", "assistant") for m in out) and
          all(not m.get("tool_calls") for m in out), repr(out))
    check("条数 = 摘要 1 + 本轮 2", len(out) == 3, f"{len(out)}：{out}")
    check("摘要排在最前", str(out[0].get("content", "")).startswith(SUMMARY_PREFIX))


def test_current_turn_answer_is_authoritative():
    print("\n【③ 当前轮的 answer 以「用户实际看到的」为准】")
    # 异常路径不 append assistant、空回复兜底 append 的是「（空回复）」占位——
    # 用户看到的文本只能由调用方（SSE 聚合）回传。
    s = agent.AgentSession()
    s.messages.append({"role": "user", "content": "查订单"})
    s.messages.append({"role": "assistant", "content": "（空回复）"})   # 占位
    s.record_turn("查订单", "抱歉，系统暂时无法处理，请稍后重试。")
    out = s.session_history()
    check("占位文案被真实文案覆盖", out[-1]["content"] == "抱歉，系统暂时无法处理，请稍后重试。",
          repr(out[-1]))

    s2 = agent.AgentSession()          # 异常路径：压根没 append assistant
    s2.messages.append({"role": "user", "content": "查订单"})
    s2.record_turn("查订单", "抱歉，系统暂时无法处理，请稍后重试。")
    out2 = s2.session_history()
    check("没 append 时也要补上（否则这轮从历史里消失）",
          out2[-1]["role"] == "assistant" and "抱歉" in out2[-1]["content"], repr(out2))


def test_history_accumulates_across_turns():
    print("\n【③ 跨轮往返：历史必须累积（不能每轮覆盖）】")
    # 曾抓到两个功能回退：① 只导出本轮登记的 → 每轮覆盖掉全部历史；
    # ② 改成播种全部轮次后，摘要与「它替换掉的原文」**同时持久化**、压缩白做。
    # 正确基准是 `self.messages`（压缩改的正是它）。
    h = []
    for i, (q, a) in enumerate([("查订单", "请提供订单号"),
                                ("20240818001", "订单已发货"),
                                ("查物流", "物流轨迹…")], 1):
        s = agent.AgentSession(list(h))       # 模拟 session_store.load()
        _turn(s, q, a)
        h = s.session_history()
        check(f"第 {i} 轮后历史 = {i * 2} 条", len(h) == i * 2, f"{len(h)}：{h}")

    text = " ".join(str(m.get("content", "")) for m in h)
    check("第 1 轮的问答仍在（没被覆盖）", "查订单" in text and "请提供订单号" in text, text[:90])
    check("顺序正确（老的在前）",
          h[0]["content"] == "查订单" and h[-1]["content"] == "物流轨迹…", repr(h[:1]))

    print("  ── 被压缩掉的旧轮次**不能**再持久化（否则摘要白做）──")
    # 模拟压缩后的视图：摘要替换掉了前两轮
    compressed = [
        {"role": "user", "content": f"{SUMMARY_PREFIX}用户查过订单 20240818001，已答复。"},
        {"role": "user", "content": "查物流"},
        {"role": "assistant", "content": "物流轨迹…"},
    ]
    s = agent.AgentSession(list(compressed))
    s.record_turn("查物流", "物流轨迹…")
    out = s.session_history()
    check("只剩摘要 + 最近一轮（旧原文没被带回）", len(out) == 3, f"{len(out)}：{out}")
    check("摘要仍在且只有一条",
          sum(1 for m in out if str(m.get("content", "")).startswith(SUMMARY_PREFIX)) == 1)


def test_internal_prefix_registry():
    print("\n【③ 源码静态断言：新增内部注入必须登记】")
    import re as _re
    from src.config.prompts import INTERNAL_MSG_PREFIXES as REG
    src = open(os.path.join(_project_root, "src", "agent.py"), encoding="utf-8").read()

    # agent.py 里不应再出现**硬编码**的注入前缀字面量——都必须走登记表里的常量。
    # 这条守的是「将来有人加一处 `messages.append({"role":"user","content":"【新引导】…"})`
    # 却忘了登记」——那种漏项不会被任何行为测试发现，只会静默地把内部指令写进会话历史。
    hard = _re.findall(r'"【[^"】\n]{2,24}】', src)
    check("agent.py 无硬编码注入前缀（都走常量）", not hard, repr(hard))

    for p in REG:
        check(f"登记表项 {p[:14]!r} 在 prompts.py 有定义", isinstance(p, str) and bool(p))


def test_menu_negation_not_bypassable():
    print("\n【① 否定守卫不能被空白绕过】")
    # route_by_rule 匹配的是去空白副本，菜单分支优先且早退——用原始串判会让
    # 「不 用转人工」「不　用转人工」绕过守卫，等于客户端单方面覆写规则层安全判定。
    for variant in ["不用转人工", "不 用转人工", "不　用转人工", "不  用  转人工"]:
        r = route_by_menu("human", variant, HIST)
        check(f"拒绝 {variant!r}", r is None, f"实际 {r!r}")

    print("  ── 订单号也要在去空白副本上提取（与 route_by_rule 对齐）──")
    r = route_by_menu("orders", "查订单 2024 0818001", [])
    check("被打散的订单号仍能提取", r and r[0] == "tool", repr(r))


def test_trim_keeps_summary():
    print("\n【③ 丢轮次降级不能删掉摘要】")
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": f"{SUMMARY_PREFIX}摘要正文，含订单号 20240818001"}]
    for i in range(6):
        msgs.append({"role": "user", "content": f"问题{i}" * 200})
        msgs.append({"role": "assistant", "content": f"回答{i}" * 200})
    agent._trim_history(msgs, 500)
    text = " ".join(str(m.get("content", "")) for m in msgs)
    check("摘要仍在（它 role=user 且排最前，最容易第一个被删）",
          SUMMARY_PREFIX in text, f"剩余 {len(msgs)} 条")
    check("确实丢了轮次（不是没生效）", len(msgs) < 14, f"剩余 {len(msgs)} 条")


def test_cap_keeps_summary():
    print("\n【③ 会话层条数裁剪不能把摘要裁掉】")
    hist = [{"role": "user", "content": f"{SUMMARY_PREFIX}摘要"}] + [
        {"role": r, "content": f"第{i}轮"} for i in range(30) for r in ("user", "assistant")
    ]
    capped = session_store._cap(hist)
    check(f"裁到上限 {session_store.MAX_TURNS * 2} 条",
          len(capped) == session_store.MAX_TURNS * 2, f"{len(capped)}")
    check("摘要仍在最前", str(capped[0].get("content", "")).startswith(SUMMARY_PREFIX),
          repr(capped[0])[:60])

    short = [{"role": "user", "content": f"{SUMMARY_PREFIX}摘要"}, {"role": "user", "content": "q"}]
    check("未超上限时原样返回", session_store._cap(short) == short)


def test_category_context():
    print("\n【④ 类别跨轮补参（类别下沉）】")
    from src.tools import detect_category, find_recent_category
    from src.agent import _with_context_category

    print("  ── 泛指词（之前一个都不命中 → 不锁类别 → 全库检索）──")
    for q, want in [("我想知道猫猫吃什么比较好", "猫粮"), ("猫吃什么", "猫粮"),
                    ("狗狗吃什么", "狗粮"), ("狗吃啥", "狗粮")]:
        check(f"{q!r} → {want}", detect_category(q) == want, repr(detect_category(q)))

    print("  ── S1 回归守卫：泛义词**不许跟具体词抢分** ──")
    # 泛义词并进主表时实测踩到的两个真回归（code-review 用新旧词表对拍全仓 case 查出来的）：
    #   ① 「洁齿骨，能让狗狗当饭吃吗」零食(洁齿骨)=1 vs 狗粮(狗+狗狗)=2 → 狗粮赢，
    #      而洁齿骨是**零食**类商品 → 预过滤把正确答案整个滤掉；
    #   ② 「有没有猫咪吃的洁齿骨」各 1 分 → 打平 → 不锁 → 退回全库检索（就是本批要修的形态）。
    # 修法是**两轮打分**：具体词全落空才用泛义词。
    for q, want in [("我买的那个洁齿骨，能让狗狗当饭吃吗", "零食"),
                    ("有没有猫咪吃的洁齿骨", "零食"),
                    ("有没有狗狗用的洁齿骨", "零食")]:
        check(f"具体词压过泛义词：{q[:18]!r}… → {want}",
              detect_category(q) == want, repr(detect_category(q)))
    from src.tools import CATEGORY_KEYWORDS, GENERIC_KEYWORDS
    check("泛义词在独立表里（不在 CATEGORY_KEYWORDS）",
          not any("猫" == w or "狗" == w for ws in CATEGORY_KEYWORDS.values() for w in ws))
    check("泛义词表非空且两类别都有",
          set(GENERIC_KEYWORDS) == {"猫粮", "狗粮"}, repr(GENERIC_KEYWORDS))

    print("  ── S2 回归守卫：内部注入块不能被当成「用户说过的类别」──")
    # 画像块 / 路由引导 / 工具回填都是 role=user，但不是用户说的话。不跳过的话：
    #   · 画像块里有「宠物：猫」→ 之后所有无类别追问都被锁成猫粮（用户压根没提过猫）
    #   · 工具回填里有商品名 → **查过一次订单就决定了下一轮锁狗粮**
    from src.config.prompts import GUIDE_PREFIX, ROUTED_TOOL_PREFIX, MEMORY_INJECT_PREFIX
    for label, injected in [
        ("画像块", f"{MEMORY_INJECT_PREFIX}，仅作参考】\n- 宠物：猫"),
        ("路由引导块", f"{GUIDE_PREFIX}浏览】我们的商品分类（狗粮、猫粮、零食）"),
        ("工具回填块", f"{ROUTED_TOOL_PREFIX} search_orders，结果如下】\n中大型成犬特应性皮炎全价处方粮"),
    ]:
        h = [{"role": "user", "content": injected}]
        check(f"{label}被跳过（不给类别线索）",
              find_recent_category(h) is None, repr(find_recent_category(h)))
    check("真实用户发言仍然生效",
          find_recent_category([{"role": "user", "content": "有没有狗狗用的"}]) == "狗粮")

    print("  ── find_recent_category 从历史找最近类别 ──")
    hist = [{"role": "user", "content": "我想知道猫猫吃什么比较好"},
            {"role": "assistant", "content": "给您找到几款猫粮…"}]
    check("从历史取到猫粮", find_recent_category(hist) == "猫粮", repr(find_recent_category(hist)))
    check("空历史返回 None", find_recent_category([]) is None)
    check("没有类别的历史返回 None",
          find_recent_category([{"role": "user", "content": "你好"}]) is None)
    # 取**最近**一条：用户改口说狗 → 应该跟到狗
    mixed = hist + [{"role": "user", "content": "算了，我家狗吃什么"}]
    check("取最近一条（用户改口后跟到狗粮）",
          find_recent_category(mixed) == "狗粮", repr(find_recent_category(mixed)))

    print("  ── _with_context_category 注入规则 ──")
    q = {"query": "有没有别的品牌的"}
    check("本句无类别 → 补上会话类别",
          _with_context_category("search_products", q, hist) == {"query": q["query"], "category": "猫粮"})
    check("已经带了合法类别 → 不覆盖",
          _with_context_category("search_products", {"query": "x", "category": "狗粮"}, hist)
          == {"query": "x", "category": "狗粮"})
    check("⚠️ 非法 category（LLM 幻觉）→ 丢弃，不采信",
          _with_context_category("search_products", {"query": "x", "category": "狗"}, hist)
          == {"query": "x", "category": "猫粮"},
          "未知值会一路进 Qdrant MatchValue → 0 命中 → 静默拒答")
    check("非检索工具 → 原样不动",
          _with_context_category("search_orders", {"order_id": "1"}, hist) == {"order_id": "1"})
    check("会话里没类别 → 不补（保召回）",
          _with_context_category("search_products", q, []) == q)
    check("args 非 dict（解析失败）→ 不崩",
          _with_context_category("search_products", None, hist) is None)

    print("  ── search_products 的 category 参数是显式优先的 ──")
    import inspect
    from src.tools import search_products, TOOL_SCHEMAS
    check("工具签名接受 category", "category" in inspect.signature(search_products).parameters)
    sp = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "search_products")
    check("⚠️ category **刻意不在 TOOL_SCHEMAS 里**（不让 LLM 填）",
          "category" not in sp["function"]["parameters"]["properties"],
          repr(list(sp["function"]["parameters"]["properties"])))


def test_metrics_label():
    print("\n【② 菜单轮计入「规则路由占比」】")
    from src.infra.observability import MetricsStore, RULE_ROUTE_LABELS
    check('"菜单" 在 RULE_ROUTE_LABELS 里', "菜单" in RULE_ROUTE_LABELS)
    check('"规则降级" **不在**（它没省下决策调用，计入会高估）',
          "规则降级" not in RULE_ROUTE_LABELS)

    class T:
        route_source = "菜单"
    class Fake:
        def __init__(self, rs):
            self._rs = rs
        def summary(self):
            return {"总耗时(秒)": 0.1, "总 token 消耗": 1, "结束原因": "正常",
                    "路由来源": self._rs, "缓存命中 token": 0, "缓存未命中 token": 0,
                    "推理 token": 0, "completion token": 1}
    m = MetricsStore()
    m.record(Fake("菜单"))
    m.record(Fake("规则"))
    m.record(Fake("规则降级"))
    s = m.summary()
    check("规则路由占比 = 2/3（菜单 + 规则，不含降级）",
          s["规则路由占比"] == round(2 / 3, 4), str(s["规则路由占比"]))


async def main():
    test_menu_whitelist()
    test_menu_param_resolution()
    test_menu_negation_not_bypassable()
    await test_zero_llm_ask()
    test_session_history_whitelist()
    test_current_turn_answer_is_authoritative()
    test_history_accumulates_across_turns()
    test_internal_prefix_registry()
    test_trim_keeps_summary()
    test_cap_keeps_summary()
    test_category_context()
    test_metrics_label()
    print("\n" + "-" * 60)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return FAIL


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
