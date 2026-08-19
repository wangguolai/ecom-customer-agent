# -*- coding: utf-8 -*-
"""工具定义 —— 5 个工具的 Function Calling schema + mock 数据 + 执行函数

工具列表（来自 REQUIREMENTS.md）：
  search_orders     订单查询（只读）
  search_logistics  物流追踪（只读）
  check_stock       库存查询（只读，含价格）
  get_return_policy 退货政策（只读）
  transfer_to_human 转人工（写，demo 阶段无权限开关）
"""

import sys
import os
import uuid

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════
# 工具 schema（Function Calling 格式，LLM 靠这些文本决定调用哪个工具）
# ═══════════════════════════════════════════════════════════════

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "检索商品知识库，查询商品信息（适用对象、成分、规格、价格、特点等）。当用户咨询商品本身（如「XX 适合我家狗吗」「XX 的成分是什么」「有没有适合肠胃敏感的粮」）时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户的商品咨询问题或关键词"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_orders",
            "description": "根据订单号查询订单状态（是否已发货、已送达等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号，如 20240818001"}
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_logistics",
            "description": "根据订单号查询物流轨迹（包裹到哪了）",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号，如 20240818001"}
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_stock",
            "description": "查询某个商品的库存和价格",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string", "description": "商品名，如「幼犬成长粮」"}
                },
                "required": ["product_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_return_policy",
            "description": "获取退换货政策",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_human",
            "description": "把问题转给人工客服，生成工单",
            "parameters": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string", "description": "用户问题的描述"}
                },
                "required": ["problem"],
            },
        },
    },
]


# ═══════════════════════════════════════════════════════════════
# mock 数据（订单/物流/库存没有真实数据源，先用内存 mock）
# ═══════════════════════════════════════════════════════════════

MOCK_ORDERS = {
    "20240818001": {"状态": "已发货", "下单时间": "2024-08-18 10:23", "商品": "幼犬成长粮（贝乐牌）1.5kg", "金额": "¥89"},
    "20240817002": {"状态": "待付款", "下单时间": "2024-08-17 15:40", "商品": "豆腐猫砂 6L", "金额": "¥32"},
    "20240816003": {"状态": "已完成", "下单时间": "2024-08-16 09:12", "商品": "猫薄荷玩具球", "金额": "¥19"},
}

MOCK_LOGISTICS = {
    "20240818001": [
        {"时间": "2024-08-18 12:00", "地点": "杭州分拨中心", "状态": "已揽收"},
        {"时间": "2024-08-18 18:30", "地点": "杭州转运中心", "状态": "运输中"},
        {"时间": "2024-08-19 08:00", "地点": "上海转运中心", "状态": "派送中"},
    ],
}

MOCK_STOCK = {
    "幼犬成长粮": {"库存": 120, "价格": "¥89 / ¥219"},
    "成犬均衡粮": {"库存": 80, "价格": "¥119 / ¥399"},
    "猫薄荷玩具球": {"库存": 45, "价格": "¥19"},
    "豆腐猫砂": {"库存": 0, "价格": "¥32"},
}

RETURN_POLICY = (
    "退换货政策：\n"
    "1. 7 天无理由退货（商品未拆封、不影响二次销售）；\n"
    "2. 质量问题 15 天内可退换；\n"
    "3. 食品类（粮/零食）拆封后不支持无理由退换；\n"
    "4. 退货需提供订单号，退款 1-3 个工作日到账。"
)


# ═══════════════════════════════════════════════════════════════
# 工具执行函数（LLM 只输出调用意图，真正执行的是这里的代码）
# ═══════════════════════════════════════════════════════════════

def search_orders(order_id: str) -> str:
    """订单查询"""
    order = MOCK_ORDERS.get(order_id)
    if order is None:
        return f"未查到订单号 {order_id}，请核对订单号。"
    return f"订单 {order_id}：状态={order['状态']}，商品={order['商品']}，金额={order['金额']}，下单时间={order['下单时间']}"


