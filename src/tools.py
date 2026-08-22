# -*- coding: utf-8 -*-
"""工具定义 —— 7 个工具的 Function Calling schema + 执行函数

工具列表（来自 REQUIREMENTS.md）：
  search_products   商品知识库检索（只读，RAG）
  search_orders     订单查询（只读）
  search_logistics  物流追踪（只读）
  check_stock       库存查询（只读，含价格）
  get_return_policy 退货政策（只读）
  refund_order      退款申请（写，待人工审批）
  transfer_to_human 转人工（写，demo 阶段无权限开关）
"""

import sys
import os
import uuid
import asyncio
from urllib.parse import quote

import httpx

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
            "name": "refund_order",
            "description": "为订单申请退款（写操作：只生成待人工审批的工单，不直接退款，需提供订单号和退款金额）",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号，如 20240818001"},
                    "amount": {"type": "number", "description": "退款金额（元）"}
                },
                "required": ["order_id", "amount"],
            },
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

# 订单/物流/库存已迁到后端（src/backend/main.py 的 SQLite），工具走 HTTP 查询，不再是内存 mock。
# 后端未启动/超时 → 工具返回友好错误，不静默降级（让失败可见，可以说明「真实工具的失败处理」）。
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
REQUEST_TIMEOUT = 2.0  # 秒

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

async def search_orders(order_id: str) -> str:
    """订单查询（真实后端接口）"""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(f"{BACKEND_URL}/orders/{order_id}")
    except (httpx.ConnectError, httpx.TimeoutException):
        return "订单查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到订单号 {order_id}，请核对订单号。"
    if resp.status_code != 200:
        return f"订单查询失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    return f"订单 {d['order_id']}：状态={d['status']}，商品={d['product']}，金额={d['amount']}，下单时间={d['created_at']}"


async def search_logistics(order_id: str) -> str:
    """物流追踪（真实后端接口）"""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(f"{BACKEND_URL}/logistics/{order_id}")
    except (httpx.ConnectError, httpx.TimeoutException):
        return "物流查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到订单号 {order_id} 的物流信息。"
    if resp.status_code != 200:
        return f"物流查询失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    lines = [f"{t['time']} {t['location']} {t['status']}" for t in d["traces"]]
    return "物流轨迹：\n" + "\n".join(lines)


async def check_stock(product_name: str) -> str:
    """库存查询（真实后端接口，含价格）"""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(f"{BACKEND_URL}/stock/{quote(product_name)}")
    except (httpx.ConnectError, httpx.TimeoutException):
        return "库存查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到商品「{product_name}」，请确认商品名。"
    if resp.status_code != 200:
        return f"库存查询失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    if d["qty"] <= 0:
        return f"「{product_name}」暂时缺货，价格 {d['price']}。"
    return f"「{product_name}」有货，库存 {d['qty']} 件，价格 {d['price']}。"


async def get_return_policy() -> str:
    """退货政策"""
    return RETURN_POLICY


async def transfer_to_human(problem: str) -> str:
    """转人工（生成工单）"""
    ticket_id = f"TK{uuid.uuid4().hex[:8].upper()}"
    return f"已为您创建工单 {ticket_id}，人工客服将尽快联系您。问题：{problem}"


# 写工具集合（代码级标记：写操作要过权限门槛，读工具随便调）
# Prompt Injection 防线之一：读/写分离，写工具的行为在代码层被约束，不随 LLM 意图走
WRITE_TOOLS = {"refund_order", "transfer_to_human"}


async def refund_order(order_id: str, amount: float) -> str:
    """退款（写工具，代码级防御：只提交「待人工审批」工单，不直接退款）。

    关键：LLM 只有「建议权」（提交退款申请），没有「执行权」（真正退款在人工审批）。
    Prompt Injection 诱导 LLM 调 refund，最多生成一个待审批工单，金额非法会被后端参数校验拦下。
    """
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{BACKEND_URL}/refund",
                json={"order_id": order_id, "amount": amount},
            )
    except (httpx.ConnectError, httpx.TimeoutException):
        return "退款服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到订单 {order_id}，无法退款。"
    if resp.status_code == 400:
        return f"退款请求被拒绝：{resp.json().get('detail', '参数非法')}"
    if resp.status_code != 200:
        return f"退款失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    if d.get("duplicate"):
        return f"退款工单已存在（{d['ticket_id']}），金额 ¥{d['amount']}，状态：{d['status']}。已去重，未重复创建。"
    return f"已生成退款工单 {d['ticket_id']}，订单 {d['order_id']}，金额 ¥{d['amount']}，状态：{d['status']}。真正的退款需人工审批后执行。"


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


# 策略映射层（硬编码规则）：四维置信度 label → 给 LLM 的话术提示
STRATEGY_HINTS = {
    "双高": "检索高置信度命中，可直接推荐给用户（确定语气）。",
    "单高一致": "检索中等置信度，用确认语气推荐（如「您是不是想要…」），并说明这是推测、可让用户确认。",
    "单高冲突": "检索结果存在冲突，列出候选让用户选择，优先推荐第 1 条（精确词匹配那一路）。",
}


def _search_products_sync(query: str, top_k: int) -> str:
    """search_products 的同步实现。embedding + rerank 是 CPU/GPU 密集，丢线程池跑（to_thread），不阻塞事件循环。"""
    category = detect_category(query)
    label, results = _get_hybrid_retriever().search(query, top_k=top_k, category=category)

    if label == "双低":
        return "知识库检索无高置信度匹配。请如实告知用户暂未找到相关信息、可建议联系人工客服，不要编造商品信息。"

    parts = []
    for i, (cid, score, text) in enumerate(results, 1):
        title = text.split("\n")[0].strip("# ").strip() if text else ""
        parts.append(f"[{i}] {title}\n{text}")

    # 策略提示（系统生成的受信任指令）+ 检索结果（外部数据），分开标注，不混进「数据/指令分离」的防御里
    return f"[检索策略：{label}]\n{STRATEGY_HINTS[label]}\n\n" + "\n\n".join(parts)


async def search_products(query: str, top_k: int = 3) -> str:
    """商品知识库检索（RAG）——混合检索 + category 预过滤 + 四维置信度策略映射"""
    return await asyncio.to_thread(_search_products_sync, query, top_k)


# 工具名白名单映射（幻觉工具校验 + 派发执行）
TOOL_MAP = {
    "search_products": search_products,
    "search_orders": search_orders,
    "search_logistics": search_logistics,
    "check_stock": check_stock,
    "get_return_policy": get_return_policy,
    "transfer_to_human": transfer_to_human,
    "refund_order": refund_order,
}
