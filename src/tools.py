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
from src.infra.egress import validate_backend_url
from src.config.prompts import RETURN_POLICY, INTERNAL_MSG_PREFIXES
from src.config.rules import WRITE_TOOLS
from src.config.settings import REQUEST_TIMEOUT


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
# 出网限制（架构级注入防御）：BACKEND_URL 来自环境变量，import 时就校验主机白名单。
# 不校验的话，改掉这个变量就能把 agent 的全部工具请求（含用户订单数据）导向攻击者服务器。
BACKEND_URL = validate_backend_url(os.environ.get("BACKEND_URL", "http://localhost:8000"))


def _sanitize(s) -> str:
    """边界清洗：外部输入里的孤立代理（U+D800-U+DFFF 非法码点）替换成 ?。

    为什么要清洗：孤立代理不是合法 Unicode，encode("utf-8") 会抛 UnicodeEncodeError。
    它一路都能炸——quote()（URL 编码）、embedding 模型 .encode()、re.fullmatch 后的
    HTTPException 回显——每一处都是雷。fuzz 实测抓到三个：auth 的 _ct_equal、后端 /refund
    的错误回显、工具层 quote()。统一在边界把非法码点降级成 ?，后面所有环节都安全。
    """
    if not isinstance(s, str):
        s = str(s)
    return s.encode("utf-8", "replace").decode("utf-8")

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
    except httpx.HTTPError:
        # 非传输类 httpx 异常，典型是 InvalidURL（URL 里有空格/控制字符）。
        # 它是 HTTPError 的直接子类、不是 TransportError 子类，上面那个 except 接不住，
        # 会一路穿透到 agent 的 ReAct 循环。路径参数已统一 quote()，正常路径不该再触发；
        # 这里是边界兜底——工具层绝不能把异常抛给调用方。
        # 不计熔断失败：请求根本没发出去，是我们自己构造的 URL 坏了，不是下游故障。
        return None, "bad_request"
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

# RETURN_POLICY（政策兜底文本）已移到 src/config/prompts.py —— 它是对用户说的话，属提示词层。


# ═══════════════════════════════════════════════════════════════
# 工具执行函数（LLM 只输出调用意图，真正执行的是这里的代码）
# ═══════════════════════════════════════════════════════════════

async def search_orders(order_id: str) -> str:
    """订单查询（真实后端接口）"""
    order_id = _sanitize(order_id)
    resp, err = await _http_request("GET", f"{BACKEND_URL}/orders/{quote(order_id, safe='')}")
    if err:
        if err == "breaker_open":
            return "服务暂不可用（当前熔断中，请稍后重试或转人工）。"
        if err == "rate_limited":
            return "请求过于频繁，请稍后再试。"
        return "订单查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到订单号 {order_id}，请核对订单号。"
    if resp.status_code != 200:
        # 不回显 HTTP 状态码：属于「系统内部机制」，SYSTEM_PROMPT 第 7 条明令不外泄。
        # **这里是实测到的真实泄露源**：评测 baseline 的「工具参数非法拦截」case，
        # 用户问「查订单abcdefg的物流」，后端对非法订单号返回 400，
        # agent 原话就是「系统返回查询失败（状态码 400）」——这条 case 一次都没调 check_stock，
        # 所以把收口只做在 check_stock 上是打偏了（曾因此漏堵这里）。
        return "订单查询暂时失败，请稍后重试或转人工。"
    d = resp.json()
    # 输出校验（格式级）：字段类型/非空。枚举级（status 值）透传——业务枚举会扩展（如「已退款」），
    # 未知值如实告诉用户，不降级「数据异常」（降级会掩盖正常数据）。格式级异常（脏数据/缺字段）才拦。
    status = d.get("status")
    if not isinstance(status, str) or not status.strip():
        return "订单数据异常，请联系人工客服。"
    return f"订单 {d.get('order_id', '未知')}：状态={status}，商品={d.get('product', '未知')}，金额={d.get('amount', '未知')}，下单时间={d.get('created_at', '未知')}"


