# -*- coding: utf-8 -*-
"""电商客服后端 —— 订单/物流/库存 真实数据源（FastAPI + SQLite）

替代 tools.py 里的内存 mock。agent 的工具通过 HTTP 查这里，才叫「真实工具」。
启动：uvicorn src.backend.main:app --port 8000
"""

import sys
import os
import sqlite3
from uuid import uuid4

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

DB_PATH = os.path.join(_project_root, "data", "ecommerce.db")

app = FastAPI(title="电商客服后端")

# 种子数据（从 tools.py 的 MOCK_* 迁过来，内容一致）
_SEED_ORDERS = [
    ("20240818001", "已发货", "2024-08-18 10:23", "幼犬成长粮（贝乐牌）1.5kg", "¥89"),
    ("20240817002", "待付款", "2024-08-17 15:40", "豆腐猫砂 6L", "¥32"),
    ("20240816003", "已完成", "2024-08-16 09:12", "猫薄荷玩具球", "¥19"),
]
_SEED_LOGISTICS = [
    ("20240818001", "2024-08-18 12:00", "杭州分拨中心", "已揽收"),
    ("20240818001", "2024-08-18 18:30", "杭州转运中心", "运输中"),
    ("20240818001", "2024-08-19 08:00", "上海转运中心", "派送中"),
]
_SEED_STOCK = [
    ("幼犬成长粮", 120, "¥89 / ¥219"),
    ("成犬均衡粮", 80, "¥119 / ¥399"),
    ("猫薄荷玩具球", 45, "¥19"),
    ("豆腐猫砂", 0, "¥32"),
]


def _init_db():
    """建表 + 灌种子数据（只在空表时灌，避免重复）"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, status TEXT, created_at TEXT, product TEXT, amount TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS logistics (order_id TEXT, time TEXT, location TEXT, status TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS stock (product_name TEXT PRIMARY KEY, qty INTEGER, price TEXT)")
    cur.execute("SELECT COUNT(*) FROM orders")
    if cur.fetchone()[0] == 0:
        cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", _SEED_ORDERS)
        cur.executemany("INSERT INTO logistics VALUES (?,?,?,?)", _SEED_LOGISTICS)
        cur.executemany("INSERT INTO stock VALUES (?,?,?)", _SEED_STOCK)
    conn.commit()
    conn.close()


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT status, created_at, product, amount FROM orders WHERE order_id=?", (order_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id}")
    return {"order_id": order_id, "status": row[0], "created_at": row[1], "product": row[2], "amount": row[3]}


@app.get("/logistics/{order_id}")
def get_logistics(order_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT time, location, status FROM logistics WHERE order_id=? ORDER BY time", (order_id,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id} 的物流信息")
    return {"order_id": order_id, "traces": [{"time": r[0], "location": r[1], "status": r[2]} for r in rows]}


@app.get("/stock/{product_name}")
def get_stock(product_name: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT qty, price FROM stock WHERE product_name=?", (product_name,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"未查到商品「{product_name}」")
    return {"product_name": product_name, "qty": row[0], "price": row[1]}


@app.post("/refund")
def refund_order(payload: dict):
    """退款（写操作）——代码级防御：不直接退款，只生成「待人工审批」工单；参数校验拦截非法金额。

    Prompt Injection 的核心防线：LLM 只有「建议权」（提交退款申请），没有「执行权」（真正退款在人工审批）。
    恶意数据诱导 LLM 调 refund，最多生成一个待审批工单，且金额非法会被参数校验拦下。
    """
    order_id = payload.get("order_id")
    amount = payload.get("amount")
    if not order_id or amount is None:
        raise HTTPException(status_code=400, detail="缺少 order_id 或 amount")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT amount FROM orders WHERE order_id=?", (order_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"未查到订单 {order_id}")

    # 参数校验（代码级，拦截 LLM 诱导的非法金额：0 元 / 负数 / 超订单金额）
    order_amount = float(row[0].replace("¥", "").split("/")[0].strip())
    if amount <= 0 or amount > order_amount:
        raise HTTPException(status_code=400, detail=f"退款金额非法：必须 >0 且 ≤ 订单金额 ¥{order_amount}")

    # 幂等去重：同一订单 + 同一金额的工单已存在，直接返回已存在的（不重复插入）。
    # 生产更标准的是「客户端幂等键（refund_id）」，demo 用业务字段简化。
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS refunds (ticket_id TEXT PRIMARY KEY, order_id TEXT, amount REAL, status TEXT)")
    cur.execute("SELECT ticket_id, status FROM refunds WHERE order_id=? AND amount=?", (order_id, amount))
    existing = cur.fetchone()
    if existing:
        conn.close()
        return {"ticket_id": existing[0], "order_id": order_id, "amount": amount, "status": existing[1], "duplicate": True}

    # 生成退款工单（待人工审批，不直接退款）
    ticket_id = f"RF{uuid4().hex[:8].upper()}"
    cur.execute("INSERT INTO refunds VALUES (?,?,?,?)", (ticket_id, order_id, amount, "待人工审批"))
    conn.commit()
    conn.close()
    return {"ticket_id": ticket_id, "order_id": order_id, "amount": amount, "status": "待人工审批", "duplicate": False}


_init_db()
