# -*- coding: utf-8 -*-
"""意图路由层：规则优先，LLM 兜底

在 ReAct 循环之前，用正则/关键字识别「高置信的常用命令意图」，直接路由到对应工具，
绕过 LLM 决策（省一次带 tools 的 LLM 调用）。

设计原则（规范）：
  - 判断交规则、表达交模型：规则只负责「意图 + 参数」，话术仍由 LLM 生成。
  - 三级漏斗（产品视角）：菜单/规则（常用命令，零成本）→ 假人工/agent（长尾，LLM 兜底）
    → 真人（明确转人工才出，且要 check_online 查在线）。
  - 只路由「自包含、高置信」的意图；多轮指代、模糊意图、语义检索、写操作（退款）走 LLM。
  - ⚠️ **例外：菜单路径（`route_by_menu`）故意破上一条**——它会从会话历史补参数。
    理由与边界见该函数的 docstring；手打输入仍严格守上一条。

能规则路由的（自包含、参数可精确提取）：
  search_orders / search_logistics（订单号强模式 + 关键词区分）
  get_return_policy（明确政策词）
  transfer_to_human（投诉/转人工强意图词，硬触发解决「软 prompt 不可靠」）
不能规则路由的（走 LLM）：
  search_products（语义检索）/ check_stock（两步链路 product_id）/ refund_order（写操作 + 复杂参数）
"""

import re

from src.derived.categories import build_category_keywords

# 词表 / 正则统一在 src/config/rules.py —— 加意图词只改那一个文件。
# 这里用 `as` 别名保住本文件的引用名（带下划线是本模块的历史命名），
# 避免重构时大面积改引用点引入笔误。想改词表请去 config/rules.py，不要在这里加。
from src.config.rules import (
    ORDER_ID_RE as _ORDER_ID_RE,
    ORDER_ID_FULL_RE as _ORDER_ID_FULL_RE,
    HUMAN_WORDS as _HUMAN_WORDS,
    NEGATE_WORDS as _NEGATE_WORDS,
    LOGISTICS_WORDS as _LOGISTICS_WORDS,
    ORDER_WORDS as _ORDER_WORDS,
    WRITE_INTENT_WORDS as _WRITE_INTENT_WORDS,
    POLICY_WORDS as _POLICY_WORDS,
    AFTERSALE_WORDS as _AFTERSALE_WORDS,
    SUMMARIZE_WORDS as _SUMMARIZE_WORDS,
    BROWSE_WORDS as _BROWSE_WORDS,
    MENU_INTENTS,
    MENU_ASK_TEXT,
)

# 订单号模式：11 位、20 开头（demo 种子数据 20240818001 这种）。
# 锚定 20 开头 + 前后不能是数字，防 11 位手机号（13x/15x/18x 开头）和「嵌在更长数字里」误匹配。
#
# 为什么用 (?<!\d)...(?!\d) 而不是 \b：Python 正则里中文也算 \w，
# 所以「订单20240818001什么状态」中「单」和「2」之间**没有**词边界 → \b 匹配不到，
# 规则路由静默失效、白白回落 LLM。而用户不打空格是常态。
# 换成「前后非数字」的断言：中文紧贴能识别，防手机号/长数字嵌套的效果不变。
# 类别触发词（类别 → 触发词列表），用于「浏览 vs 检索」边界判断：
# 「有什么推荐的猫粮」含类别词「猫粮」→ 是检索该类别，不是浏览；「随便看看」无类别词 → 真浏览。
# ⚠️ 它刻意不在 config/rules.py 里：这是从 data/category_synonyms.md **生成**的派生数据
# （src/derived/categories.py），属于 SSOT 三层里的派生层，不是人工维护的规则表。
_CATEGORY_WORDS = build_category_keywords()


def _has_category(query: str) -> bool:
    """query 是否命中任何类别触发词（有明确类别诉求）"""
    return any(w in query for words in _CATEGORY_WORDS.values() for w in words)


def _extract_order_id(text: str):
    """提取订单号（11 位、20 开头），两级：

    1. 常规正则 search——订单号完整无污染时直接命中；
    2. 数字碎片清洗兜底——被符号/空白打散（202￥408$$$180/01）时，
       findall 捞所有数字碎片拼接，再用 fullmatch 校验格式。
    两级都失败返回 None（走 LLM）。
    """
    m = _ORDER_ID_RE.search(text)
    if m:
        return m.group(0)
    digits = "".join(re.findall(r"\d+", text))
    m = _ORDER_ID_FULL_RE.fullmatch(digits)
    return m.group(0) if m else None


def _find_recent_order_id(messages: list):
    """从消息列表里找**最近提到**的订单号（从后往前扫）。

    扫 user 与 assistant 两类：用户自己报过号，助手回复里也常带（「订单 20240818001 已发货」）。
    只要「最近提到的那个」——刻意不做「挑一个最像的」这种判断，那又是把概率问题塞回规则层。
    """
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        oid = _extract_order_id(str(m.get("content", "")))
        if oid:
            return oid
    return None