async def search_logistics(order_id: str) -> str:
    """物流追踪（真实后端接口）"""
    order_id = _sanitize(order_id)
    resp, err = await _http_request("GET", f"{BACKEND_URL}/logistics/{quote(order_id, safe='')}")
    if err:
        if err == "breaker_open":
            return "服务暂不可用（当前熔断中，请稍后重试或转人工）。"
        if err == "rate_limited":
            return "请求过于频繁，请稍后再试。"
        return "物流查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        return f"未查到订单号 {order_id} 的物流信息。"
    if resp.status_code != 200:
        # 同 /orders：状态码不外泄（这里是实测泄露源之一）
        return "物流查询暂时失败，请稍后重试或转人工。"
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
    product_id = _sanitize(product_id)
    resp, err = await _http_request("GET", f"{BACKEND_URL}/products/{quote(product_id, safe='')}")
    if err:
        if err == "breaker_open":
            return "服务暂不可用（当前熔断中，请稍后重试或转人工）。"
        if err == "rate_limited":
            return "请求过于频繁，请稍后再试。"
        return "价格/库存查询服务暂不可用（后端未启动或超时），请稍后重试或转人工。"
    if resp.status_code == 404:
        # 不回显 product_id：它是内部字段，LLM 会原样转述给用户（实测发生过「英短专用粮（P016）」）。
        # 「未查到」保留——它确实是空返回，会让 agent 打 is_empty 标记，这是**正确**的语义。
        return "未查到该商品的库存信息。请确认商品名称，或换一个关键词重新查找。"
    if resp.status_code != 200:
        # 不回显 HTTP 状态码：属于「系统内部机制」，SYSTEM_PROMPT 第 7 条明令不外泄。
        return "价格/库存查询暂时失败，请稍后重试或转人工。"
    d = resp.json()
    # 输出校验（格式级）：qty 是非负 int（排除 bool，bool 是 int 子类）、price 是非负数字。
    # qty=0 是合法缺货（如 P009/P058/P146/P153），不误伤；格式级异常（负数/非数字/脏数据）降级，不把脏数据回灌 LLM。
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
    # 政策检索此前**从不记录 chunk_id** —— 而政策问答（如「15 天无理由还能用吗」）
    # 恰恰是最需要复现的一类：错答往往是「命中了错的块」而不是「没命中」，
    # 不记 chunk_id 就永远看不出它命中了哪一块。同样放在 return 之前（双低也记）。
    _record_retrievals(results)
    if label == "双低" or not results:
        return RETURN_POLICY
    parts = []
    for i, (cid, score, text, title, product_id, _image) in enumerate(results, 1):
        parts.append(f"[{i}] {title}\n{text}")
    hits_text = "\n\n".join(parts)
    return f"[来源: 退货政策]\n{hits_text}\n\n{RETURN_POLICY}"


async def get_return_policy(query: str = None) -> str:
    """退货政策（政策知识库 RAG）——检索命中的政策块 + 整段政策参考；query 缺失时整段兜底"""
    if not query:
        return RETURN_POLICY
    query = _sanitize(query)
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
    problem = _sanitize(problem)
    ticket_id = f"TK{uuid.uuid4().hex[:8].upper()}"
    resp, err = await _http_request("GET", f"{BACKEND_URL}/online")
    online = False
    if err is None and resp.status_code == 200:
        online = bool(resp.json().get("online"))
    if online:
        return f"已为您转接人工客服，工单 {ticket_id}，人工将尽快联系您。问题：{problem}"
    # 不在线（或在线状态查询失败）→ 生成工单（记录问题），**既不声称已转接、也不承诺跟进**。
    # ⚠️ 2026-09-18 修：原话术带「（工作时间）将尽快处理」——本地 demo 没有真人坐席，这句兑现不了，
    # 属「说了没发生的事」；而且 LLM 会把工具返回原文照搬进回答，改这里才拦得住。
    # 改为只陈述现状：工单已记录、当前无人接待、转接没成。要演示「已转接」就设 CS_ONLINE=true。
    return f"已记录您的问题，工单号 {ticket_id}。当前没有人工客服在线（工作时间 9:00-18:00），暂时无法为您转接。问题：{problem}"


# 写工具集合 WRITE_TOOLS 已移到 src/config/rules.py（那里标了「安全关键，改动需过 code review」）。


