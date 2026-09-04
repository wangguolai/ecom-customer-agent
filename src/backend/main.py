# -*- coding: utf-8 -*-
"""电商客服后端 —— 订单/物流/库存 真实数据源（FastAPI + MySQL）

替代 tools.py 里的内存 mock。agent 的工具通过 HTTP 查这里，才叫「真实工具」。
启动：uvicorn src.backend.main:app --port 8000
"""

import sys
import os
import re
import time
import json
import asyncio
import threading
from contextlib import asynccontextmanager

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.responses import StreamingResponse

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.backend.db import get_conn, close_conn
from src.backend import cache
from src.backend import fault
from src.backend import mq
from src.backend import ratelimit
from src.backend import auth
from src.backend.seed import _SEED_ORDERS, _SEED_LOGISTICS, _SEED_PRODUCTS


def _init_db():
    """建表 + 灌种子数据。全部表 DROP 重建：seed 是唯一源，保证 schema/数据每次启动最新。

    refunds 也 DROP 重建：模块 5 改 UNIQUE(order_id,amount) → UNIQUE(order_id)（状态机幂等，
    一个订单一个工单），schema 变更需重建；demo 历史工单是本地测试产物，无需保留。
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS orders")
        cur.execute("DROP TABLE IF EXISTS logistics")
        cur.execute("DROP TABLE IF EXISTS products")
        cur.execute("DROP TABLE IF EXISTS refunds")
        cur.execute("CREATE TABLE orders (order_id VARCHAR(32) PRIMARY KEY, status VARCHAR(20), created_at VARCHAR(32), product VARCHAR(128), amount VARCHAR(32))")
        cur.execute("CREATE TABLE logistics (order_id VARCHAR(32), time VARCHAR(32), location VARCHAR(64), status VARCHAR(20), KEY idx_order_id (order_id))")
        cur.execute("CREATE TABLE products (product_id VARCHAR(16) PRIMARY KEY, name VARCHAR(128), price DOUBLE, qty INTEGER)")
        cur.execute("CREATE TABLE refunds (ticket_id VARCHAR(32) PRIMARY KEY, order_id VARCHAR(32), amount DOUBLE, status VARCHAR(20), UNIQUE KEY uk_order (order_id))")
        cur.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s,%s)", _SEED_ORDERS)
        cur.executemany("INSERT INTO logistics VALUES (%s,%s,%s,%s)", _SEED_LOGISTICS)
        cur.executemany("INSERT INTO products VALUES (%s,%s,%s,%s)", _SEED_PRODUCTS)
    finally:
        close_conn(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 DB。不在 import 时执行——MySQL 连不上不该让 import 崩，移到这里报错更明确。

    _init_db() 会 DROP 重建 seed 表（重写真源），和 refresh.py 同属「重写真源」路径，
    缓存副本必须同步失效（cache.flush()），否则重启后最长 60s 返回旧数据——SSOT 单向链路闭环。
    """
    # 鉴权配置自检（模块 8）：AUTH_SECRET 缺失直接抛错，不让后端带着「无密钥」状态起来。
    # 放在 _init_db 之前——配置错误要在碰数据之前就暴露。
    auth.check_auth_config()
    _init_db()
    cache.flush()
    # Redis 探活（非致命）：没起来就降级查库，但启动日志要能区分「Redis OK / 不可用」方便排障
    print("✅ Redis OK（缓存可用）" if cache.ping() else "⚠️ Redis 不可用，降级查库（缓存旁路）")
    # 启动 MQ 消费者（daemon 线程）：BRPOP 阻塞拉取退款工单消息，异步落库。
    # stop_event 让 shutdown 干净退出（BRPOP 最多 5s 后返回）；daemon=True 保证进程退出不阻塞。
    _consumer_stop = threading.Event()
    _consumer_thread = threading.Thread(target=mq.consume_loop, args=(_consumer_stop,), daemon=True, name="refund-consumer")
    _consumer_thread.start()
    yield
    _consumer_stop.set()


app = FastAPI(title="电商客服后端", lifespan=lifespan)


