# -*- coding: utf-8 -*-
"""退款工单 MQ —— Redis list 模拟（模块 4）

把「工单落库 + 通知人工」从 /refund 同步链路拆出来异步化，演示四个概念：
  解耦（生产者/消费者经 MQ 解耦）、削峰（高峰进队列）、异步（落库不阻塞响应）、幂等消费（两层）。

为什么不引真 Kafka/RabbitMQ：demo 用 Redis list 模拟，说明「Redis 做 MQ 的局限」
（不持久化 / 无 ack / 无重试 / 至多一次语义）——这正是「真 MQ 要 Kafka/RabbitMQ」的对照。

Redis 连接复用 cache.py 的进程级单例（redis-py 连接池线程安全，不重复建连接）。
注意 cache._redis 的 socket_timeout=1 与 BRPOP 阻塞语义的交互需实测（redis-py 可能用命令
timeout 覆盖 socket_timeout）；consume_loop 已把 TimeoutError（空队列超时，静默）和
ConnectionError（真故障，重试）分开处理，两种行为都安全。
"""

import sys
import os
import re
import json
import uuid
import time
import threading

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pymysql
import redis
from src.backend import db
from src.backend.cache import _redis as _redis_client  # 复用缓存进程级单例连接

# MQ 键用独立前缀 mq:，不放 ecom:——cache.flush() 只清 ecom:*（业务缓存），
# MQ 队列和幂等标记是「在途数据」不是「缓存」，被 flush 清掉会导致在途消息静默丢失、
# 已受理的退款工单永不到账。独立前缀避开 flush 的 scan_iter("ecom:*")。
QUEUE_KEY = "mq:refund_queue"
DONE_PREFIX = "mq:refund:done:"
DONE_TTL = 604800  # 幂等标记 7 天：控制 key 增长（脱离 ecom: 后无 flush 兜底清理），过期由业务级唯一约束兜底

# 退款工单状态机（模块 5）：pending → approved → refunded（终态）；pending → rejected（终态）。
# 状态流转由代码层/人工驱动（审批接口），不由 LLM 驱动。状态常量放 mq.py（退款域），
# 不放 db.py（连接池基础设施，不是业务域）。
STATUS_PENDING = "待人工审批"
STATUS_APPROVED = "已批准"
STATUS_REFUNDED = "已退款"
STATUS_REJECTED = "已拒绝"

# 合法流转表：action → {当前状态: 新状态}。只有匹配的当前状态才允许该 action。
# 终态（refunded/rejected）不在任何 {当前状态} 里 → 任何 action 都非法（终态不可再流转）。
TRANSITIONS = {
    "approve": {STATUS_PENDING: STATUS_APPROVED},
    "execute": {STATUS_APPROVED: STATUS_REFUNDED},
    "reject": {STATUS_PENDING: STATUS_REJECTED},
}


def _validate_refund(order_id):
    """退款校验（金额下沉后）：LLM 只传 order_id，退款金额=订单金额（全额退款），由后端权威值决定。

    返回 (ok, err_msg, status_code, refund_amount)：
      ok=True  → refund_amount 是订单金额（后端权威值，落库唯一来源）
      ok=False → status_code 区分 404（订单不存在）和 400（格式/金额数据异常），供 /refund 翻译成 HTTPException。
    消费者拿 refund_amount 落库，不取消息字段（消息只带 order_id，无 amount 可篡改）。

    为什么不抛 HTTPException：HTTP 层概念不该泄漏进消费者线程；纯函数让「同步翻译成 HTTP、
    异步直接判断」两种用法都干净。
    """
    if not isinstance(order_id, str) or not re.fullmatch(r"\d{8,32}", order_id):
        return False, f"订单号格式非法：{order_id}", 400, None

    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT amount FROM orders WHERE order_id=%s", (order_id,))
        row = cur.fetchone()
    finally:
        db.close_conn(conn)

    if row is None:
        return False, f"未查到订单 {order_id}", 404, None

    # 金额解析（防御）：订单金额是「下单锁定的价格快照」，格式如 "¥89"。脏数据→拒绝退款，不抛异常
    # （原 float() 遇脏数据 ValueError 违反「纯函数不抛异常」设计，plan-reviewer 11-H 指出，顺手修）。
    try:
        order_amount = float(row[0].replace("¥", "").split("/")[0].strip())
    except (ValueError, AttributeError):
        return False, f"订单金额数据异常，无法退款，请联系人工", 400, None
    if order_amount <= 0:
        return False, f"订单金额异常（≤0），无法退款，请联系人工", 400, None
    return True, None, None, order_amount