async def refund_order(order_id: str) -> str:
    """退款（写工具，代码级防御：只提交「待人工审批」工单，不直接退款）。

    金额下沉：LLM 只传 order_id，退款金额由后端查订单得出（全额退款），LLM 无权指定金额——
    防 Prompt Injection 诱导 LLM 填 0/负数/超额（数据分治：能结构化查到的参数不让 LLM 填）。

    MQ 异步化后：后端 /refund 返回「已受理」+ refund_amount（订单金额），工单由消费者异步生成。
    按响应里 ticket_id 是否为空分两套话术——降级回退路径带工单号，异步路径带受理号。
    话术金额用响应 refund_amount（后端权威值），不引用任何局部 amount。
    """
    order_id = _sanitize(order_id)
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
        # 同其他工具：状态码不外泄（同类泄露，顺手收口）
        return "退款失败，请稍后重试或转人工。"
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
from src.derived.categories import build_category_keywords, build_generic_keywords
CATEGORY_KEYWORDS = build_category_keywords()
# 泛义词（猫/狗/猫猫/狗狗）：**独立一张表**，只在具体词全落空时兜底。
# 合进 CATEGORY_KEYWORDS 会跟具体词抢分 —— 理由见 detect_category 的 docstring。
GENERIC_KEYWORDS = build_generic_keywords()


def find_recent_category(messages: list):
    """从消息列表里找**最近提到**的类别（从后往前扫）。用于**跨轮补参**。

    场景（2026-09-20 实测 bug）：用户第一轮说「我想知道猫猫吃什么」，第二轮只说
    「有没有别的品牌的」——第二句里**一个类别词都没有**，`detect_category` 返回 None
    → 不锁类别 → 全库检索 → **狗粮混进了猫粮的推荐里**。
    根因与「伪菜单掉 LLM」同源：规则/工具层只看当前这一句，看不到会话上下文。

    ⚠️ 也扫 assistant：用户从没说过类别（「有什么推荐」）时，助手回复里的类别是唯一线索。
    代价是「助手某一轮答偏了会把偏的类别带下去」——但取**最近**一条，用户的下一句话会覆盖它。

    ⚠️ **必须跳过内部注入块**：它们也是 `role="user"`（画像 / 路由引导 / 工具回填），
    但**不是用户说的话**。不跳的话实测会出两类静默错误：
      · 画像块里有「宠物：猫」→ 之后所有无类别追问都被锁成猫粮（用户压根没提过猫）；
      · 工具回填里有商品名（「中大型成犬…处方粮」）→ **查过一次订单就决定了下一轮锁狗粮**。
    登记表复用 `prompts.INTERNAL_MSG_PREFIXES`——`session_history()` 排除的是同一批，
    **同一份 messages 不能有两个出口两套语义**。
    """
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = str(m.get("content", ""))
        if any(content.startswith(p) for p in INTERNAL_MSG_PREFIXES):
            continue
        cat = detect_category(content)
        if cat:
            return cat
    return None


def _score_categories(table: dict, query: str):
    """命中词数最多者胜；**并列或全 0 → None**（不明确，不过滤，保召回）"""
    best_cat = None
    best_count = 0
    tie = False
    for cat, words in table.items():
        count = sum(1 for w in words if w in query)
        if count > best_count:
            best_cat, best_count, tie = cat, count, False
        elif count == best_count and count > 0:
            tie = True
    return None if (tie or best_count == 0) else best_cat


def detect_category(query: str):
    """从 query 提取明确类别。**两轮打分：具体词优先，泛义词兜底**（顺序不可换）。

    ⚠️ 为什么必须分两轮（2026-09-20 实测回归）：泛义词（猫/狗/猫猫/狗狗）加进主表后
    会跟具体词**抢分**——
      · 「洁齿骨，能让**狗狗**当饭吃吗」→ 零食(洁齿骨)=1 vs 狗粮(狗+狗狗)=2 → **狗粮赢**，
        而洁齿骨是零食类商品 → 预过滤把正确答案整个滤掉；
      · 「有没有**猫咪**吃的洁齿骨」→ 各 1 分 → **打平 → 不锁类别** → 退回全库检索。
    分两轮后：「泛义词只在具体词一个都没命中时才生效」，压不过具体词。
    """
    cat = _score_categories(CATEGORY_KEYWORDS, query)
    if cat:
        return cat
    return _score_categories(GENERIC_KEYWORDS, query)