# ── 限流依赖（模块 5）──
# 读接口挂 read 桶（松），写接口挂 write 桶（严：退款/审批/执行资金敏感）。
# Depends 依赖函数：超限 raise 429。client 固定 "demo"（单客户端），生产 per-IP + 全局双层。
#
# 阈值配置外部化（对齐模块 7「配置外部化」规范）：默认值 = 原硬编码值，行为零变化。
# 压测要临时放宽阈值时改环境变量即可，不用改代码。
def _int_env(name: str, default: int) -> int:
    """读整型环境变量，非法值回落默认（配置错不该让后端起不来）"""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _limit_read():
    if not ratelimit.rate_limit("read", "demo", window=_int_env("RL_READ_WINDOW", 10), max_req=_int_env("RL_READ_MAX", 100)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


def _limit_write():
    if not ratelimit.rate_limit("write", "demo", window=_int_env("RL_WRITE_WINDOW", 10), max_req=_int_env("RL_WRITE_MAX", 10)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


def _limit_auth():
    """登录接口独立限流桶（模块 8）。

    为什么不复用 write 桶：登录是暴力破解的靶子，攻击者狂刷密码会顺带把 write 桶打满，
    退款/审批这些正常业务写操作跟着被 429 —— 攻击一个接口，瘫痪一片。
    独立桶让爆破的代价只落在它自己身上（故障隔离，和熔断按下游分粒度同一个道理）。
    阈值也更严：5 次/60s。
    """
    if not ratelimit.rate_limit("auth", "demo", window=_int_env("RL_AUTH_WINDOW", 60), max_req=_int_env("RL_AUTH_MAX", 5)):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后重试")


@app.get("/orders/{order_id}")
def get_order(order_id: str, _: None = Depends(_limit_read)):
    # 参数校验（穿透三层第一层）：非法格式直接 400，不查库不写空标记——
    # 否则任意不存在订单号都会写 30s 空标记，被 LLM 幻觉/攻击者刷爆 Redis。
    # 上界 32 与 orders.logistics 的 order_id VARCHAR(32) 对齐：超长纯数字在库中必然不存在，
    # 校验住它才真正堵住「刷空标记」口子。
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")
    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    # 注入的是 tools.py 已有的降级路径（超时→友好提示、脏数据→数据异常），不定义新降级行为。
    mode = fault.apply_fault("orders")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")  # 200 但 JSON 解析失败 → bad_response
    if mode == "empty":
        return {}  # 合法 JSON 缺字段，触发工具层「数据异常」降级
    cache_key = f"ecom:order:{order_id}"

    def rebuild():
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT status, created_at, product, amount FROM orders WHERE order_id=%s", (order_id,))
            row = cur.fetchone()
        finally:
            close_conn(conn)
        if row is None:
            return False, None
        return True, {"order_id": order_id, "status": row[0], "created_at": row[1], "product": row[2], "amount": row[3]}

    _, data = cache.get_or_rebuild(cache_key, 60, rebuild)
    if data is None:  # 命中空标记（不存在）
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id}")
    return data


@app.get("/logistics/{order_id}")
def get_logistics(order_id: str, _: None = Depends(_limit_read)):
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")
    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    mode = fault.apply_fault("logistics")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")  # 200 但 JSON 解析失败 → bad_response
    if mode == "empty":
        return {}  # 合法 JSON 缺字段，触发工具层「数据异常」降级
    cache_key = f"ecom:logistics:{order_id}"

    def rebuild():
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT time, location, status FROM logistics WHERE order_id=%s ORDER BY time", (order_id,))
            rows = cur.fetchall()
        finally:
            close_conn(conn)
        if not rows:
            return False, None
        return True, {"order_id": order_id, "traces": [{"time": r[0], "location": r[1], "status": r[2]} for r in rows]}

    _, data = cache.get_or_rebuild(cache_key, 60, rebuild)
    if data is None:
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id} 的物流信息")
    return data


