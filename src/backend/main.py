# -*- coding: utf-8 -*-
"""电商客服后端 —— 订单/物流/库存 真实数据源（FastAPI + MySQL）

替代 tools.py 里的内存 mock。agent 的工具通过 HTTP 查这里，才叫「真实工具」。
启动：uvicorn src.backend.main:app --port 8000
"""

import sys
import os
import re
import threading
from contextlib import asynccontextmanager

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException, Depends

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.backend.db import get_conn, close_conn
from src.backend import cache
from src.backend import mq
from src.backend import ratelimit
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
def _limit_read():
    if not ratelimit.rate_limit("read", "demo", window=10, max_req=100):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


def _limit_write():
    if not ratelimit.rate_limit("write", "demo", window=10, max_req=10):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


@app.get("/orders/{order_id}")
def get_order(order_id: str, _: None = Depends(_limit_read)):
    # 参数校验（穿透三层第一层）：非法格式直接 400，不查库不写空标记——
    # 否则任意不存在订单号都会写 30s 空标记，被 LLM 幻觉/攻击者刷爆 Redis。
    # 上界 32 与 orders.logistics 的 order_id VARCHAR(32) 对齐：超长纯数字在库中必然不存在，
    # 校验住它才真正堵住「刷空标记」口子。
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")
    cache_key = f"ecom:order:{order_id}"
    hit, data = cache.get_json(cache_key)
    if hit:
        if data is None:  # 命中空标记（不存在）
            raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id}")
        return data

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, created_at, product, amount FROM orders WHERE order_id=%s", (order_id,))
        row = cur.fetchone()
    finally:
        close_conn(conn)
    if row is None:
        cache.set_empty(cache_key, 30)  # 缓存空标记防穿透
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id}")
    data = {"order_id": order_id, "status": row[0], "created_at": row[1], "product": row[2], "amount": row[3]}
    cache.set_json(cache_key, data, 60)
    return data


@app.get("/logistics/{order_id}")
def get_logistics(order_id: str, _: None = Depends(_limit_read)):
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")
    cache_key = f"ecom:logistics:{order_id}"
    hit, data = cache.get_json(cache_key)
    if hit:
        if data is None:
            raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id} 的物流信息")
        return data

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT time, location, status FROM logistics WHERE order_id=%s ORDER BY time", (order_id,))
        rows = cur.fetchall()
    finally:
        close_conn(conn)
    if not rows:
        cache.set_empty(cache_key, 30)
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id} 的物流信息")
    data = {"order_id": order_id, "traces": [{"time": r[0], "location": r[1], "status": r[2]} for r in rows]}
    cache.set_json(cache_key, data, 60)
    return data


@app.get("/products/{product_id}")
def get_product(product_id: str, _: None = Depends(_limit_read)):
    """商品实时价格 + 库存（动态数据走工具实时查，数据分治：价格/库存不进向量库）"""
    # 格式校验（对齐 orders 的防御水平）：任意短字符串都会 404 写空标记，被 LLM 幻觉/攻击者刷爆 Redis。
    # 用格式 P\d{1,15} 拦掉，和 seed 的 P001 形态对齐。
    if not re.fullmatch(r"P\d{1,15}", product_id):
        raise HTTPException(status_code=400, detail=f"商品 ID 非法：{product_id}")
    cache_key = f"ecom:product:{product_id}"
    hit, data = cache.get_json(cache_key)
    if hit:
        if data is None:
            raise HTTPException(status_code=404, detail=f"未查到商品 {product_id}")
        return data

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, price, qty FROM products WHERE product_id=%s", (product_id,))
        row = cur.fetchone()
    finally:
        close_conn(conn)
    if row is None:
        cache.set_empty(cache_key, 30)
        raise HTTPException(status_code=404, detail=f"未查到商品 {product_id}")
    data = {"product_id": product_id, "name": row[0], "price": row[1], "qty": row[2]}  # qty=0 是真实数据（缺货），正常缓存
    cache.set_json(cache_key, data, 30)
    return data


@app.post("/refund")
def refund_order(payload: dict, _: None = Depends(_limit_write)):
    """退款（写操作）—— MQ 异步化：同步校验 + 发消息，消费者异步落库 + 通知人工。

    代码级防御不变：只生成「待人工审批」工单，不直接退款；参数校验拦截非法金额。
    同步只做「参数校验」（即时反馈，非法订单/金额不让用户白等），「落库 + 通知人工」走 MQ 异步。
    降级：MQ（Redis）不可用 → 回退同步落库（复用 _create_refund_ticket，等价旧行为），退款不因 MQ 挂而失败。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    order_id = payload.get("order_id")
    amount = payload.get("amount")
    if not order_id or amount is None:
        raise HTTPException(status_code=400, detail="缺少 order_id 或 amount")

    # 同步校验（即时反馈）：订单号格式 / 订单存在 / 金额区间
    ok, err_msg, status_code = mq._validate_refund(order_id, amount)
    if not ok:
        raise HTTPException(status_code=status_code, detail=err_msg)

    # 发消息到 MQ
    pub = mq.publish_refund(order_id, amount)
    if pub["ok"]:
        return {"status": "已受理", "message_id": pub["message_id"], "ticket_id": None}

    # 降级回退：MQ 不可用 → 同步落库（复用 _create_refund_ticket，等价旧行为）
    ticket = mq._create_refund_ticket(order_id, amount)
    return {"status": "已受理", "message_id": None, "ticket_id": ticket["ticket_id"]}


@app.post("/refund/{ticket_id}/review")
def review_refund(ticket_id: str, payload: dict, _: None = Depends(_limit_write)):
    """人工审批（代码层驱动状态机流转，不由 LLM 驱动）。action=approve/reject。

    并发安全：mq._apply_transition 用条件 UPDATE（WHERE status=当前状态）乐观锁，
    两个并发审批只有一个成功，另一个 409。
    审批/执行接口无鉴权是 demo 取舍（资金敏感操作），生产必须鉴权 + IP 白名单。
    """
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
def execute_refund(ticket_id: str, _: None = Depends(_limit_write)):
    """退款执行（approved → refunded，mock）。真实场景接支付/财务，这里只改状态。

    execute 后 orders.status 不联动（demo mock），生产退款到账要联动订单状态。
    """
    result = mq._apply_transition(ticket_id, "execute")
    if not result["ok"]:
        raise HTTPException(status_code=result["status_code"], detail=result["err"])
    return {"ticket_id": ticket_id, "status": result["status"]}