def _create_refund_ticket(order_id, amount):
    """落库退款工单（幂等落库）。同步回退路径和异步消费者共用，等价旧 /refund 的落库逻辑。

    幂等靠 DB 唯一约束 UNIQUE(order_id)：并发/重复 INSERT 撞键 → IntegrityError →
    查已有工单返回 duplicate=True，不产生重复工单。「一个订单一个工单」由数据库硬兜底，
    同订单退 50 又退 80（不同金额）也会撞键——这是「状态机幂等」的 DB 兜底，不是先查后插。
    """
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        ticket_id = f"RF{uuid.uuid4().hex[:8].upper()}"
        try:
            cur.execute("INSERT INTO refunds VALUES (%s,%s,%s,%s)", (ticket_id, order_id, amount, STATUS_PENDING))
        except pymysql.IntegrityError:
            # 撞 UNIQUE(order_id)：同订单已有工单（不管金额），查已有返回 duplicate
            cur.execute("SELECT ticket_id, status FROM refunds WHERE order_id=%s", (order_id,))
            existing = cur.fetchone()
            if existing:
                return {"ticket_id": existing[0], "status": existing[1], "duplicate": True}
            raise
    finally:
        db.close_conn(conn)
    return {"ticket_id": ticket_id, "status": STATUS_PENDING, "duplicate": False}


def _transition(current, action):
    """状态机流转校验（纯函数）。返回 (ok, new_status)。

    ok=False 时 new_status 是错误信息（非法流转 / 未知操作）。
    终态（refunded/rejected）不在 TRANSITIONS 的 {当前状态} 里，任何 action 都非法。
    """
    if action not in TRANSITIONS:
        return False, f"未知操作：{action}"
    new_status = TRANSITIONS[action].get(current)
    if new_status is None:
        return False, f"非法状态流转：{current} 不能执行 {action}"
    return True, new_status


def _apply_transition(ticket_id, action):
    """审批/执行落库（条件 UPDATE 乐观锁）。返回 {ok, ...}。

    并发安全靠「UPDATE ... WHERE status=当前状态」：两个并发审批（approve+reject）都读到
    pending，条件 UPDATE 只有一个 affected rows=1，另一个 0 行 → 409。
    这就是「乐观锁版本号」幂等要点——不是「读 → 判断 → 普通 UPDATE」（那样 last-writer-wins）。

    先读 status 只为区分 404（工单不存在）和 409（非法流转/状态已变），真正的并发保证是条件 UPDATE。
    """
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM refunds WHERE ticket_id=%s", (ticket_id,))
        row = cur.fetchone()
        if row is None:
            return {"ok": False, "err": f"未找到工单 {ticket_id}", "status_code": 404}
        current = row[0]
        ok, result = _transition(current, action)
        if not ok:
            return {"ok": False, "err": result, "status_code": 409}
        new_status = result
        # 条件 UPDATE：只在 status 仍是 current 时更新（乐观锁），并发审批只有一个成功
        cur.execute("UPDATE refunds SET status=%s WHERE ticket_id=%s AND status=%s", (new_status, ticket_id, current))
        if cur.rowcount == 0:
            return {"ok": False, "err": "工单状态已变化，请刷新后重试", "status_code": 409}
        return {"ok": True, "ticket_id": ticket_id, "status": new_status}
    finally:
        db.close_conn(conn)


def _notify_human(ticket_id):
    """通知人工客服（mock）。真实场景这是「慢操作」（调人工系统/发短信），是异步化的核心动机。"""
    print(f"🔔 [MQ] 已通知人工客服处理退款工单 {ticket_id}")