@app.get("/products/{product_id}")
def get_product(product_id: str, _: None = Depends(_limit_read)):
    """商品实时价格 + 库存（动态数据走工具实时查，数据分治：价格/库存不进向量库）"""
    # 格式校验（对齐 orders 的防御水平）：任意短字符串都会 404 写空标记，被 LLM 幻觉/攻击者刷爆 Redis。
    # 用格式 P\d{1,15} 拦掉，和 seed 的 P001 形态对齐。
    if not re.fullmatch(r"P\d{1,15}", product_id):
        raise HTTPException(status_code=400, detail=f"商品 ID 非法：{product_id}")
    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    mode = fault.apply_fault("products")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")  # 200 但 JSON 解析失败 → bad_response
    if mode == "empty":
        return {}  # 合法 JSON 缺字段，触发工具层「数据异常」降级
    cache_key = f"ecom:product:{product_id}"

    def rebuild():
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name, price, qty FROM products WHERE product_id=%s", (product_id,))
            row = cur.fetchone()
        finally:
            close_conn(conn)
        if row is None:
            return False, None
        return True, {"product_id": product_id, "name": row[0], "price": row[1], "qty": row[2]}  # qty=0 是真实数据（缺货），正常缓存

    _, data = cache.get_or_rebuild(cache_key, 30, rebuild)
    if data is None:
        raise HTTPException(status_code=404, detail=f"未查到商品 {product_id}")
    return data


# 客服在线状态的运行时覆盖（评测用）：None = 未覆盖，回落读 CS_ONLINE 环境变量。
# 为什么需要：CS_ONLINE 是启动时读的，容器里改要重启，而重启会 DROP 重建全部表。
# 转人工三分支（在线 / 不在线 / 查询失败）评测必须能在一次运行内切换状态。
_online_override = None


@app.get("/online")
def check_online(_: None = Depends(_limit_read)):
    """客服在线状态（动态数据走接口查，数据分治）。demo 用环境变量 CS_ONLINE mock（可测试切换），
    生产来自坐席系统（IM/客服工作台）实时状态。

    CS_ONLINE 布尔解析：字符串 "false" 是 truthy，必须 strip().lower() == "true" 判断。
    """
    # 故障注入（评测用）：让 transfer_to_human 走「在线状态查不到」的降级分支
    mode = fault.apply_fault("online")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")
    if mode == "empty":
        return {}  # 缺 online 字段 → tools 侧 .get("online") 为 None → 按不在线保守处理
    if _online_override is not None:
        return {"online": _online_override}
    online = os.environ.get("CS_ONLINE", "true").strip().lower() == "true"
    return {"online": online}


