# -*- coding: utf-8 -*-
"""意图路由层：规则优先，LLM 兜底

在 ReAct 循环之前，用正则/关键字识别「高置信的常用命令意图」，直接路由到对应工具，
绕过 LLM 决策（省一次带 tools 的 LLM 调用）。

设计原则（设计取舍）：
  - 判断交规则、表达交模型：规则只负责「意图 + 参数」，话术仍由 LLM 生成。
  - 三级漏斗（产品视角）：菜单/规则（常用命令，零成本）→ 假人工/agent（长尾，LLM 兜底）
    → 真人（明确转人工才出，且要 check_online 查在线）。
  - 只路由「自包含、高置信」的意图；多轮指代、模糊意图、语义检索、写操作（退款）走 LLM。

能规则路由的（自包含、参数可精确提取）：
  search_orders / search_logistics（订单号强模式 + 关键词区分）
  get_return_policy（明确政策词）
  transfer_to_human（投诉/转人工强意图词，硬触发解决「软 prompt 不可靠」）
不能规则路由的（走 LLM）：
  search_products（语义检索）/ check_stock（两步链路 product_id）/ refund_order（写操作 + 复杂参数）
"""

import re

from src.derived.categories import build_category_keywords

# 订单号模式：11 位、20 开头（demo 种子数据 20240818001 这种）。
# 锚定 20 开头 + \b 边界，防 11 位手机号（13x/15x/18x 开头）误匹配——宁可漏不可错。
_ORDER_ID_RE = re.compile(r"\b20\d{9}\b")

# 转人工硬触发词（投诉/人工求助）
_HUMAN_WORDS = ("投诉", "转人工", "人工客服", "找人工")
# 否定词：用户「拒绝转人工」不该被反向触发写工具（方向性错误，如「不用转人工」「别转人工」）
_NEGATE_WORDS = ("不用", "不要", "别", "不想", "能不", "无需")

# 物流轨迹词（区分 logistics vs orders）
_LOGISTICS_WORDS = ("到哪", "物流", "快递", "包裹", "轨迹")
# 订单状态词
_ORDER_WORDS = ("状态", "订单", "发货")

# 退货政策词（明确政策词；「能退吗/我要退货」这类模糊的走 LLM，避免和退款意图混淆）
_POLICY_WORDS = ("退货政策", "退换货政策", "七天无理由", "无理由退货", "退货流程", "退货条件", "退货运费")

# 总结意图词（想对比多款 → 反问澄清）；只收高置信词
_SUMMARIZE_WORDS = ("哪个好", "哪款好", "哪种好", "对比", "区别", "差别", "比较", "优缺点", "哪个更好", "哪款适合")

# 浏览意图词（无明确目标 → 列分类概览）；只收高置信词，不含「看看」「有什么」这类会误判检索的
_BROWSE_WORDS = ("随便看看", "随便逛逛", "逛逛", "有什么推荐", "推荐一下", "买点啥", "买啥", "买什么")

# 类别触发词（类别 → 触发词列表），用于「浏览 vs 检索」边界判断：
# 「有什么推荐的猫粮」含类别词「猫粮」→ 是检索该类别，不是浏览；「随便看看」无类别词 → 真浏览。
_CATEGORY_WORDS = build_category_keywords()


def _has_category(query: str) -> bool:
    """query 是否命中任何类别触发词（有明确类别诉求）"""
    return any(w in query for words in _CATEGORY_WORDS.values() for w in words)


def _extract_order_id(text: str):
    m = _ORDER_ID_RE.search(text)
    return m.group(0) if m else None


def route_by_rule(user_msg: str):
    """规则路由：返回 (kind, tool_name, args) 或 None（None = 交给 LLM）。

    kind ∈ {"tool", "browse", "summarize"}：
      - tool：高置信工具意图（订单/物流/政策/转人工），直接执行工具 + LLM 生成话术
      - browse：浏览意图（无明确目标），列分类概览
      - summarize：总结意图（想对比多款），反问澄清
    只处理「自包含、高置信」的意图。宁可漏（走 LLM）不可错（错误路由）。
    """
    # 转人工：强意图词硬触发，排除否定句
    if any(w in user_msg for w in _HUMAN_WORDS) and not any(w in user_msg for w in _NEGATE_WORDS):
        return ("tool", "transfer_to_human", {"problem": user_msg})

    order_id = _extract_order_id(user_msg)
    if order_id:
        # 物流：轨迹词
        if any(w in user_msg for w in _LOGISTICS_WORDS):
            return ("tool", "search_logistics", {"order_id": order_id})
        # 订单状态：状态词
        if any(w in user_msg for w in _ORDER_WORDS):
            return ("tool", "search_orders", {"order_id": order_id})

    # 退货政策：明确政策词
    if any(w in user_msg for w in _POLICY_WORDS):
        return ("tool", "get_return_policy", {})

    # 总结意图（想对比多款）→ 反问澄清
    if any(w in user_msg for w in _SUMMARIZE_WORDS):
        return ("summarize", None, None)

    # 浏览意图（无明确目标）→ 列分类概览；带明确类别（「有什么推荐的猫粮」）是检索该类别，回落 LLM
    if any(w in user_msg for w in _BROWSE_WORDS) and not _has_category(user_msg):
        return ("browse", None, None)

    return None
