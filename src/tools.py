# -*- coding: utf-8 -*-
"""工具定义 —— 7 个工具的 Function Calling schema + 执行函数

工具列表（来自 REQUIREMENTS.md）：
  search_products   商品知识库检索（只读，RAG）
  search_orders     订单查询（只读）
  search_logistics  物流追踪（只读）
  check_stock       库存查询（只读）
  get_return_policy 退货政策（只读）
  refund_order      退款申请（写，待人工审批）
  transfer_to_human 转人工（写，demo 阶段无权限开关）
"""

import sys
import os
import json
import uuid
import asyncio
import threading
from urllib.parse import quote

import httpx

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.circuit_breaker import CircuitBreaker


# ═══════════════════════════════════════════════════════════════
# 工具 schema（Function Calling 格式，LLM 靠这些文本决定调用哪个工具）
# ═══════════════════════════════════════════════════════════════

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "检索商品知识库，查询商品静态信息（适用对象、成分、规格、特点等），并返回商品的 product_id。当用户咨询商品本身（如「XX 适合我家狗吗」「XX 的成分是什么」「有没有适合肠胃敏感的粮」）时调用。查价格/库存用 check_stock（传这里返回的 product_id）。",
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
            "description": "查询某个商品的实时价格和库存（是否有货、库存量、当前价格）。product_id 从 search_products 的检索结果里获取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "商品 ID，如 P001（从 search_products 检索结果的 product_id 字段获取）"}
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_return_policy",
            "description": "获取退换货政策（从政策知识库检索命中的政策块 + 整段政策参考）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户的退货政策咨询问题"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_online",
            "description": "查询人工客服当前是否在线（是否有人工客服值班）",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refund_order",
            "description": "为订单申请退款（写操作：只生成待人工审批的工单，不直接退款。只需订单号，退款金额由系统按订单金额全额退，不要指定金额）",
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
# 后端未启动/超时 → 工具返回友好错误，不静默降级（让失败可见，可讲「真实工具的失败处理」）。
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
REQUEST_TIMEOUT = 2.0  # 秒

# 熔断器（进程级单例）：demo 单后端一个全局实例；生产按下游服务粒度分（订单/物流/库存各一个）。
# 熔断打开时快速失败（不真调后端），保护自己不被挂掉的后端拖垮，同时给用户降级话术。
_breaker = CircuitBreaker(fail_threshold=5, cooldown=30.0)

# HTTP 客户端（模块级单例，复用连接池）：不每次请求新建 AsyncClient，TCP 连接复用（keep-alive）。
# httpx 懒创建连接池，import 时不在事件循环内也安全（连接在首次 await 时建立）。
_client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)


