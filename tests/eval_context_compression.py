# -*- coding: utf-8 -*-
"""上下文压缩评测：量化「丢轮次 vs 摘要压缩」的 token 消耗 + 关键实体保留（效果退化）

用法：
  python tests/eval_context_compression.py          # 逻辑层：mock 摘要（正则提取订单号），不烧钱
  python tests/eval_context_compression.py --real   # 真实层：调真实 LLM 摘要（需用户明确说跑）

指标：
  关键实体保留 —— 压缩后 messages 里是否还含订单号（丢轮次会丢，摘要应保留）
  压缩率       —— 压缩后 / 压缩前 的估算 token 比（越小越省）
"""

import sys
import os
import re
import asyncio
from copy import deepcopy
from unittest import mock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.agent as agent
from cases import COMPRESSION_CASES as EVAL_SET

# 订单号模式（评测的关键实体：跨轮指代依赖的对象）
ORDER_ID_PATTERN = re.compile(r"20240818\d{3}")

# 评测用小预算（500），强制触发压缩——真实 MAX_HISTORY_TOKENS=2000 时轮次要更多才超限。
# 用 500 是为了让 3 条评测场景都真正触发压缩，清晰对比「丢轮次 vs 摘要压缩」的实体保留差异。
EVAL_MAX_TOKENS = 500

# 上下文压缩评测集已集中到 cases.py（COMPRESSION_CASES），此处不再内联定义。


async def _mock_summarize(history_messages):
    """mock 摘要：从历史里正则提取订单号，生成「保留实体」的摘要（模拟理想 LLM 摘要）"""
    text = "".join(str(m.get("content", "")) for m in history_messages)
    order_ids = list(dict.fromkeys(ORDER_ID_PATTERN.findall(text)))  # 去重保序
    summary = "用户咨询过的订单号：" + "、".join(order_ids) if order_ids else "（无订单号）"
    fake_usage = mock.Mock(prompt_tokens=len(text), completion_tokens=len(summary))
    return summary, fake_usage, 0.0


def _build_messages(turns):
    """构造完整 messages：system + 多轮对话（最后一个 turn 是当前轮，无 assistant）"""
    messages = [{"role": "system", "content": "你是客服"}]
    for user_msg, assistant_msg in turns:
        messages.append({"role": "user", "content": user_msg})
        if assistant_msg is not None:
            messages.append({"role": "assistant", "content": assistant_msg})
    return messages


def _entity_kept(messages, entities):
    text = "".join(str(m.get("content", "")) for m in messages)
    return {e: (e in text) for e in entities}


async def run_eval(real=False):
    summarize = agent._summarize if real else _mock_summarize

    print("=" * 76)
    print(f"{'场景':<14} {'策略':<8} {'压缩前':>7} {'压缩后':>7} {'压缩率':>7}  {'订单号保留'}")
    print("-" * 76)

    for i, scene in enumerate(EVAL_SET, 1):
        print(f"📍 [{i}/{len(EVAL_SET)}] {scene['name']} — 对比丢轮次 vs 摘要压缩中...")
        base = _build_messages(scene["turns"])
        before = agent._messages_tokens(base)

        for strategy in ["丢轮次", "摘要压缩"]:
            messages = deepcopy(base)
            if strategy == "丢轮次":
                agent._trim_history(messages, EVAL_MAX_TOKENS)
            else:
                if real:
                    await agent._compress_history(messages, EVAL_MAX_TOKENS, trace=None)
                else:
                    with mock.patch.object(agent, "_summarize", side_effect=summarize):
                        await agent._compress_history(messages, EVAL_MAX_TOKENS, trace=None)
            after = agent._messages_tokens(messages)
            ratio = after / before if before else 0
            kept = _entity_kept(messages, scene["entities"])
            kept_str = " ".join(("✅" if kept[e] else "❌") + e for e in scene["entities"])
            print(f"{scene['name']:<14} {strategy:<8} {before:>7} {after:>7} {ratio:>7.2%}  {kept_str}")

    print("=" * 76)
    print("结论：丢轮次=无差别丢最早轮，订单号随轮次一起丢；摘要压缩=旧轮压成摘要，关键实体保留。")


if __name__ == "__main__":
    asyncio.run(run_eval(real="--real" in sys.argv))