@app.post("/refund")
def refund_order(payload: dict, _: None = Depends(_limit_write)):
    """退款（写操作）—— 金额下沉 + MQ 异步化。

    金额下沉：LLM 只传 order_id，退款金额=订单金额（后端查，全额退款），
    LLM 无权指定金额（防被诱导填 0/负数/超额，数据分治：能结构化查到的参数不让 LLM 填）。
    代码级防御不变：只生成「待人工审批」工单，不直接退款。
    同步只做「校验 + 查订单金额」（即时反馈），「落库 + 通知人工」走 MQ 异步。
    降级：MQ（Redis）不可用 → 回退同步落库，退款不因 MQ 挂而失败。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="缺少 order_id")
    # 边界清洗：孤立代理等非法码点在「错误回显」时会再炸一次 UnicodeEncodeError（fuzz 实测）。
    # 外部输入进系统第一件事就是把非法码点降级成 ?，之后校验/回显/落库全程安全。
    if isinstance(order_id, str):
        order_id = order_id.encode("utf-8", "replace").decode("utf-8")

    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    # 注入的是 tools.py 已有的降级路径（超时→友好提示、脏数据→数据异常），不定义新降级行为。
    mode = fault.apply_fault("refund")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")
    if mode == "empty":
        return {}

    # 校验 + 查订单金额（金额下沉：退款金额=订单金额，后端权威值）
    ok, err_msg, status_code, refund_amount = mq._validate_refund(order_id)
    if not ok:
        raise HTTPException(status_code=status_code, detail=err_msg)

    # 发消息到 MQ（消息只带 order_id，不带 amount，防篡改）
    pub = mq.publish_refund(order_id)
    if pub["ok"]:
        return {"status": "已受理", "message_id": pub["message_id"], "ticket_id": None, "refund_amount": refund_amount}

    # 降级回退：MQ 不可用 → 同步落库（refund_amount 是订单金额）
    ticket = mq._create_refund_ticket(order_id, refund_amount)
    return {"status": "已受理", "message_id": None, "ticket_id": ticket["ticket_id"], "refund_amount": refund_amount}


@app.post("/chat/stream")
async def chat_stream(request: Request, payload: dict, _: None = Depends(_limit_read)):
    """SSE 流式输出最终答案（text/event-stream，逐 token）。

    客户端断开检测：由 Starlette 1.6 的 StreamingResponse 内置兜住，代码不重复造轮子。

    踩坑复盘（2026-09-02 误判 → 2026-09-03 纠正）：
      - 早先以为「客户端断开后服务端继续把答案生成完」要自己写检测，加了
        request.is_disconnected()。但它不可靠：Starlette 1.6 用 anyio CancelScope(cs.cancel())
        做「非阻塞 receive」，cancel 立即触发，await _receive() 还没等到 uvicorn 的
        message_event 就被打断，永远拿不到 http.disconnect——这是死代码。
      - 实际 Starlette 1.6 的 StreamingResponse.__call__ 已按 ASGI spec_version 分支处理断开：
        spec < 2.4 用内置 listen_for_disconnect（start_soon 跑流式 + await listen_for_disconnect，
        断开 cancel 整个 task_group）；spec >= 2.4 靠 ASGI 2.4 规范「send 到断开连接抛 OSError」
        捕获。当前 uvicorn 0.52 发 spec_version 2.3，走 listen_for_disconnect 可靠分支
        （FAKE_STREAM 假流式 + 原始 socket 断开实测：只发 3 段就停止）。
      - 所以断开时 Starlette 主动 cancel 本生成器，这里只保留 CancelledError 兜底：接住让
        请求体面结束（LLM 流已随 cancel 级联关闭，停止烧 token）。
    """
    # 惰性 import：避免后端启动即加载 agent 的重依赖（只在 /chat/stream 请求时才加载）
    from src.agent import AgentSession
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    user_msg = payload.get("message")
    if not isinstance(user_msg, str) or not user_msg.strip():
        raise HTTPException(status_code=400, detail="缺少 message")
    # 边界清洗：孤立代理等非法码点在流式链路（LLM/SSE）里一样会炸，进系统第一件事先降级
    user_msg = user_msg.encode("utf-8", "replace").decode("utf-8")

    session = AgentSession()

    async def event_gen():
        try:
            # 假流式（debug，零付费）：FAKE_STREAM=1 时用假内容 + sleep 模拟慢速流式，
            # 专门验证「断开检测」机制，不真调 LLM。和 ENABLE_DEBUG_FAULT 同性质的调试能力。
            if os.environ.get("FAKE_STREAM", "").strip().lower() in ("1", "true", "yes"):
                for i in range(500):
                    yield f"data: {json.dumps({'delta': f'假内容第{i}段'}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.02)
                print("假流式跑完 500 段（客户端未断开）")
                return
            async for delta in session.stream_chat(user_msg):
                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # Starlette 1.6 内置 listen_for_disconnect 检测到断开会 cancel 本生成器，
            # 接住让请求体面结束；LLM 流已随 cancel 级联关闭，停止烧 token。
            print("客户端已断开，停止流式输出")

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/auth/token")
def issue_token(payload: dict, _: None = Depends(_limit_auth)):
    """签发 admin token（模块 8）。demo 账号来自环境变量，密码只存 pbkdf2 慢哈希，不存明文。

    失败一律返回同一个 401「用户名或密码错误」——不区分「用户不存在」和「密码错」，防用户枚举
    （能枚举出有效用户名，爆破范围就从「用户名×密码」缩小成「密码」，成本差几个数量级）。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    # 边界清洗：username/password 里的孤立代理会打崩 auth 的编码（fuzz 实测 500）。
    # 这里先把非法码点降级，auth.authenticate 内部也有 errors="replace" 双保险。
    username = payload.get("username")
    password = payload.get("password")
    username = username.encode("utf-8", "replace").decode("utf-8") if isinstance(username, str) else ""
    password = password.encode("utf-8", "replace").decode("utf-8") if isinstance(password, str) else ""
    role = auth.authenticate(username, password)
    if not role:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"access_token": auth.create_token(payload.get("username"), role), "token_type": "Bearer", "expires_in": auth.TOKEN_TTL}