async def _http_request(method, url, json=None):
    """统一 HTTP 调用（带熔断）。返回 (resp, error)：
    resp: httpx.Response（error=None 时，2xx/4xx 的响应）或 None
    error: None 或 "breaker_open" / "rate_limited" / "timeout" / "5xx" / "bad_response"

    熔断记账（失败判定边界）：
      breaker_open：熔断打开，不调下游，直接快速失败
      rate_limited：429 限流（后端健康，不算熔断失败，单独话术防 LLM 重试浪费轮次）
      timeout：TransportError（Connect/Timeout/Read/RemoteProtocol 等）→ record_failure
      5xx：后端内部错（如 MySQL 挂）→ record_failure
      bad_response：200 但 JSON 解析失败（坑 7：200 垃圾响应）→ record_failure
      4xx（除 429）：业务正常响应（订单不存在/参数非法/409）→ record_success（重置连续失败计数）
    """
    if not _breaker.allow():
        return None, "breaker_open"
    try:
        if method == "GET":
            resp = await _client.get(url)
        else:
            resp = await _client.post(url, json=json)
    except httpx.TransportError:
        # TransportError 覆盖 ConnectError/TimeoutException/ReadError/WriteError/
        # RemoteProtocolError/CloseError——后端中途挂（连接断开）也走熔断失败。
        # 只捕 ConnectError+TimeoutException 会漏 RemoteProtocolError，穿透炸 agent 循环。
        _breaker.record_failure()
        return None, "timeout"
    if resp.status_code >= 500:
        _breaker.record_failure()
        return None, "5xx"
    if resp.status_code == 429:
        _breaker.record_success()  # 429 后端健康（被限流是自己的错），不算熔断失败
        return None, "rate_limited"
    if resp.status_code >= 400:
        _breaker.record_success()  # 4xx 后端健康，重置连续失败计数
        return resp, None
    # 2xx：校验响应格式（坑 7），垃圾响应算熔断失败
    # 注意：这里只能用 ValueError，不能用 json.JSONDecodeError——本函数参数名 json 遮蔽了
    # 模块级 import json（json 参数默认 None，except 里 json.JSONDecodeError 会 None 属性报错）。
    # JSONDecodeError 是 ValueError 子类，except ValueError 就能捕获。此坑故障注入 dirty 才暴露。
    try:
        resp.json()
    except ValueError:
        _breaker.record_failure()
        return None, "bad_response"
    _breaker.record_success()
    return resp, None

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
    resp, err = await _http_request("GET", f"{BACKEND_URL}/orders/{order_id}")
    if err:
        if err == "breaker_open":
            return "服务暂不可用（当前熔断中，请稍后重试或转人工）。"
        if err == "rate_limited":
            return "请求过于频繁，请稍后再试。"
        return "订单查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到订单号 {order_id}，请核对订单号。"
    if resp.status_code != 200:
        return f"订单查询失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    # 输出校验（格式级）：字段类型/非空。枚举级（status 值）透传——业务枚举会扩展（如「已退款」），
    # 未知值如实告诉用户，不降级「数据异常」（降级会掩盖正常数据）。格式级异常（脏数据/缺字段）才拦。
    status = d.get("status")
    if not isinstance(status, str) or not status.strip():
        return "订单数据异常，请联系人工客服。"
    return f"订单 {d.get('order_id', '未知')}：状态={status}，商品={d.get('product', '未知')}，金额={d.get('amount', '未知')}，下单时间={d.get('created_at', '未知')}"


async def search_logistics(order_id: str) -> str:
    """物流追踪（真实后端接口）"""
    resp, err = await _http_request("GET", f"{BACKEND_URL}/logistics/{order_id}")
    if err:
        if err == "breaker_open":
            return "服务暂不可用（当前熔断中，请稍后重试或转人工）。"
        if err == "rate_limited":
            return "请求过于频繁，请稍后再试。"
        return "物流查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到订单号 {order_id} 的物流信息。"
    if resp.status_code != 200:
        return f"物流查询失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    # 输出校验（格式级）：traces 必须是 list 且每项含 time/location/status 三字段。
    # 否则（如故障注入 empty 返回 {}）d["traces"] 会 KeyError，被 agent 误捕成「参数不匹配」而非「数据异常」。
    traces = d.get("traces")
    if not isinstance(traces, list) or any(not isinstance(t, dict) or not all(k in t for k in ("time", "location", "status")) for t in traces):
        return "物流数据异常，请联系人工客服。"
    lines = [f"{t['time']} {t['location']} {t['status']}" for t in traces]
    return "物流轨迹：\n" + "\n".join(lines)


async def check_stock(product_id: str) -> str:
    """实时价格 + 库存查询（真实后端接口）。动态数据（价格/库存）走工具实时查，不进向量库（数据分治）。"""
    resp, err = await _http_request("GET", f"{BACKEND_URL}/products/{quote(product_id)}")
    if err:
        if err == "breaker_open":
            return "服务暂不可用（当前熔断中，请稍后重试或转人工）。"
        if err == "rate_limited":
            return "请求过于频繁，请稍后再试。"
        return "价格/库存查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到商品 {product_id}。"
    if resp.status_code != 200:
        return f"价格/库存查询失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    # 输出校验（格式级）：qty 是非负 int（排除 bool，bool 是 int 子类）、price 是非负数字。
    # qty=0 是合法缺货（豆腐猫砂），不误伤；格式级异常（负数/非数字/脏数据）降级，不把脏数据回灌 LLM。
    qty = d.get("qty")
    price = d.get("price")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
        return "商品数据异常，请联系人工客服。"
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
        return "商品数据异常，请联系人工客服。"
    name = d.get("name", "未知商品")
    if qty <= 0:
        return f"「{name}」暂时缺货（价格 ¥{price:g}）。"
    return f"「{name}」有货，库存 {qty} 件，价格 ¥{price:g}。"


