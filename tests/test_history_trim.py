# -*- coding: utf-8 -*-
"""上下文压缩单元测试 —— 摘要压缩 + 丢轮次对照基线（mock 摘要，不调真实 LLM）"""

import sys
import os
from unittest import mock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.agent as agent


def _mk(role, content):
    return {"role": role, "content": content}


def _fake_summarize(history_messages):
    """mock 摘要：返回固定摘要（含订单号）+ fake usage + 耗时 0，不调真实 LLM"""
    fake_usage = mock.Mock(prompt_tokens=100, completion_tokens=20)
    return "用户查了订单 20240818001", fake_usage, 0.0


def test_摘要压缩_旧轮被摘要替代():
    """超预算时，最早轮被摘要替代，关键实体（订单号）保留，最近一轮 + 当前轮保留"""
    messages = [
        _mk("system", "你是客服"),
        _mk("user", "查订单 20240818001"), _mk("assistant", "好的" * 200),  # 第一轮（长）
        _mk("user", "问题2"), _mk("assistant", "答案2"),                      # 第二轮（最近完成轮）
        _mk("user", "问题3"),                                                 # 第三轮（当前轮）
    ]
    with mock.patch.object(agent, "_summarize", side_effect=_fake_summarize):
        agent._compress_history(messages, max_tokens=200)
    contents = [m.get("content", "") for m in messages]
    assert messages[0]["role"] == "system", "system 应保留"
    assert any(c.startswith(agent.SUMMARY_PREFIX) for c in contents), "应有摘要消息"
    assert "20240818001" in "".join(contents), "摘要应保留订单号（关键实体不丢）"
    assert messages[-1]["content"] == "问题3", "当前轮应保留"
    assert "问题2" in contents, "最近一轮完成对话应保留"
    print("✅ 摘要压缩：旧轮被摘要替代，关键实体保留，最近一轮+当前轮保留")


def test_摘要压缩_只剩一轮不压():
    """只剩当前一轮时，即使超预算也不压缩（单轮爆由截断+步数兜底）"""
    messages = [
        _mk("system", "你是客服"),
        _mk("user", "当前问题" * 200),
    ]
    with mock.patch.object(agent, "_summarize", side_effect=_fake_summarize) as m:
        agent._compress_history(messages, max_tokens=50)
    assert m.call_count == 0, "只剩当前一轮不应调摘要"
    assert len(messages) == 2, "system + 当前 user 都应保留"
    print("✅ 只剩一轮不压缩：单轮爆兜底生效")


def test_摘要压缩_未超预算不触发():
    """不超预算时，不应调摘要"""
    messages = [
        _mk("system", "你是客服"),
        _mk("user", "问题1"), _mk("assistant", "答案1"),
        _mk("user", "问题2"),
    ]
    with mock.patch.object(agent, "_summarize", side_effect=_fake_summarize) as m:
        agent._compress_history(messages, max_tokens=10000)
    assert m.call_count == 0, "未超预算不应调摘要"
    print("✅ 未超预算不触发摘要")


def test_丢轮次_对照基线():
    """_trim_history 仍是无差别丢轮次（保留作「丢轮次」对照，评测用它对比）"""
    messages = [
        _mk("system", "你是客服"),
        _mk("user", "问题1"), _mk("assistant", "答案1" * 100),
        _mk("user", "问题2"), _mk("assistant", "答案2"),
        _mk("user", "问题3"),
    ]
    agent._trim_history(messages, max_tokens=200)
    contents = [m.get("content") for m in messages]
    assert "问题1" not in contents, "丢轮次应丢弃最早轮"
    assert messages[-1]["content"] == "问题3", "当前轮保留"
    print("✅ 丢轮次对照基线：最早轮被丢弃")


if __name__ == "__main__":
    test_摘要压缩_旧轮被摘要替代()
    test_摘要压缩_只剩一轮不压()
    test_摘要压缩_未超预算不触发()
    test_丢轮次_对照基线()
    print("\n✅ 上下文压缩全部通过")