@app.post("/refund/{ticket_id}/review")
def review_refund(ticket_id: str, payload: dict, _: None = Depends(_limit_write),
                  __: dict = Depends(auth.require_role("admin"))):
    """人工审批（代码层驱动状态机流转，不由 LLM 驱动）。action=approve/reject。

    并发安全：mq._apply_transition 用条件 UPDATE（WHERE status=当前状态）乐观锁，
    两个并发审批只有一个成功，另一个 409。
    鉴权（模块 8）：这是资金敏感操作，挂 require_role("admin")（垂直越权防线），
    和 execute 一致。IP 白名单是生产进一步的纵深，demo 未做（知道规范即可）。
    """
    # 路径参数格式校验（对齐 orders 的 \d{8,32} / products 的 P\d{1,15} 防御水平）：
    # 工单号由 mq._create_refund_ticket 生成，格式 RF + 8 位大写 hex。非法值拦在进库之前。
    if not re.fullmatch(r"RF[0-9A-F]{8}", ticket_id):
        raise HTTPException(status_code=400, detail=f"工单号格式非法：{ticket_id}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    action = payload.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail=f"非法 action：{action}，只支持 approve/reject")
    result = mq._apply_transition(ticket_id, action)
    if not result["ok"]:
        raise HTTPException(status_code=result["status_code"], detail=result["err"])
    return {"ticket_id": ticket_id, "status": result["status"]}


@app.post("/refund/{ticket_id}/execute")
def execute_refund(ticket_id: str, _: None = Depends(_limit_write),
                   __: dict = Depends(auth.require_role("admin"))):
    """退款执行（approved → refunded，mock）。真实场景接支付/财务，这里只改状态。

    execute 后 orders.status 不联动（demo mock），生产退款到账要联动订单状态。

    鉴权（模块 8）：这里和 review 是真正的「钱闸」，必须 admin。
    注意 /refund（agent 代客申请）刻意不加鉴权——它只生成待审工单、没有资金流，属低权操作。
    按「资金影响」分级，而不是「凡写接口一律鉴权」：后者会白白打断 agent 链路且没有安全收益。
    """
    if not re.fullmatch(r"RF[0-9A-F]{8}", ticket_id):
        raise HTTPException(status_code=400, detail=f"工单号格式非法：{ticket_id}")
    result = mq._apply_transition(ticket_id, "execute")
    if not result["ok"]:
        raise HTTPException(status_code=result["status_code"], detail=result["err"])
    return {"ticket_id": ticket_id, "status": result["status"]}


# ── 故障注入调试端点（模块：评测用，生产默认关闭）──
# ENABLE_DEBUG_FAULT 环境变量为真才挂路由——「能让系统出故障」的无鉴权端点不能裸奔
# （和 prompt injection 的写权限开关同类：调试后门代码级默认关闭）。
if os.environ.get("ENABLE_DEBUG_FAULT", "").strip().lower() in ("1", "true", "yes"):
    @app.post("/debug/fault")
    def debug_set_fault(payload: dict, _: None = Depends(_limit_write)):
        target = payload.get("target")
        mode = payload.get("mode")
        count = payload.get("count", 1)
        if target not in ("orders", "logistics", "products", "refund", "online"):
            raise HTTPException(status_code=400, detail=f"非法 target：{target}")
        if mode not in ("timeout", "dirty", "empty"):
            raise HTTPException(status_code=400, detail=f"非法 mode：{mode}")
        fault.set_fault(target, mode, count)
        return {"status": "已注入", "target": target, "mode": mode, "count": count}

    @app.post("/debug/fault/clear")
    def debug_clear_fault(payload: dict = None, _: None = Depends(_limit_write)):
        target = (payload or {}).get("target")
        fault.clear_fault(target)
        return {"status": "已清除"}

    @app.post("/debug/online")
    def debug_set_online(payload: dict, _: None = Depends(_limit_write)):
        """运行时切换客服在线状态（评测用）。online=null 清除覆盖、回落 CS_ONLINE 环境变量。

        和 /debug/fault 同属「评测用调试设施」，共用 ENABLE_DEBUG_FAULT 门控——
        能改业务状态的无鉴权端点，生产必须默认关闭。
        """
        global _online_override
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        value = payload.get("online")
        if value is not None and not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"online 必须是布尔或 null：{value!r}")
        _online_override = value
        return {"status": "已设置", "online_override": _online_override}
