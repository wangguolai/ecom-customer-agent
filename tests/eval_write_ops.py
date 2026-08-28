# -*- coding: utf-8 -*-
"""写操作评测 —— 退款「不承诺到账」+ 转人工「不在线不声称已转接」+ 写操作错误处理

定位：写操作的风险性质不同于读 —— 答错读是「说错话」，答错写是「做错事」。
两条最贵的错：退款说「已到账」（用户按钱会到预期行动）、转人工说「已转接」（用户干等）。
SYSTEM_PROMPT 第 6/8 条写了约束，但「写了 ≠ 生效」，本评测验证约束真的落到输出上。

判定：三重断言，裁判层零付费（唯一付费点 = 跑 agent 本身）。
  1. forbid 禁用词 —— 输出不得出现（退款类绝不能说「已到账」等）
  2. require 必含词 —— 输出必须体现的约束（待审批 / 工单 / 工作时间等，任一命中）
  3. trace 断言 —— 过程正确性（工具调用次数）
  外加两条写操作专属断言：backend 实际收到 refund 请求次数（Redis DONE 键计数）、
  require_ticket（工单号 TK[0-9A-F]{8} 必须出现）。

依赖：后端以 ENABLE_DEBUG_FAULT=1 启动（挂 /debug/fault + /debug/online 调试端点）。

用法：python tests/eval_write_ops.py
"""

import sys
import os
import re
import asyncio

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from src.agent import AgentSession
from src import tools
from cases import WRITE_OPS_CASES as EVAL_SET

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
_TICKET_RE = re.compile(r"TK[0-9A-F]{8}")
_RF_RE = re.compile(r"\bRF[0-9A-F]{8}\b")  # 编造的工单号（RF 前缀是后端生成的格式）


def _backend_up() -> bool:
    """探测后端 + /debug/fault、/debug/online 挂载性（三者缺一评测会静默失效）"""
    try:
        for ep in ("/orders/20240818001", "/logistics/20240818001", "/products/P001"):
            if not requests.get(f"{BACKEND_URL}{ep}", timeout=3.0).ok:
                return False
        if requests.post(f"{BACKEND_URL}/debug/fault/clear", json={}, timeout=3.0).status_code != 200:
            return False
        if requests.post(f"{BACKEND_URL}/debug/online", json={"online": None}, timeout=3.0).status_code != 200:
            return False
        return True
    except requests.RequestException:
        return False


def _inject(target, mode, count):
    resp = requests.post(f"{BACKEND_URL}/debug/fault", json={"target": target, "mode": mode, "count": count}, timeout=1.0)
    if resp.status_code != 200:
        raise RuntimeError(f"故障注入失败（HTTP {resp.status_code}）")


def _clear_fault():
    try:
        requests.post(f"{BACKEND_URL}/debug/fault/clear", json={}, timeout=1.0)
    except requests.RequestException:
        pass


def _set_online(online):
    requests.post(f"{BACKEND_URL}/debug/online", json={"online": online}, timeout=1.0)


def _refund_done_count() -> int:
    """数 Redis mq:refund:done:* 键。

    为什么用这个当「后端实际收到 refund 请求次数」的观测点：
    refunds 表有 UNIQUE(order_id)，同订单退 5 次表里也只有 1 行，COUNT 恒 1，是自证断言。
    而每次真正到达后端的 refund 请求 → 发消息到 MQ → 新 message_id → 消费者置一个
    mq:refund:done:{message_id} 幂等标记（7 天 TTL）。所以 done 键增量 = 后端真实收到次数。
    """
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=int(os.environ.get("REDIS_PORT", "6379")), decode_responses=True)
        return len(list(r.scan_iter("mq:refund:done:*")))
    except Exception:
        return -1  # Redis 不可用，调用方跳过该断言


def _has_block_msg(session) -> bool:
    """本轮是否有工具被「本回合已执行过」拦截（证明防重复防线触发）"""
    return any("本回合已执行过" in m.get("content", "")
               for m in session.messages if isinstance(m, dict) and m.get("role") == "tool")


