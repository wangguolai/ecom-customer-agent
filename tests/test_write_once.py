# -*- coding: utf-8 -*-
"""写工具「本回合单次执行」对抗测试 —— 零 API 成本（monkeypatch LLM，不真调）

锁定 `agent.py` 里 `called_write` 这道防线的两个方向：
  1. 同一回合内，完全相同的写操作重复调用 → 第二次被拦（防模型自动重试导致重复退款/重复工单）
  2. 同一回合内，两个**不同订单**的退款 → 都必须放行（不能只按工具名拦，否则用户被无声少退一单）
  3. 跨回合：下一轮对话应重置防线 → 可以再次退款（粒度是「本回合」不是「本会话」）

用 monkeypatch 构造「LLM 连续返回指定 tool_calls」的确定性序列，绕开真实 LLM 的随机性，
精准打在这道防线上。真正的 LLM 行为（会不会真重试）由 eval_write_ops 付费评测覆盖。

用法：python tests/test_write_once.py
"""

import sys
import os
import json
import asyncio
from unittest.mock import patch, MagicMock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import agent
from src.agent import AgentSession


def _fake_message(tool_calls=None, content=""):
    """构造一个和 OpenAI SDK message 形状兼容的假对象（model_dump 用得上）"""
    m = MagicMock()
    m.content = content
    m.tool_calls = tool_calls or []
    if tool_calls:
        calls = []
        for i, (name, args) in enumerate(tool_calls):
            tc = MagicMock()
            tc.id = f"call_{i}"
            tc.function.name = name
            tc.function.arguments = json.dumps(args, ensure_ascii=False)
            calls.append(tc)
        m.tool_calls = calls
    m.model_dump.return_value = {"role": "assistant", "content": content,
                                 "tool_calls": [{"id": tc.id, "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                                for tc in m.tool_calls]} if tool_calls else {"role": "assistant", "content": content}
    return m


class _FakeUsage:
    total_tokens = 10
    prompt_tokens = 8
    completion_tokens = 2
    prompt_cache_hit_tokens = 0
    prompt_cache_miss_tokens = 0


def _run_with_responses(responses):
    """用预设的 LLM 响应序列跑一轮对话。返回 (answer, session)。"""
    called = {"count": 0}

    async def fake_chat_with_usage(messages, tools=None, **kw):
        idx = min(called["count"], len(responses) - 1)
        called["count"] += 1
        return responses[idx], _FakeUsage()

    with patch.object(agent, "chat_with_usage", fake_chat_with_usage):
        session = AgentSession()
        answer = asyncio.run(session.chat("测试写操作"))
        return answer, session


def _tool_messages(session):
    """取出本轮对话里所有 role=tool 的消息 content"""
    return [m["content"] for m in session.messages if isinstance(m, dict) and m.get("role") == "tool"]


def _refund_exec_count(monkeypatch_counter):
    """统计 refund_order 工具真正执行的次数（拦截掉的不算）"""
    return monkeypatch_counter


_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name} {detail}")


def test_same_order_twice_blocked():
    """同回合完全相同写操作连发两次 → 第二次被拦"""
    from src import tools
    real_refund = tools.TOOL_MAP["refund_order"]
    counter = {"n": 0}

    async def fake_refund(order_id):
        counter["n"] += 1
        return f"退款申请已受理（受理号 fake），金额 ¥99，工单处理中。"

    # ⚠️ 必须换 TOOL_MAP 里的引用：agent.py 是 `from src.tools import TOOL_MAP`，
    # 派发时走 TOOL_MAP[name]，只换 tools.refund_order 换不到 agent 已持有的函数对象。
    tools.TOOL_MAP["refund_order"] = fake_refund
    try:
        responses = [
            _fake_message(tool_calls=[("refund_order", {"order_id": "20240818001"})]),
            _fake_message(tool_calls=[("refund_order", {"order_id": "20240818001"})]),  # 完全相同 → 应拦
            _fake_message(content="退款申请已受理，请留意工单进度。"),
        ]
        _, session = _run_with_responses(responses)
        tool_msgs = _tool_messages(session)
        blocked_msgs = [m for m in tool_msgs if "本回合已执行过" in m]
        check("相同写操作第二次被拦（返回拦截话术）", len(blocked_msgs) == 1, f"tool_msgs={tool_msgs}")
        check("工具真正执行次数 == 1", counter["n"] == 1, f"执行了 {counter['n']} 次")
    finally:
        tools.TOOL_MAP["refund_order"] = real_refund


def test_two_different_orders_both_execute():
    """同回合两个不同订单退款 → 都放行（不能只按工具名拦）"""
    from src import tools
    real_refund = tools.TOOL_MAP["refund_order"]
    counter = {"n": 0}

    async def fake_refund(order_id):
        counter["n"] += 1
        return f"退款申请已受理，金额 ¥99，工单处理中。"

    tools.TOOL_MAP["refund_order"] = fake_refund
    try:
        responses = [
            _fake_message(tool_calls=[
                ("refund_order", {"order_id": "20240818001"}),
                ("refund_order", {"order_id": "20240817002"}),  # 不同订单 → 应放行
            ]),
            _fake_message(content="两笔退款都已提交。"),
        ]
        _, session = _run_with_responses(responses)
        tool_msgs = _tool_messages(session)
        check("不同订单两次退款都放行（无拦截话术）", not any("本回合已执行过" in m for m in tool_msgs), f"tool_msgs={tool_msgs}")
        check("工具真正执行次数 == 2", counter["n"] == 2, f"执行了 {counter['n']} 次")
    finally:
        tools.TOOL_MAP["refund_order"] = real_refund


def test_cross_turn_reset():
    """跨回合：下一轮对话应重置防线，可以再次退款（粒度是本回合，不是本会话）"""
    from src import tools
    real_refund = tools.TOOL_MAP["refund_order"]
    counter = {"n": 0}

    async def fake_refund(order_id):
        counter["n"] += 1
        return f"退款申请已受理，金额 ¥99，工单处理中。"

    tools.TOOL_MAP["refund_order"] = fake_refund
    try:
        # 第 1 轮：退订单 A
        resp1 = [
            _fake_message(tool_calls=[("refund_order", {"order_id": "20240818001"})]),
            _fake_message(content="退款已提交。"),
        ]
        answer1, session = _run_with_responses(resp1)
        # 第 2 轮：同一 session 继续退订单 A（或 B）—— 防线应已重置
        resp2 = [
            _fake_message(tool_calls=[("refund_order", {"order_id": "20240818001"})]),
            _fake_message(content="退款已提交。"),
        ]
        with patch.object(agent, "chat_with_usage", _fake_sequence(resp2)):
            answer2 = asyncio.run(session.chat("再退一次这个订单"))
        check("跨回合防线重置：第二轮退款放行", "本回合已执行过" not in answer2 and counter["n"] == 2, f"counter={counter['n']}")
    finally:
        tools.TOOL_MAP["refund_order"] = real_refund


def _fake_sequence(responses):
    called = {"count": 0}

    async def fake(messages, tools=None, **kw):
        idx = min(called["count"], len(responses) - 1)
        called["count"] += 1
        return responses[idx], _FakeUsage()
    return fake


if __name__ == "__main__":
    print("=" * 70)
    print("写工具「本回合单次执行」对抗测试")
    print("-" * 70)
    test_same_order_twice_blocked()
    test_two_different_orders_both_execute()
    test_cross_turn_reset()
    print("=" * 70)
    print(f"结果：通过 {_passed} / 失败 {_failed}")
    print("=" * 70)
    sys.exit(1 if _failed else 0)
