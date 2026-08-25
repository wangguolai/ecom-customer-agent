# -*- coding: utf-8 -*-
"""MQ 落地测试 —— 验证退款工单异步化：异步受理 + 消费者落库 + 幂等消费（业务级唯一约束）

前置：MySQL + Redis + 后端（uvicorn src.backend.main:app --port 8000）已起。
降级回退（Redis 挂 → 同步落库）单独手动验证，不在本脚本（需停 Redis）。
"""

import sys
import os
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import httpx
import pymysql
from dotenv import load_dotenv

load_dotenv(os.path.join(_project_root, ".env"))
BACKEND_URL = "http://127.0.0.1:8000"

# 测试直查 MySQL（autocommit=True，避免手动 commit）
def _db_conn():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", "ecommerce"),
        charset="utf8mb4",
        autocommit=True,
    )


def _count_refunds(order_id):
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM refunds WHERE order_id=%s", (order_id,))
        return cur.fetchone()[0]
    finally:
        conn.close()


def _clear_refunds():
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM refunds")
    finally:
        conn.close()


def _post_refund(order_id, amount):
    resp = httpx.post(f"{BACKEND_URL}/refund", json={"order_id": order_id, "amount": amount}, timeout=5)
    return resp.status_code, resp.json()


def main():
    # 清空 refunds 表，保证测试干净（refunds 是运行时数据，不 DROP，测试前手动清）
    _clear_refunds()

    print("📍 [1/2] 正常路径：异步受理 → 消费者落库生成工单")
    order_id = "20240818001"
    status, body = _post_refund(order_id, 89)
    assert status == 200, f"期望 200，实际 {status}：{body}"
    assert body["status"] == "已受理", body
    assert body["ticket_id"] is None, f"异步路径 ticket_id 应为 None（工单异步生成），实际 {body['ticket_id']}"
    print(f"  POST /refund → 已受理，message_id={body['message_id']}")
    time.sleep(2)  # 等消费者线程消费（BRPOP 阻塞拉取 + 落库，毫秒级，2s 足够）
    n = _count_refunds(order_id)
    assert n == 1, f"期望消费者生成 1 张工单，实际 {n}"
    print(f"  ✅ 消费者已异步落库（refunds 表 {n} 张工单）")

    print("📍 [2/2] 幂等：重复 POST（不同 message_id，本质同一笔退款）→ 业务级唯一约束拦截")
    status2, body2 = _post_refund(order_id, 89)
    assert status2 == 200, f"期望 200，实际 {status2}：{body2}"
    time.sleep(2)
    n2 = _count_refunds(order_id)
    assert n2 == 1, f"业务级幂等应拦截重复（UNIQUE(order_id, amount)），期望 1 张，实际 {n2}"
    print(f"  ✅ 重复退款被唯一约束拦截（仍 {n2} 张工单）")

    print("🎉 MQ 异步化 + 幂等消费：全部通过")


if __name__ == "__main__":
    main()