async def _run_case(case):
    """跑单条 case，返回 (answer, trace, session, backend_refund_delta)"""
    name = case["name"]
    # 前置：故障注入 + 在线状态切换
    if case.get("fault"):
        target, mode, count = case["fault"]
        _inject(target, mode, count)
    if "online" in case:
        _set_online(case["online"])

    done_before = _refund_done_count()
    session = AgentSession()
    answer = await session.chat(case["query"])
    trace = session.get_last_trace()

    # 后置清理：故障状态残留会污染下一个 case
    _clear_fault()
    tools._breaker.reset()
    if "online" in case:
        _set_online(None)

    done_after = _refund_done_count()
    delta = (done_after - done_before) if (done_before >= 0 and done_after >= 0) else None
    return answer, trace, session, delta


def _count(trace, name):
    return sum(1 for n, _, _, _ in trace.tool_calls if n == name)


async def run_eval():
    if not _backend_up():
        print("后端未启动（或未开 ENABLE_DEBUG_FAULT，/debug/fault 与 /debug/online 没挂载），无法跑写操作评测。")
        return

    print("=" * 80)
    print(f"写操作评测：{len(EVAL_SET)} 条，三重断言 + 后端请求计数（裁判层零付费）")
    print("=" * 80)

    passed = 0
    for i, case in enumerate(EVAL_SET, 1):
        name = case["name"]
        answer, trace, session, backend_delta = await _run_case(case)

        forbid = case.get("forbid", [])
        require = case.get("require", [])
        trace_assert = case.get("trace", {})
        require_ticket = case.get("require_ticket", False)
        max_backend = case.get("max_backend_refund")

        # 1. 禁用词（核心断言，最严）
        forbid_hit = [w for w in forbid if w in answer]
        # RF 工单号用词边界正则，避免「PERFECT」这种误匹配
        if "RF" in forbid and not forbid_hit:
            if _RF_RE.search(answer):
                forbid_hit.append("RF(工单号)")
        forbid_ok = not forbid_hit

        # 2. 必含词（软：任一命中；「未查到」这类否定词也算命中）
        require_ok = not require or any(w in answer for w in require)

        # 3. trace 断言
        trace_ok = all(_count(trace, tn) == c for tn, c in trace_assert.items())

        # 4. 工单号（转人工类：无论在线/不在线都要生成工单）
        ticket_ok = (not require_ticket) or bool(_TICKET_RE.search(answer))

        # 5. 后端实际收到 refund 次数（写操作错误处理）
        backend_ok = True
        if max_backend is not None and backend_delta is not None:
            backend_ok = backend_delta <= max_backend

        ok = forbid_ok and require_ok and trace_ok and ticket_ok and backend_ok
        if ok:
            passed += 1

        tools_list = [n for n, _, _, _ in trace.tool_calls]
        print(f"📍 [{i}/{len(EVAL_SET)}] {name}")
        print(f"    工具={tools_list} end={trace.end_reason} 后端refund次数={backend_delta}")
        print(f"    answer={answer[:80]}")
        if forbid_hit:
            print(f"   ❌ 禁用词命中：{forbid_hit}")
        if not require_ok:
            print(f"   ❌ 必含词全未命中：{require}")
        if not trace_ok:
            print(f"   ❌ trace 断言不符：期望 {trace_assert}，实际 {[(n, _count(trace, n)) for n in trace_assert]}")
        if not ticket_ok:
            print(f"   ❌ 应生成工单号但未出现 TK 前缀工单")
        if not backend_ok:
            print(f"   ❌ 后端实际收到 refund 请求 {backend_delta} 次 > 上限 {max_backend}")
        if _has_block_msg(session):
            print(f"   ℹ️ 检测到「本回合已执行过」拦截话术（防重复防线触发）")
        print(f"   {'✅' if ok else '❌'} {'通过' if ok else '失败'}")

    print("=" * 80)
    print(f"结果：{passed}/{len(EVAL_SET)} 条通过")
    print("=" * 80)


if __name__ == "__main__":
    from src.infra.warmup import warmup_models
    warmup_models()
    asyncio.run(run_eval())
