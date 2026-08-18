# -*- coding: utf-8 -*-
"""防御逻辑单元测试 —— 幻觉工具 + 死循环防护（mock LLM，不调真实 API）"""

import sys
import os
from unittest import mock

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.agent as agent


def _resp(tool_calls=None, content=None):
    r = mock.Mock()
    r.tool_calls = tool_calls
    r.content = content
    return r


def _tool_call(name, arguments):
    tc = mock.Mock()
    tc.id = "fake_id"
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def test_幻觉工具_错误回灌():
    """LLM 调不存在的工具 → 回灌「工具不存在」，不崩"""
    seen_messages = []
    fake_hallucination = _resp(tool_calls=[_tool_call("不存在的工具", "{}")])
    fake_final = _resp(tool_calls=None, content="好的")

    def fake_chat(messages, **kwargs):
        seen_messages.append(messages)
        return fake_hallucination if len(seen_messages) == 1 else fake_final

    with mock.patch('src.agent.chat', side_effect=fake_chat):
        result = agent.run_agent("测试")

    # 第二次调用时 messages 里应有「不存在」的错误回灌
    second = seen_messages[1]
    tool_msgs = [m for m in second if isinstance(m, dict) and m.get("role") == "tool"]
    assert any("不存在" in m["content"] for m in tool_msgs), "幻觉工具错误未回灌"
    assert result == "好的"
    print("✅ 幻觉工具校验：错误已回灌，未崩溃")


def test_死循环_连续三次相同():
    """LLM 连续 3 次调同一工具 → 判死循环"""
    same = _resp(tool_calls=[_tool_call("check_stock", '{"product_name": "幼犬成长粮"}')])
    with mock.patch('src.agent.chat', side_effect=[same] * 10):
        result = agent.run_agent("测试")
    assert "死循环" in result, result
    print("✅ 死循环防护：", result)


if __name__ == "__main__":
    test_幻觉工具_错误回灌()
    test_死循环_连续三次相同()
    print("\n✅ 防御逻辑全部通过")