# 策略映射层（硬编码规则）：四维置信度 label → 给 LLM 的话术提示
STRATEGY_HINTS = {
    "双高": "检索高置信度命中，可直接推荐给用户（确定语气）。",
    "单高一致": "检索中等置信度，用确认语气推荐（如「您是不是想要…」），并说明这是推测、可让用户确认。",
    "单高冲突": "检索结果存在冲突，两路候选都列出让用户选择确认，不要偏袒某一项（精确词/语义都可能是对的）。",
}


# 本轮对话检索命中的商品图（模块级收集器：search_products 命中即收集，agent 每轮对话结束后
# 由 pop_collected_images 读取并清空）。图片是「展示层数据」，不进 LLM 文本（LLM 只处理语义，
# 图片路径对它是噪声），由 agent 在流式输出前/后单独发给前端渲染。
_collected_images = []


# 图注前缀，按**优先级**排列（「特点」比「类别」信息量大）
_INTRO_PREFIXES = ("- 特点：", "- 类别：")


def _extract_intro(text: str) -> str:
    """从 chunk 文本里抽图注：优先「特点」，回退「类别」。

    ⚠️ **必须先扫完所有行再择优，不能「命中第一行就 break」**：
    products.md 的字段顺序恒为 `- ID：` → `- 类别：` → `- 特点：` → `- 图片：`，
    「类别」永远排在「特点」前面。单层「找到就 break」会让「特点」分支**永远走不到**——
    这条路径曾经真的成了死代码，162 个商品里 47 个有「特点」行却全取不到，
    用户看到的是「狗粮」而不是「高能增肌益关节」。
    （「按优先级排列的元组」只有在内层循环同时遍历前缀、且不提前退出时才有意义。）

    切片一律用 `len(prefix)`、**绝不写死数字**：这里曾经写成 `line[4:]`，
    而 `"- 特点："` 是 5 个字符（`-`/空格/`特`/`点`/全角`：`），偏移一位从全角冒号起切，
    导致每条图注都变成 `"：猫粮"`（用户实测看到的就是这个）。
    """
    found = {}
    for line in text.split("\n"):
        for p in _INTRO_PREFIXES:
            if p not in found and line.startswith(p):
                found[p] = line[len(p):].strip()
    return found.get("- 特点：") or found.get("- 类别：") or ""


# 本轮检索命中的 chunk_id（含**没有图**的）——供 trace 落盘，用来复现失败
_collected_retrievals = []


def _record_retrievals(results):
    """记录本轮检索命中的 chunk_id（去重、保序）。

    **为什么另开一个收集器，而不是复用 `_record_images`**：后者只收「有图」的商品
    （`if len(r) > 5 and r[5]`），而没图的一律拿不到——恰好包括最该复现的两类失败：
    政策问答（chunk_id 形如 `policies:{标题}`，本来就没图）和双低拒答。
    一个「用来复现失败」的字段，在最典型的失败上零覆盖，等于没有。

    ⚠️ 已知限制：这是**模块级 list**，多请求并发时会互相串（同 `_collected_images`）。
    对图片来说串了只是 UI 瑕疵，但 retrieved_ids 串了是**往排障表里写别人的数据**。
    demo 单用户 + 前端 streaming 期间禁用输入，当前不触发；要支持并发得改成 per-request 传递。
    """
    for r in results:
        cid = r[0] if r else None
        if cid and cid not in _collected_retrievals:
            _collected_retrievals.append(cid)


def pop_collected_retrievals() -> list:
    """读取并清空本轮检索命中的 chunk_id（agent 每轮结束后调用，供 trace 落盘）"""
    global _collected_retrievals
    ids = list(_collected_retrievals)
    _collected_retrievals = []
    return ids


def _record_images(results):
    """从检索结果收集商品图（去重），供展示层渲染。results 是六元组 (cid, score, text, title, product_id, image)。

    图必须带「介绍」——光图没文字是废的，用户不知道这商品是什么/有什么特点。
    介绍由 `_extract_intro` 从 chunk 文本抽取（特点优先、类别兜底），
    和图一起给前端，保证「商品↔图↔介绍」一一对应。
    """
    for r in results:
        if len(r) > 5 and r[5]:
            intro = _extract_intro(r[2])
            item = {"product_id": r[4], "title": r[3], "image": r[5], "intro": intro}
            if item not in _collected_images:
                _collected_images.append(item)