def search_logistics(order_id: str) -> str:
    """物流追踪"""
    traces = MOCK_LOGISTICS.get(order_id)
    if traces is None:
        return f"未查到订单号 {order_id} 的物流信息。"
    lines = [f"{t['时间']} {t['地点']} {t['状态']}" for t in traces]
    return "物流轨迹：\n" + "\n".join(lines)


def check_stock(product_name: str) -> str:
    """库存查询（含价格）"""
    item = MOCK_STOCK.get(product_name)
    if item is None:
        return f"未查到商品「{product_name}」，请确认商品名。"
    if item["库存"] <= 0:
        return f"「{product_name}」暂时缺货，价格 {item['价格']}。"
    return f"「{product_name}」有货，库存 {item['库存']} 件，价格 {item['价格']}。"


def get_return_policy() -> str:
    """退货政策"""
    return RETURN_POLICY


def transfer_to_human(problem: str) -> str:
    """转人工（生成工单）"""
    ticket_id = f"TK{uuid.uuid4().hex[:8].upper()}"
    return f"已为您创建工单 {ticket_id}，人工客服将尽快联系您。问题：{problem}"


_hybrid_retriever = None


def _get_hybrid_retriever():
    """懒加载单例——BM25 索引 + embedding 模型只建一次"""
    global _hybrid_retriever
    if _hybrid_retriever is None:
        from src.infra.hybrid_retriever import HybridRetriever
        _hybrid_retriever = HybridRetriever()
    return _hybrid_retriever


# 类别关键词表（意图识别 → category 映射；识别不出返回 None，不过滤保召回）
CATEGORY_KEYWORDS = {
    "猫粮": ["猫粮", "幼猫", "成猫", "奶糕", "美毛", "泌尿", "牛磺酸", "老年猫", "布偶", "英短"],
    "狗粮": ["狗粮", "幼犬", "成犬", "骨骼", "钙磷", "老年犬", "大型犬", "小型犬"],
    "猫砂": ["猫砂", "结团", "除臭", "膨润土", "松木", "水晶"],
    "零食": ["零食", "冻干", "磨牙棒", "洁齿", "化毛膏", "猫条", "训练饼干"],
    "玩具": ["玩具", "猫薄荷", "橡胶球", "飞盘", "逗猫棒", "猫抓板", "漏食球"],
    "用品": ["饮水机", "航空箱", "梳毛", "牵引绳", "猫窝", "狗窝", "猫爬架", "食盆"],
}


def detect_category(query: str):
    """从 query 提取明确类别；多类别并列或识别不出返回 None（不过滤，保召回）"""
    best_cat = None
    best_count = 0
    tie = False
    for cat, words in CATEGORY_KEYWORDS.items():
        count = sum(1 for w in words if w in query)
        if count > best_count:
            best_cat = cat
            best_count = count
            tie = False
        elif count == best_count and count > 0:
            tie = True
    # 并列（多个类别同样命中数）或没命中 → 不明确，不过滤
    if tie or best_count == 0:
        return None
    return best_cat


def search_products(query: str, top_k: int = 3) -> str:
    """商品知识库检索（RAG）——混合检索 + category 预过滤（锁类别优先）"""
    category = detect_category(query)
    results = _get_hybrid_retriever().search(query, top_k=top_k, category=category)

    if not results:
        return "知识库检索无高置信度匹配。请如实告知用户暂未找到相关信息、可建议联系人工客服，不要编造商品信息。"

    parts = []
    for i, (cid, score, text) in enumerate(results, 1):
        title = text.split("\n")[0].strip("# ").strip() if text else ""
        parts.append(f"[{i}] {title}\n{text}")
    return "\n\n".join(parts)


# 工具名白名单映射（幻觉工具校验 + 派发执行）
TOOL_MAP = {
    "search_products": search_products,
    "search_orders": search_orders,
    "search_logistics": search_logistics,
    "check_stock": check_stock,
    "get_return_policy": get_return_policy,
    "transfer_to_human": transfer_to_human,
}
