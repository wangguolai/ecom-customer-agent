# -*- coding: utf-8 -*-
"""退款状态机测试 —— 状态流转 + 非法流转拦截 + UNIQUE(order_id) 幂等

前置：MySQL 已起 + 后端启动过一次（lifespan 建 refunds 表 + UNIQUE(order_id) 约束）。
直接测 mq.py 的状态机函数（不经 HTTP/MQ 消费者），但依赖 refunds 表已由后端建好。
"""

import sys
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from dotenv import load_dotenv

load_dotenv(os.path.join(_project_root, ".env"))

from src.backend import db
from src.backend import mq


def _clear_refunds():
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM refunds")
    finally:
        db.close_conn(conn)


def main():
    _clear_refunds()

    print("📍 [1/4] 建工单 → 状态 pending")
    t1 = mq._create_refund_ticket("20240818001", 89)
    assert t1["status"] == mq.STATUS_PENDING, t1
    assert t1["duplicate"] is False
    ticket_id = t1["ticket_id"]
    print(f"  ✅ 工单 {ticket_id} 状态={t1['status']}")

    print("📍 [2/4] 状态流转：pending → approved → refunded")
    r1 = mq._apply_transition(ticket_id, "approve")
    assert r1["ok"] and r1["status"] == mq.STATUS_APPROVED, r1
    r2 = mq._apply_transition(ticket_id, "execute")
    assert r2["ok"] and r2["status"] == mq.STATUS_REFUNDED, r2
    print("  ✅ pending → approved → refunded")

    print("📍 [3/4] 非法流转：终态 refunded 不能再 approve → 409")
    r3 = mq._apply_transition(ticket_id, "approve")
    assert r3["ok"] is False and r3["status_code"] == 409, r3
    print(f"  ✅ refunded 再 approve 被拒：{r3['err']}")

    print("📍 [4/4] UNIQUE(order_id) 幂等：同订单不同金额二次退款 → duplicate")
    t2 = mq._create_refund_ticket("20240818001", 50)  # 不同金额，撞 UNIQUE(order_id)
    assert t2["duplicate"] is True, f"同订单不同金额应撞 UNIQUE(order_id)，实际 {t2}"
    assert t2["ticket_id"] == ticket_id, "应返回已有工单"
    print(f"  ✅ 同订单退 50（已有工单 {ticket_id}）被 UNIQUE(order_id) 拦：duplicate=True")

    print("🎉 退款状态机：全部通过")


if __name__ == "__main__":
    main()