def publish_refund(order_id):
    """生产者：发退款工单生成请求到 MQ。返回 {ok, message_id}。

    消息只带 order_id，不带 amount——金额唯一来源是消费者重查 DB 的订单金额（refund_amount），
    消息里的金额字段是可篡改的攻击面（超额退款洞），从源头消除。
    RPUSH 抛 RedisError 表示 MQ 不可用 → ok=False，调用方（/refund）回退同步落库。
    不做前置 ping：多一次往返 + 竞态窗口，实际操作失败才降级。
    """
    message_id = uuid.uuid4().hex
    payload = {"message_id": message_id, "order_id": order_id}
    try:
        _redis_client.rpush(QUEUE_KEY, json.dumps(payload, ensure_ascii=False))
    except redis.exceptions.RedisError:
        return {"ok": False, "message_id": None}
    return {"ok": True, "message_id": message_id}


def _process_message(payload):
    """消费者处理一条消息：幂等检查 → 校验 → 落库 → 通知人工。

    message_id 幂等（消息级）：SET NX EX 原子抢占标记，防「生产者重复入队同一条消息」。
    Redis 不可用时降级为「不拦」（acquired=True），业务级唯一约束仍兜底，正确性不丢。
    """
    message_id = payload["message_id"]
    order_id = payload["order_id"]

    # 幂等检查（消息级）：已处理过 → 跳过（重复消息）
    try:
        acquired = _redis_client.set(f"{DONE_PREFIX}{message_id}", "1", nx=True, ex=DONE_TTL)
    except redis.exceptions.RedisError:
        acquired = True  # 幂等标记不可用，降级不拦——业务级唯一约束兜底
    if not acquired:
        print(f"[MQ] 重复消息，跳过：{message_id}")
        return

    # 校验 + 查订单金额（消费者不信任消息：金额唯一来源是重查 DB 的 refund_amount，不取消息字段）
    ok, err_msg, _, refund_amount = _validate_refund(order_id)
    if not ok:
        # 消息已被 BRPOP 弹出，校验失败只能「记日志 + 丢弃」——异步化后无同步拒绝回执，
        # 用户已收到「已受理」但工单不会生成，这是「受理 ≠ 完成」的代价。
        print(f"[MQ] 消费者校验失败，丢弃消息（{message_id}）：{err_msg}")
        return

    ticket = _create_refund_ticket(order_id, refund_amount)
    if not ticket["duplicate"]:
        # 业务级幂等命中（撞唯一约束）时工单早已存在、早已通知过，不重复通知——避免重复下游副作用
        _notify_human(ticket["ticket_id"])
    print(f"[MQ] 工单生成：{ticket['ticket_id']}（订单 {order_id}，¥{refund_amount:g}）{'[重复已去重]' if ticket['duplicate'] else ''}")


def consume_loop(stop_event):
    """消费者循环（daemon 线程，lifespan 启动）。BRPOP 阻塞拉取，有限超时 + stop_event 干净退出。

    Redis 挂时线程不能死：catch RedisError → 记日志 → sleep 重试，否则消费者静默死亡、
    消息永久积压。socket_timeout=1 会让 BRPOP 周期性抛 TimeoutError（RedisError 子类），
    当「超时没消息」正常吞掉。
    """
    while not stop_event.is_set():
        try:
            item = _redis_client.brpop(QUEUE_KEY, timeout=5)
        except redis.exceptions.TimeoutError:
            # 空队列超时（正常，静默继续）：socket_timeout=1 可能让 BRPOP 周期性抛 TimeoutError
            continue
        except redis.exceptions.RedisError as e:
            # 真故障（连接断开等）：记日志 + sleep 重试，线程不能死，否则消息永久积压
            print(f"⚠️ [MQ] Redis 不可用，消费者 1s 后重试：{e}")
            time.sleep(1)
            continue
        if item is None:  # BRPOP timeout=5 正常返回 None（空队列）
            continue
        _, raw = item
        try:
            payload = json.loads(raw)
            _process_message(payload)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            # 非法消息（脏数据/手误入队）→ 记日志丢弃，不阻塞后续消息
            print(f"⚠️ [MQ] 非法消息丢弃：{raw!r}（{e}）")
        except pymysql.MySQLError as e:
            # DB 异常（MySQL 重启/连接池瞬时耗尽）→ 记日志丢弃，线程必须继续消费后续消息。
            # 消息已被 BRPOP 弹出无法重试（Redis 做 MQ 的已知代价），但线程不能死，否则消息永久积压。
            print(f"⚠️ [MQ] DB 异常，消息丢弃：{e}")
