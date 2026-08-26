# -*- coding: utf-8 -*-
"""ReAct 循环边界评测：读 trace 断言「过程」（循环次数/工具调用序列），裁判层零付费

判的是「过程断言」不是「结果断言」：LLM 裁判只看 answer 判不了「调了几次工具/是否死循环」，
必须读 Trace 的 tool_calls/steps/end_reason。唯一付费点是跑 agent 本身（LLM 决策）。

用法：python tests/eval_react_boundary.py（跑 agent 调 LLM，需用户明确说跑）
"""

import sys
import os
import asyncio

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# tests/ 目录（脚本所在目录）显式加进 path，让 `from cases import ...` 不依赖「脚本目录自动进 sys.path[0]」的隐式行为
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.agent import AgentSession
from cases import REACT_BOUNDARY_CASES as EVAL_SET


def _count(trace, name):
    """统计 trace 里某工具被调用次数"""
    return sum(1 for n, _, _, _ in trace.tool_calls if n == name)


# name -> 过程断言函数（读 trace，返回 bool）。阈值分档：单次检索 2 步、多工具 <=8 步。
ASSERTIONS = {
    "ReAct-无意义输入防空轮": lambda t: len(t.tool_calls) == 0,
    "ReAct-单次可解决禁多轮loop": lambda t: _count(t, "search_products") == 1 and len(t.tool_calls) <= 2 and len(t.steps) <= 3,
    "ReAct-工具返回后禁二次调用": lambda t: _count(t, "search_orders") <= 1 and len(t.tool_calls) <= 2,
    "ReAct-歧义反问禁调工具": lambda t: len(t.tool_calls) == 0,
    "ReAct-知识库无答案禁重试": lambda t: _count(t, "search_products") <= 2,
    "ReAct-用户否定停工具链": lambda t: len(t.tool_calls) == 0,
    "ReAct-多工具冲突防乱调": lambda t: 1 <= len(t.tool_calls) <= 5 and len(t.steps) <= 8,
    "ReAct-咨询类误调订单工具": lambda t: _count(t, "search_orders") == 0 and _count(t, "search_logistics") == 0 and _count(t, "search_products") >= 1,
    "ReAct-订单问题误走检索": lambda t: _count(t, "search_products") == 0 and (_count(t, "search_orders") + _count(t, "search_logistics")) >= 1,
    "ReAct-结果不满意禁刷": lambda t: _count(t, "search_products") <= 2,
    "ReAct-自问自答发散": lambda t: len(t.steps) <= 6 and len(t.tool_calls) <= 6,
}

# 软提示（不判失败，避免关键词太脆 flaky）：礼貌收尾/反问类 case 检查 answer 是否有收尾语
_SOFT_KEYWORDS = {
    "ReAct-无意义输入防空轮": ("抱歉", "请问", "帮您", "需要", "可以"),
    "ReAct-歧义反问禁调工具": ("猫", "狗", "请问", "哪种", "什么"),
}


async def run_eval():
    print("=" * 80)
    print(f"ReAct 边界评测：{len(EVAL_SET)} 条，读 trace 断言过程（零付费裁判层）")
    print("=" * 80)

    passed = 0
    for i, case in enumerate(EVAL_SET, 1):
        name = case["name"]
        query = case["query"]
        session = AgentSession()
        answer = await session.chat(query)
        trace = session.get_last_trace()

        # 全 case 必断言：不死循环、不超步数
        ok_no_loop = trace.end_reason not in ("死循环", "超步数")
        ok_assert = ASSERTIONS[name](trace) if name in ASSERTIONS else True

        route_note = ""
        if trace.route_source != "LLM":
            route_note = f"（⚠️ 走了 {trace.route_source} 分支，被规则路由拦截，未测到 ReAct 循环）"

        ok = ok_no_loop and ok_assert
        if ok:
            passed += 1

        tools = [n for n, _, _, _ in trace.tool_calls]
        print(f"📍 [{i}/{len(EVAL_SET)}] {name}")
        print(f"    工具={tools} LLM步数={len(trace.steps)} end={trace.end_reason}{route_note}")
        soft = _SOFT_KEYWORDS.get(name)
        if soft and not any(k in answer for k in soft):
            print(f"   💡 软提示：answer 未含收尾/反问关键词 {soft}，answer={answer[:40]}")
        fail_reason = "死循环/超步数" if not ok_no_loop else "过程断言未过"
        print(f"   {'✅' if ok else '❌'} {'通过' if ok else '失败'}（{'' if ok else fail_reason}）")

    print("=" * 80)
    print(f"结果：{passed}/{len(EVAL_SET)} 条通过")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_eval())