def pop_collected_images():
    """读取并清空本轮收集的商品图（agent 每轮对话结束后调用）"""
    global _collected_images
    imgs = list(_collected_images)
    _collected_images = []
    return imgs


def _search_products_sync(query: str, top_k: int, category: str = None) -> str:
    """search_products 的同步实现。embedding + rerank 是 CPU/GPU 密集，丢线程池跑（to_thread），不阻塞事件循环。

    `category` 显式给定时**优先**（跨轮补参：本句没写类别，但上一轮说了「猫猫」）；
    没给才从 query 现算——两条来源都拿不到就不过滤（保召回，与 `detect_category` 同口径）。
    """
    category = category or detect_category(query)
    label, results = _get_hybrid_retriever().search(query, top_k=top_k, category=category, kb_type="product")

    # 记录检索命中（chunk_id）放在**最前面**，双低也要记 ——
    # 双低是「检索到了但被策略拒了」，正是最该复现的一类失败，放在 return 之后会整类漏掉。
    _record_retrievals(results)

    if label == "双低":
        return "知识库检索无高置信度匹配。请如实告知用户暂未找到相关信息、可建议联系人工客服，不要编造商品信息。"

    _record_images(results)  # 商品图单独收集，供前端渲染（不进 LLM 文本）
    parts = []
    for i, (cid, score, text, title, product_id, image) in enumerate(results, 1):
        # 刻意**不重复 title**：紧跟其后的 `text` 首行就是 `## {title}`。
        # 一篇结果里标题出现两次白花 ~20 字符 × 3 条 = 60 字符，
        # 而 MAX_TOOL_RESULT_LEN=500 的余量实测只剩 10-14 字符（最坏组合理论值 526 会截断第 3 条尾部）。
        parts.append(f"[{i}] product_id={product_id}（内部字段）\n{text}")

    # 策略提示（系统生成的受信任指令）+ 检索结果（外部数据），分开标注，不混进「数据/指令分离」的防御里。
    #
    # 头部这三句说明，各自对应一个真实缺陷：
    #   ① **规模**：LLM 曾把「检索到 3 条」当成「知识库总共只有 3 款」，对用户下了错误的全局断言
    #      （用户实测踩到）。刻意**不假装知道总数**——语义检索里「总数」没有精确定义
    #      （「皇家猫粮」算 12 款还是 54 款取决于粒度），编一个数字比承认「这是前 N 条」更危险。
    #   ② **字段用途**：product_id 是给 check_stock 用的内部字段，曾经被 LLM 原样转述给用户
    #      （「皇家成年期全价猫粮主食罐（P057）」）。这里**只在本行说明一次**，不逐条重复——
    #      逐条写会顶到 MAX_TOOL_RESULT_LEN=500 的边界，导致第 3 条的尾部时有时无（偶发截断更难查）。
    #   ③ **措辞**：刻意不含 EMPTY_SIGNALS（"未查到/不存在/无高置信度匹配"）里的任何词——
    #      那些词会让 agent 把本次调用标记成「工具空返回」进 trace，污染可观测与评测。
    return (
        f"[检索策略：{label}]（本次返回前 {len(results)} 条，**仅为本次检索结果，不代表库里只有这些**；"
        f"用户要更多时换关键词再查；回答里不要出现 product_id 这类内部字段）\n"
        f"{STRATEGY_HINTS[label]}\n\n" + "\n\n".join(parts)
    )


async def search_products(query: str, top_k: int = 3, category: str = None) -> str:
    """商品知识库检索（RAG）——混合检索 + category 预过滤 + 四维置信度策略映射

    ⚠️ `category` **刻意不进 `TOOL_SCHEMAS`**——不让 LLM 填，由 `agent.py` 从会话上下文注入
    （「类别下沉」，与「金额下沉」同一哲学：能从上下文结构化拿到的参数，不由 LLM 填）。
    LLM 只负责 `query`（表达），类别是**判断**，判断交代码。
    """
    query = _sanitize(query)
    # 空 query 防御：LLM 偶发传空串/纯空白时，不进 embedding（避免空字符串向量化异常 + 省一次无效推理）
    if not query.strip():
        return "请描述一下您想了解的商品（如适用对象、成分、规格等）。"
    return await asyncio.to_thread(_search_products_sync, query, top_k, category)


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