def _get_return_policy_sync(query: str) -> str:
    """get_return_policy 的同步实现。embedding + rerank 是 CPU/GPU 密集，丢线程池跑（to_thread），不阻塞事件循环。

    政策块缺失（BM25 索引里无 kb_type="policy"，说明没入库）→ 打印警告 + 整段兜底，不静默退化。
    检索双低/空 → 整段兜底。命中 → 拼「命中的政策块 + 整段 RETURN_POLICY 参考」，
    末尾附整段是补点 2：政策 4 条语义高度相近，「只返回精准单块」挡不住单高命中错块，附整段让 LLM 有完整上下文纠偏。
    """
    retriever = _get_hybrid_retriever()
    if not retriever.has_kb_type("policy"):
        print("⚠️ 政策块未入库，请先跑 python -m src.refresh")
        return RETURN_POLICY
    label, results = retriever.search(query, kb_type="policy")
    if label == "双低" or not results:
        return RETURN_POLICY
    parts = []
    for i, (cid, score, text, title, product_id) in enumerate(results, 1):
        parts.append(f"[{i}] {title}\n{text}")
    hits_text = "\n\n".join(parts)
    return f"[来源: 退货政策]\n{hits_text}\n\n{RETURN_POLICY}"


async def get_return_policy(query: str = None) -> str:
    """退货政策（政策知识库 RAG）——检索命中的政策块 + 整段政策参考；query 缺失时整段兜底"""
    if not query:
        return RETURN_POLICY
    return await asyncio.to_thread(_get_return_policy_sync, query)


async def check_online() -> str:
    """查询人工客服是否在线（动态数据走接口查，数据分治）"""
    resp, err = await _http_request("GET", f"{BACKEND_URL}/online")
    if err:
        if err == "breaker_open":
            return "客服在线状态查询暂不可用（熔断中），请稍后重试。"
        if err == "rate_limited":
            return "请求过于频繁，请稍后再试。"
        return "客服在线状态查询暂不可用（后端未启动或超时）。"
    if resp.status_code != 200:
        return "客服在线状态查询失败，请稍后重试。"
    d = resp.json()
    return "人工客服当前在线。" if d.get("online") else "人工客服当前不在线（工作时间 9:00-18:00）。"


async def transfer_to_human(problem: str) -> str:
    """转人工（生成工单 + 查在线状态）。

    转人工前必查在线（代码层保证，不靠 LLM 决策）：在线状态是动态数据走接口查（数据分治）。
    在线 → 生成工单「已转接人工」；不在线 → 生成工单（记录问题）+ 安抚话术，不声称「已转接」。
    /online 挂掉（后端不可用/熔断/超时）→ 降级生成工单 + 保守话术（不声称已转接），
    转人工不因查询失败而拒绝——工单生成是本地逻辑，不依赖后端。
    """
    ticket_id = f"TK{uuid.uuid4().hex[:8].upper()}"
    resp, err = await _http_request("GET", f"{BACKEND_URL}/online")
    online = False
    if err is None and resp.status_code == 200:
        online = bool(resp.json().get("online"))
    if online:
        return f"已为您转接人工客服，工单 {ticket_id}，人工将尽快联系您。问题：{problem}"
    # 不在线（或在线状态查询失败）→ 生成工单（记录问题）+ 安抚，不声称已转接
    return f"已记录您的问题（工单 {ticket_id}），当前人工客服不在线，工作时间（9:00-18:00）将尽快处理。问题：{problem}"


# 写工具集合（代码级标记：写操作要过权限门槛，读工具随便调）
# Prompt Injection 防线之一：读/写分离，写工具的行为在代码层被约束，不随 LLM 意图走
WRITE_TOOLS = {"refund_order", "transfer_to_human"}


