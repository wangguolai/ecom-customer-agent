# -*- coding: utf-8 -*-
"""流式输出单元测试（零付费：mock 流式 chunk，不真调 LLM）

验证：
1. stream_events：text delta 逐段产出 + tool_calls 分片累积拼接（arguments JSON 分片、name 只首片）
2. _react_loop_stream：答案轮逐 token yield + 决策轮工具执行
"""

import sys
import os
from types import SimpleNamespace
from unittest.mock import patch

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.infra.llm as llm
import src.agent as agent
from src.infra.observability import Trace


_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {detail}")


# ── fake stream（openai 兼容 chunk）──
def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _chunk(delta):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tc(index, id=None, name=None, args=None):
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    async def create(self, **kwargs):
        return _FakeStream(self._chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))


async def _collect(agen):
    return [x async for x in agen]


async def test_stream_events_text():
    chunks = [_chunk(_delta("你")), _chunk(_delta("好"))]
    with patch.object(llm, "get_client", return_value=_FakeClient(chunks)):
        events = await _collect(llm.stream_events([{"role": "user", "content": "hi"}]))
    check("stream_events text 分片逐段产出", events == [("text", "你"), ("text", "好")], str(events))


async def test_stream_events_tool_calls():
    # arguments 分两片、name/id 只在首片 —— 模拟真实分片累积
    chunks = [
        _chunk(_delta(tool_calls=[_tc(0, id="call_1", name="search_orders", args='{"order')])),
        _chunk(_delta(tool_calls=[_tc(0, args='_id":"20240818001"}')])),
    ]
    with patch.object(llm, "get_client", return_value=_FakeClient(chunks)):
        events = await _collect(llm.stream_events([{"role": "user", "content": "查订单"}]))
    expected = [("tool_calls", [{"id": "call_1", "name": "search_orders", "arguments": '{"order_id":"20240818001"}'}])]
    check("stream_events tool_calls 分片累积拼接", events == expected, str(events))


async def _gen_answer(messages=None, tools=None, max_tokens=None):
    yield ("text", "订")
    yield ("text", "单状态正常")


async def test_react_loop_stream_answer():
    msgs = [{"role": "system", "content": "sys"}]
    trace = Trace()
    got = []
    with patch.object(agent, "stream_events", side_effect=_gen_answer):
        async for _kind, _payload in agent._react_loop_stream(msgs, trace):
            if _kind == "text":
                got.append(_payload)
    check("答案轮逐 token yield", got == ["订", "单状态正常"], str(got))
    check("答案轮 trace 正常结束", trace.end_reason == "正常", trace.end_reason)


async def _gen_tool_call(messages=None, tools=None, max_tokens=None):
    yield ("tool_calls", [{"id": "call_1", "name": "search_orders", "arguments": '{"order_id":"20240818001"}'}])


async def test_react_loop_stream_tool_exec():
    msgs = [{"role": "system", "content": "sys"}]
    trace = Trace()
    calls = {"n": 0}

    async def fake_search_orders(order_id):
        calls["n"] += 1
        return "订单 20240818001：状态=已发货"

    # 第一轮 stream_events 返回 tool_calls（决策轮），第二轮返回答案。
    # side_effect 列表元素必须是「已创建的 async generator 对象」——列表元素若是 callable，
    # mock 直接返回函数对象本身而非调用结果（async for 拿到函数对象报 TypeError）。
    responses = [_gen_tool_call(), _gen_answer()]
    with patch.dict(agent.TOOL_MAP, {"search_orders": fake_search_orders}), \
         patch.object(agent, "stream_events", side_effect=responses):
        got = []
        async for _kind, _payload in agent._react_loop_stream(msgs, trace):
            if _kind == "text":
                got.append(_payload)
    check("决策轮执行工具", calls["n"] == 1, str(calls))
    check("决策轮后答案逐 token yield", got == ["订", "单状态正常"], str(got))
    check("trace 正常结束", trace.end_reason == "正常", trace.end_reason)


def _usage_chunk(total=10, prompt=9, completion=1, hit=0, miss=9):
    """带 usage 的 chunk。

    刻意构造成 **choices 非空**——这是实测到的 DeepSeek 真实形态（usage 绑在最后一个
    内容块上），不是 OpenAI 标准的「空 choices 块」。两种形态解析路径不同，测试要贴真实。
    """
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=_delta(content=None))],
        usage=SimpleNamespace(
            total_tokens=total, prompt_tokens=prompt, completion_tokens=completion,
            prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=miss,
        ),
    )


async def test_stream_events_usage():
    """usage 事件必须产出，且**排在 tool_calls 之后**（调用方靠顺序区分决策/答案轮）"""
    chunks = [
        _chunk(_delta(tool_calls=[_tc(0, id="c1", name="search_orders", args="{}")])),
        _usage_chunk(),
    ]
    with patch.object(llm, "get_client", return_value=_FakeClient(chunks)):
        events = await _collect(llm.stream_events([{"role": "user", "content": "x"}]))
    kinds = [k for k, _ in events]
    check("stream_events 产出 usage 事件", "usage" in kinds, str(kinds))
    check("usage 排在 tool_calls 之后", kinds == ["tool_calls", "usage"], str(kinds))


async def _gen_answer_with_usage(messages=None, tools=None, max_tokens=None):
    yield ("text", "订")
    yield ("text", "单状态正常")
    yield ("usage", SimpleNamespace(total_tokens=10, prompt_tokens=9,
                                    prompt_cache_hit_tokens=3, prompt_cache_miss_tokens=6))


async def test_react_loop_stream_answer_with_usage():
    """🔴 回归守卫：答案轮收到 usage 必须**不崩**。

    这是 `include_usage` 引入时最容易炸的地方，而现有用例全都 mock 成不产 usage，
    所以**一条都测不到**。改之前的写法 `else: tool_calls_list = ev[1]` 会把 usage 对象
    塞进 tool_calls_list，而 `if not tool_calls_list` 对 pydantic 对象判为假 →
    走错分支（决策轮）→ 崩。答案轮是主路，等于「一开 include_usage 所有流式回答都炸」。
    """
    msgs = [{"role": "system", "content": "sys"}]
    trace = Trace()
    got = []
    with patch.object(agent, "stream_events", side_effect=_gen_answer_with_usage):
        try:
            async for _kind, _payload in agent._react_loop_stream(msgs, trace):
                if _kind == "text":
                    got.append(_payload)
        except Exception as e:  # noqa: BLE001 —— 崩了就是这条用例要抓的
            check("答案轮遇 usage 不崩", False, f"{type(e).__name__}: {e}")
            return
    check("答案轮遇 usage 不崩且文本正常", got == ["订", "单状态正常"], str(got))
    check("答案轮 trace 正常结束", trace.end_reason == "正常", trace.end_reason)
    check("usage 回填进 trace（token 不再是 0）",
          sum(s[2] for s in trace.steps) == 10, str(trace.steps))


def main():
    import asyncio
    print("=" * 60)
    print("流式输出单元测试（mock 流式，零付费）")
    print("-" * 60)
    asyncio.run(test_stream_events_text())
    asyncio.run(test_stream_events_tool_calls())
    asyncio.run(test_stream_events_usage())
    asyncio.run(test_react_loop_stream_answer())
    asyncio.run(test_react_loop_stream_answer_with_usage())
    asyncio.run(test_react_loop_stream_tool_exec())
    print("-" * 60)
    print(f"通过 {_passed} / 失败 {_failed}")
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
