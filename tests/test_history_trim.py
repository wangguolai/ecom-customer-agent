# -*- coding: utf-8 -*-
"""滑动窗口单元测试 —— token 预算 + 轮次粒度切"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.agent as agent


def _mk(role, content):
    return {"role": role, "content": content}


def test_轮次边界切():
    """超预算时，从最早轮整块丢，不切碎一轮"""
    messages = [
        _mk("system", "你是客服"),
        _mk("user", "问题1"), _mk("assistant", "答案1" * 100),  # 第一轮（长）
        _mk("user", "问题2"), _mk("assistant", "答案2"),         # 第二轮（短）
        _mk("user", "问题3"),                                    # 第三轮（当前轮）
    ]
    agent._trim_history(messages, max_tokens=200)
    contents = [m.get("content") for m in messages]
    assert "问题1" not in contents, "最早轮应该被丢弃"
    assert messages[0]["role"] == "system", "system 消息应保留"
    assert messages[-1]["content"] == "问题3", "当前轮应保留"
    print("✅ 轮次边界切：最早轮整块丢弃，system 和当前轮保留")


def test_只剩一轮不丢():
    """只剩当前一轮时，即使超预算也不丢（单轮爆由截断+步数兜底）"""
    messages = [
        _mk("system", "你是客服"),
        _mk("user", "当前问题" * 200),
    ]
    agent._trim_history(messages, max_tokens=50)
    assert len(messages) == 2, "system + 当前 user 都应保留"
    print("✅ 只剩一轮不丢：单轮爆兜底生效")


if __name__ == "__main__":
    test_轮次边界切()
    test_只剩一轮不丢()
    print("\n✅ 滑动窗口全部通过")