async def refund_order(order_id: str) -> str:
    """退款（写工具，代码级防御：只提交「待人工审批」工单，不直接退款）。

    金额下沉：LLM 只传 order_id，退款金额由后端查订单得出（全额退款），LLM 无权指定金额——
    防 Prompt Injection 诱导 LLM 填 0/负数/超额（数据分治：能结构化查到的参数不让 LLM 填）。

    MQ 异步化后：后端 /refund 返回「已受理」+ refund_amount（订单金额），工单由消费者异步生成。
    按响应里 ticket_id 是否为空分两套话术——降级回退路径带工单号，异步路径带受理号。
    话术金额用响应 refund_amount（后端权威值），不引用任何局部 amount。
    """
    resp, err = await _http_request("POST", f"{BACKEND_URL}/refund", json={"order_id": order_id})
    if err:
        if err in ("breaker_open", "rate_limited"):
            # 请求确定没到达后端（熔断拦截 / 被限流），未产生副作用，可安全重试
            return "退款服务暂不可用（请稍后重试或转人工）。"
        # timeout / 5xx / bad_response：请求可能已到达后端并被处理，只是应答没回来（应答丢包）。
        # 状态失步：不能回「失败」误导用户以为退款失败，回「处理中/未确认」+ 禁止重复提交。
        # 接受弱一致，最终一致靠外部通知（订单查询 / 人工核实）。
        return "退款申请已提交，但处理结果未确认（服务响应异常）。系统可能已受理，请勿重复提交；稍后可查询订单状态确认，或转人工核实。"
    if resp.status_code == 404:
        return f"未查到订单 {order_id}，无法退款。"
    if resp.status_code == 400:
        return f"退款请求被拒绝：{resp.json().get('detail', '参数非法')}"
    if resp.status_code != 200:
        return f"退款失败（状态码 {resp.status_code}），请稍后重试。"
    d = resp.json()
    # 输出校验（格式级）：refund_amount 必须是数字（排除 bool）；ticket_id / message_id 至少有一个。
    # 否则（如故障注入 empty 返回 {}）d["message_id"] 会 KeyError，被 agent 误捕成「参数不匹配」而非「数据异常」。
    refund_amount = d.get("refund_amount")
    if isinstance(refund_amount, bool) or not isinstance(refund_amount, (int, float)):
        return "退款数据异常，请联系人工客服。"
    if d.get("ticket_id"):
        # 降级回退路径（MQ 不可用，同步落库）：带工单号
        return f"退款申请已受理，工单已生成：{d['ticket_id']}，金额 ¥{refund_amount:g}，状态：待人工审批。真正的退款需人工审批后执行。"
    if not d.get("message_id"):
        return "退款数据异常，请联系人工客服。"
    # 异步路径：已受理，工单处理中（提金额，用户申请退款关心退多少）
    return f"退款申请已受理（受理号 {d['message_id']}），将按订单金额 ¥{refund_amount:g} 全额退款，工单处理中。真正的退款需人工审批后执行。"


_hybrid_retriever = None
_hybrid_retriever_lock = threading.Lock()


def _get_hybrid_retriever():
    """懒加载单例——BM25 索引 + embedding 模型只建一次。

    线程安全（asyncio 改造后踩的坑）：工具经 asyncio.gather 并发执行、落到 to_thread 线程池，
    多个线程可能同时首次调用、并发初始化单例，Qdrant 本地模式文件锁会 AlreadyLocked → RuntimeError。
    加锁 + 双重检查：锁外快速路径（已初始化后零开销），锁内再判，保证只初始化一次。
    """
    global _hybrid_retriever
    if _hybrid_retriever is None:
        with _hybrid_retriever_lock:
            if _hybrid_retriever is None:
                from src.infra.hybrid_retriever import HybridRetriever
                _hybrid_retriever = HybridRetriever()
    return _hybrid_retriever


# 类别触发词表（意图识别 → category 映射）从派生层生成（读映射表 category_synonyms.md）
from src.derived.categories import build_category_keywords
CATEGORY_KEYWORDS = build_category_keywords()


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
    "单高冲突": "检索结果存在冲突，两路候选都列出让用户选择确认，不要偏袒某一项（精确词/语义都可能是对的）。",
}


def _search_products_sync(query: str, top_k: int) -> str:
    """search_products 的同步实现。embedding + rerank 是 CPU/GPU 密集，丢线程池跑（to_thread），不阻塞事件循环。"""
    category = detect_category(query)
    label, results = _get_hybrid_retriever().search(query, top_k=top_k, category=category, kb_type="product")

    if label == "双低":
        return "知识库检索无高置信度匹配。请如实告知用户暂未找到相关信息、可建议联系人工客服，不要编造商品信息。"

    parts = []
    for i, (cid, score, text, title, product_id) in enumerate(results, 1):
        parts.append(f"[{i}] {title}（product_id: {product_id}）\n{text}")

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
    "check_online": check_online,
    "transfer_to_human": transfer_to_human,
    "refund_order": refund_order,
}