def route_by_menu(menu_intent, user_msg: str, messages: list):
    """菜单意图路由（**确定性入口**，与 route_by_rule 的概率性输入分开）。

    返回与 `route_by_rule` 同构的三元组：
      `("tool", name, args)` —— 参数齐了，执行工具（调用方**复用规则路由的工具执行路径**）
      `("ask",  None, 文案)`  —— 缺参数，**固定反问**（零 LLM）
      `None`                 —— 不是白名单 id / 被否定守卫拦下 → 调用方落回 `route_by_rule`

    ⚠️ **本函数是模块契约的显式例外**（见文件头第 3 条）。菜单是用户**点出来的**确定性入口，
    不存在「猜意图」；而「从会话历史补参数」正是为了修「点一下菜单要等 6.5 秒做一次 LLM 决策」
    （实测：`查订单` 2 次 LLM 往返、工具一次没调）。例外**仅限菜单路径**——手打的裸意图词
    仍走 LLM，放宽会重演 2026-09-20 刚修的「裸词误伤」。

    ⚠️ **路由只由白名单 id 决定；`user_msg` 只当参数来源，不参与判断意图。**
    否则客户端可以发 `menu_intent=human` + `message="不用转人工"`，单方面覆写规则层的
    否定守卫——那是「客户端篡改安全判定」，不是配置错误。
    """
    entry = MENU_INTENTS.get(menu_intent) if isinstance(menu_intent, str) else None
    if entry is None:
        return None                      # 白名单外：不报错、不猜，落回规则层

    tool_name, param = entry

    # ⚠️ 否定守卫必须在**去空白副本**上判，不能拿原始串——`route_by_rule` 用的就是副本
    # （`re.sub(r"\s+", "", user_msg)`，见文件下方），而菜单分支是**优先且早退**的。
    # 用原始串的话，「不 用转人工」「不　用转人工」（半角/全角空格打断）会绕过守卫，
    # 拿到的正是「客户端单方面覆写规则层安全判定」——本函数 docstring 明令要防的事。
    compact = re.sub(r"\s+", "", user_msg or "")
    if any(w in compact for w in _NEGATE_WORDS):
        return None

    if param == "order":
        oid = _extract_order_id(compact) or _find_recent_order_id(messages)
        if oid:
            return ("tool", tool_name, {"order_id": oid})
        return ("ask", None, MENU_ASK_TEXT["order"])

    if param == "product":
        # 拿不到内部 product_id（明令不外露 + 工具结果不进会话历史）→ 只能反问商品名。
        # 刻意**不猜商品**：用户答了名字之后走正常检索链路，那条路本来就是对的。
        return ("ask", None, MENU_ASK_TEXT["product"])

    # 无必需参数：args 从本轮消息组装（与 route_by_rule 同款）
    if tool_name == "get_return_policy":
        return ("tool", tool_name, {"query": user_msg})
    if tool_name == "transfer_to_human":
        return ("tool", tool_name, {"problem": user_msg})
    return None


def route_by_rule(user_msg: str):
    """规则路由：返回 (kind, tool_name, args) 或 None（None = 交给 LLM）。

    kind ∈ {"tool", "browse", "summarize"}：
      - tool：高置信工具意图（订单/物流/政策/转人工），直接执行工具 + LLM 生成话术
      - browse：浏览意图（无明确目标），列分类概览
      - summarize：总结意图（想对比多款），反问澄清
    只处理「自包含、高置信」的意图。宁可漏（走 LLM）不可错（错误路由）。
    """
    # 去空格副本：删所有空白（含全角空格 　），规则层关键词/订单号都在副本上匹配。
    # 中文空格是噪声不是词边界（和英文相反）——「订　　单」被全角空格打断时，
    # _ORDER_WORDS 的「订单」子串匹配会 miss → 订单意图绕过规则层、掉进 LLM 裸奔。
    # 原始 user_msg 原样流转给 LLM/RAG/trace（不清洗，防丢信息/篡改语义）。
    compact = re.sub(r"\s+", "", user_msg)

    # 转人工：强意图词硬触发，排除否定句。
    # 刻意排在写意图排除之前——「转人工」是用户显式的升级信号，硬触发的存在意义就是
    # 不依赖软 prompt；「退款不成，我要转人工」该照常转人工，不因为带「退款」二字就降级给 LLM。
    if any(w in compact for w in _HUMAN_WORDS) and not any(w in compact for w in _NEGATE_WORDS):
        return ("tool", "transfer_to_human", {"problem": user_msg})

    # 写操作意图（退款）排除：规则层不碰写操作，直接交 LLM 走 ReAct。
    # 必须放在订单号分支之前——否则「我要退款订单 X」会被 _ORDER_WORDS 的「订单」抢走。
    if any(w in compact for w in _WRITE_INTENT_WORDS):
        return None

    order_id = _extract_order_id(compact)
    if order_id:
        # 物流：轨迹词
        if any(w in compact for w in _LOGISTICS_WORDS):
            return ("tool", "search_logistics", {"order_id": order_id})
        # 订单状态：状态词
        if any(w in compact for w in _ORDER_WORDS):
            return ("tool", "search_orders", {"order_id": order_id})

    # 退货政策：明确政策词（query 传用户原话，get_return_policy 走政策 RAG 检索）
    if any(w in compact for w in _POLICY_WORDS):
        return ("tool", "get_return_policy", {"query": user_msg})

    # 售后意图（退货/换货流程）：交 LLM 走 ReAct（退货是流程引导，非单一工具能完成；
    # 且要抢在浏览词之前拦截，防「我要退货，随便看看」被劫持成列分类概览）。
    if any(w in compact for w in _AFTERSALE_WORDS):
        return None

    # 总结意图（想对比多款）→ 反问澄清
    if any(w in compact for w in _SUMMARIZE_WORDS):
        return ("summarize", None, None)

    # 浏览意图（无明确目标）→ 列分类概览；带明确类别（「有什么推荐的猫粮」）是检索该类别，回落 LLM
    if any(w in compact for w in _BROWSE_WORDS) and not _has_category(compact):
        return ("browse", None, None)

    return None
