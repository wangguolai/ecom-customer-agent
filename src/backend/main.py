# -*- coding: utf-8 -*-
"""电商客服后端 —— 订单/物流/库存 真实数据源（FastAPI + MySQL）

替代 tools.py 里的内存 mock。agent 的工具通过 HTTP 查这里，才叫「真实工具」。
启动：uvicorn src.backend.main:app --port 8000
"""

import sys
import os
import re
from uuid import uuid4
from contextlib import asynccontextmanager

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException
import pymysql

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.backend.db import get_conn, close_conn
from src.backend import cache
from src.backend.seed import _SEED_ORDERS, _SEED_LOGISTICS, _SEED_STOCK


def _init_db():
    """建表 + 灌种子数据。seed 表 DROP 重建：seed 是唯一源，保证 schema/数据每次启动最新。

    refunds 是运行时数据（退款工单），不 DROP（保留工单），CREATE IF NOT EXISTS 带唯一约束（幂等兜底）。
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS orders")
        cur.execute("DROP TABLE IF EXISTS logistics")
        cur.execute("DROP TABLE IF EXISTS stock")
        cur.execute("CREATE TABLE orders (order_id VARCHAR(32) PRIMARY KEY, status VARCHAR(20), created_at VARCHAR(32), product VARCHAR(128), amount VARCHAR(32))")
        cur.execute("CREATE TABLE logistics (order_id VARCHAR(32), time VARCHAR(32), location VARCHAR(64), status VARCHAR(20), KEY idx_order_id (order_id))")
        cur.execute("CREATE TABLE stock (product_name VARCHAR(64) PRIMARY KEY, qty INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS refunds (ticket_id VARCHAR(32) PRIMARY KEY, order_id VARCHAR(32), amount DOUBLE, status VARCHAR(20), UNIQUE KEY uk_order_amount (order_id, amount))")
        cur.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s,%s)", _SEED_ORDERS)
        cur.executemany("INSERT INTO logistics VALUES (%s,%s,%s,%s)", _SEED_LOGISTICS)
        cur.executemany("INSERT INTO stock VALUES (%s,%s)", _SEED_STOCK)
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
    yield


app = FastAPI(title="电商客服后端", lifespan=lifespan)


@app.get("/orders/{order_id}")
def get_order(order_id: str):
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
def get_logistics(order_id: str):
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


@app.get("/stock/{product_name}")
def get_stock(product_name: str):
    if not product_name or len(product_name) > 64:
        raise HTTPException(status_code=400, detail=f"商品名非法：{product_name}")
    cache_key = f"ecom:stock:{product_name}"
    hit, data = cache.get_json(cache_key)
    if hit:
        if data is None:
            raise HTTPException(status_code=404, detail=f"未查到商品「{product_name}」")
        return data

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT qty FROM stock WHERE product_name=%s", (product_name,))
        row = cur.fetchone()
    finally:
        close_conn(conn)
    if row is None:
        cache.set_empty(cache_key, 30)
        raise HTTPException(status_code=404, detail=f"未查到商品「{product_name}」")
    data = {"product_name": product_name, "qty": row[0]}  # qty=0 是真实数据（缺货），正常缓存
    cache.set_json(cache_key, data, 30)
    return data


@app.post("/refund")
def refund_order(payload: dict):
    """退款（写操作）——代码级防御：不直接退款，只生成「待人工审批」工单；参数校验拦截非法金额。

    幂等：UNIQUE(order_id, amount) 兜底。refund 是单步写（只插一条工单），原子性由单条 INSERT 保证，
    不需要显式事务；撞唯一键后 autocommit 下直接 SELECT 就能看到已提交的行，也不需要 rollback。
    """
    order_id = payload.get("order_id")
    amount = payload.get("amount")
    if not order_id or amount is None:
        raise HTTPException(status_code=400, detail="缺少 order_id 或 amount")
    # 与 get_order/get_logistics 同标准：非法订单号直接 400，不进 SELECT
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT amount FROM orders WHERE order_id=%s", (order_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"未查到订单 {order_id}")

        # 参数校验（代码级，拦截 LLM 诱导的非法金额：0 元 / 负数 / 超订单金额）
        order_amount = float(row[0].replace("¥", "").split("/")[0].strip())
        if amount <= 0 or amount > order_amount:
            raise HTTPException(status_code=400, detail=f"退款金额非法：必须 >0 且 ≤ 订单金额 ¥{order_amount}")

        # 插入工单（单条原子）；撞唯一键 → 已存在工单，查出来返回 duplicate
        ticket_id = f"RF{uuid4().hex[:8].upper()}"
        try:
            cur.execute("INSERT INTO refunds VALUES (%s,%s,%s,%s)", (ticket_id, order_id, amount, "待人工审批"))
        except pymysql.IntegrityError:
            cur.execute("SELECT ticket_id, status FROM refunds WHERE order_id=%s AND amount=%s", (order_id, amount))
            existing = cur.fetchone()
            if existing:
                return {"ticket_id": existing[0], "order_id": order_id, "amount": amount, "status": existing[1], "duplicate": True}
            raise
    finally:
        close_conn(conn)

    return {"ticket_id": ticket_id, "order_id": order_id, "amount": amount, "status": "待人工审批", "duplicate": False}
