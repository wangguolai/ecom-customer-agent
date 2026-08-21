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
from copy import deepcopy
from unittest import mock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.agent as agent

# 订单号模式（评测的关键实体：跨轮指代依赖的对象）
ORDER_ID_PATTERN = re.compile(r"20240818\d{3}")

# 评测用小预算（500），强制触发压缩——真实 MAX_HISTORY_TOKENS=2000 时轮次要更多才超限。
# 用 500 是为了让 3 条评测场景都真正触发压缩，清晰对比「丢轮次 vs 摘要压缩」的实体保留差异。
EVAL_MAX_TOKENS = 500

# 评测集：多轮对话场景，早期轮含订单号，最后一轮是「指代追问」（依赖早期轮的订单号）
# turns = [(user_msg, assistant_msg), ...]，最后一个 turn 的 assistant 是 None（当前轮未答）
EVAL_SET = [
    {
        "name": "单订单指代",
        "turns": [
            ("查一下订单 20240818001 的物流到哪了", "物流轨迹：杭州转运中心已发出，下一站上海分拨" * 30),
            ("这款幼犬粮适合我家金毛吗", "幼犬粮富含优质蛋白，适合中大型犬" * 30),
            ("这个订单能退款吗", None),
        ],
        "entities": ["20240818001"],
    },
    {
        "name": "多订单指代",
        "turns": [
            ("订单 20240818001 发货了吗", "订单已发货，物流单号 SF123456" * 30),
            ("订单 20240818002 呢", "订单还在备货中" * 30),
            ("第一个订单能退吗", None),
        ],
        "entities": ["20240818001", "20240818002"],
    },
    {
        "name": "订单+售后混合",
        "turns": [
            ("订单 20240818001 到哪了", "已签收" * 30),
            ("你们的退货政策是什么", "7 天无理由退货，质量问题 15 天" * 30),
            ("那这个订单退款要多久", None),
        ],
        "entities": ["20240818001"],
    },
]


def _mock_summarize(history_messages):
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


def run_eval(real=False):
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
                    agent._compress_history(messages, EVAL_MAX_TOKENS, trace=None)
                else:
                    with mock.patch.object(agent, "_summarize", side_effect=summarize):
                        agent._compress_history(messages, EVAL_MAX_TOKENS, trace=None)
            after = agent._messages_tokens(messages)
            ratio = after / before if before else 0
            kept = _entity_kept(messages, scene["entities"])
            kept_str = " ".join(("✅" if kept[e] else "❌") + e for e in scene["entities"])
            print(f"{scene['name']:<14} {strategy:<8} {before:>7} {after:>7} {ratio:>7.2%}  {kept_str}")

    print("=" * 76)
    print("结论：丢轮次=无差别丢最早轮，订单号随轮次一起丢；摘要压缩=旧轮压成摘要，关键实体保留。")


if __name__ == "__main__":
    run_eval(real="--real" in sys.argv)
