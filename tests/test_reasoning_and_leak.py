# -*- coding: utf-8 -*-
"""P0/P1 修复的回归网（2026-09-20 那批）—— 钉住三件事：

  ① **摘要 / 抽取的请求形态**：指令必须**尾置**为最后一条 user 消息、且**关闭思考**。
     整个 P0 修复都押在这个形态上（推理 token 会吃光 max_tokens，导致 content 恒为空），
     而此前的测试全都 `mock.patch.object(agent, "_summarize")` 直接把它替换掉——
     **连 `_summarize` 内部都测不到**，这正是它没抓到线上失效的原因。

  ② **工具调用文本泄漏**：泄漏那一行**不能发给用户**；残留缓冲**必须 flush**
     （回答多半不以换行结尾，漏了这条会静默截掉最后一段，而且截尾后非空、兜底也判不到）。

  ③ **空摘要降级**：摘要真的为空时要降级到丢轮次（有损但有界），而不是静默保留原历史。

用法：python tests/test_reasoning_and_leak.py
"""

import sys
import os
import asyncio
from unittest import mock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import src.agent as agent
from src.config.prompts import SUMMARY_INSTRUCTION
from src.config.settings import REASONING_EFFORT_MECHANICAL

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ═══════════════════════════════════════════════════════════════
# ① 摘要 / 抽取的请求形态
# ═══════════════════════════════════════════════════════════════

async def test_summarize_request_shape():
    print("\n【① 摘要请求形态：指令尾置 + 关思考】")
    captured = {}

    async def fake_chat(msgs, **kw):
        captured["msgs"] = [dict(m) for m in msgs]
        captured["kw"] = kw
        r = mock.Mock()
        r.content = "摘要正文"
        return r, None

    # 两种历史尾角色都要成立：正常轮以 assistant 结尾；
    # ReAct 在死循环/超步数/异常等路径下不 append 最终 assistant，过滤后可能只剩 user。
    shapes = {
        "历史以 assistant 结尾": [
            {"role": "user", "content": "查询订单 20240818001"},
            {"role": "assistant", "content": "该订单已发货。"},
        ],
        "历史以 user 结尾（退化形态）": [
            {"role": "user", "content": "查一下订单 20240818001 的状态"},
        ],
    }
    for label, hist in shapes.items():
        captured.clear()
        with mock.patch.object(agent, "chat_with_usage", side_effect=fake_chat):
            text, _usage, _elapsed = await agent._summarize(list(hist))
        msgs, kw = captured["msgs"], captured["kw"]

        check(f"[{label}] 最后一条是 user 指令",
              msgs[-1]["role"] == "user" and msgs[-1]["content"] == SUMMARY_INSTRUCTION,
              f"实际 {msgs[-1]['role']} / {str(msgs[-1]['content'])[:30]!r}")
        check(f"[{label}] 没有把指令留在 system",
              not any(m["role"] == "system" and SUMMARY_INSTRUCTION in str(m.get("content", ""))
                      for m in msgs),
              f"system 仍有指令：{[m for m in msgs if m['role'] == 'system']}")
        check(f"[{label}] 用「以上」而非「以下」",
              "以上" in SUMMARY_INSTRUCTION and "以下客服对话历史" not in SUMMARY_INSTRUCTION)
        check(f"[{label}] 关闭思考（推理会吃光 max_tokens）",
              kw.get("reasoning_effort") == REASONING_EFFORT_MECHANICAL,
              f"实际 reasoning_effort={kw.get('reasoning_effort')!r}")
        check(f"[{label}] 历史被原样带上（没被就地改）",
              len(msgs) == len(hist) + 1, f"{len(msgs)} != {len(hist)}+1")
        check(f"[{label}] 返回摘要正文", text == "摘要正文", repr(text))


async def test_extract_request_shape():
    print("\n【① 画像抽取请求形态：关思考】")
    from src import memory as M
    captured = {}

    async def fake_chat(msgs, **kw):
        captured["kw"] = kw
        r = mock.Mock()
        r.content = "[]"
        return r, None

    with mock.patch("src.infra.llm.chat_with_usage", side_effect=fake_chat):
        await M.extract("我家养了一只金毛", "好的")
    check("抽取关闭思考（否则 400 预算被推理吃光、恒返回空）",
          captured.get("kw", {}).get("reasoning_effort") == REASONING_EFFORT_MECHANICAL,
          f"实际 {captured.get('kw', {}).get('reasoning_effort')!r}")


# ═══════════════════════════════════════════════════════════════
# ② 工具调用文本泄漏
# ═══════════════════════════════════════════════════════════════

def _mk_stream(chunks):
    async def _gen(*a, **kw):
        for c in chunks:
            yield c
    return _gen


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


async def test_leak_not_shown():
    print("\n【② 泄漏行不能发给用户】")
    chunks = [
        ("text", "好的，我帮您查一下三文鱼口味的成猫粮。\n"),
        ("text", "\n"),
        ("text", '`search_products(query="三文鱼 成猫粮")`'),   # 实测中用户看到的那一行
        ("usage", None),
    ]
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    with mock.patch.object(agent, "stream_events", _mk_stream(chunks)):
        evs = await _collect(agent._stream_generate(messages, None, on_leak="strip"))
    shown = "".join(p for k, p in evs if k == "text")
    check("泄漏行没进用户可见文本", "search_products" not in shown, repr(shown))
    check("过渡话术照常发出（只掐泄漏行，不整段丢）", "好的，我帮您查一下" in shown, repr(shown))


async def test_leak_json_form():
    print("\n【② JSON 形态的泄漏也要拦住】")
    chunks = [("text", '{"name": "search_products", "arguments": {"query": "猫粮"}}'), ("usage", None)]
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    with mock.patch.object(agent, "stream_events", _mk_stream(chunks)):
        evs = await _collect(agent._stream_generate(messages, None, on_leak="strip"))
    shown = "".join(p for k, p in evs if k == "text")
    check("JSON 形态泄漏被拦（初版正则漏检过这种）", "search_products" not in shown, repr(shown))


async def test_no_newline_flush():
    print("\n【② 流结束必须 flush 残留缓冲】")
    chunks = [("text", "这款猫粮很适合成猫，蛋白质含量高"), ("usage", None)]   # 无换行结尾
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    with mock.patch.object(agent, "stream_events", _mk_stream(chunks)):
        evs = await _collect(agent._stream_generate(messages, None, on_leak="strip"))
    shown = "".join(p for k, p in evs if k == "text")
    check("没有换行结尾时最后一段照常发出（不能静默截尾）",
          shown == "这款猫粮很适合成猫，蛋白质含量高", repr(shown))
    check("历史回填完整", messages[-1].get("content") == "这款猫粮很适合成猫，蛋白质含量高",
          repr(messages[-1]))


async def test_leak_react_degrade():
    print("\n【② 泄漏 → 降级重跑 ReAct（无已执行工具的路径）】")
    chunks = [("text", "好的我帮您查一下。\n"),
              ("text", 'search_products(query="x")'),
              ("usage", None)]
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    called = {"react": 0}

    async def fake_react(msgs, trace=None):
        called["react"] += 1
        yield ("text", "【降级后的真实回答】")

    with mock.patch.object(agent, "stream_events", _mk_stream(chunks)), \
         mock.patch.object(agent, "_react_loop_stream", fake_react):
        evs = await _collect(
            agent._stream_generate(messages, None, guide="【路由引导：总结】请反问", on_leak="react"))
    shown = "".join(p for k, p in evs if k == "text")
    check("走了 ReAct 降级", called["react"] == 1, f"调用 {called['react']} 次")
    check("用户看到的是降级后的回答", "【降级后的真实回答】" in shown, repr(shown))
    check("泄漏行仍然没发出去", "search_products" not in shown, repr(shown))
    check("guide 已被撤掉（否则与降级目的正相反）",
          not any("路由引导" in str(m.get("content", "")) for m in messages),
          repr(messages))


async def test_leak_routed_does_not_rerun_react():
    print("\n【② 工具已执行的路径禁止重跑 ReAct（会重复决策）】")
    chunks = [("text", '`search_orders(order_id="20240818001")`'), ("usage", None)]
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "【规则路由已执行工具 search_orders，结果如下…】"}]
    called = {"react": 0}

    async def fake_react(msgs, trace=None):
        called["react"] += 1
        yield ("text", "不该出现")

    with mock.patch.object(agent, "stream_events", _mk_stream(chunks)), \
         mock.patch.object(agent, "_react_loop_stream", fake_react):
        await _collect(agent._stream_generate(messages, None, on_leak="regenerate"))
    check("没有重跑 ReAct", called["react"] == 0, f"调用了 {called['react']} 次")


# ═══════════════════════════════════════════════════════════════
# ③ 空摘要降级
# ═══════════════════════════════════════════════════════════════

async def test_empty_summary_falls_back_to_trim():
    print("\n【③ 摘要为空 → 降级丢轮次（有损但有界）】")
    messages = [{"role": "system", "content": "sys"}]
    for i in range(6):
        messages.append({"role": "user", "content": f"第{i}个问题" * 200})
        messages.append({"role": "assistant", "content": f"第{i}个回答" * 200})
    messages.append({"role": "user", "content": "当前这轮"})
    before = agent._messages_tokens(messages)
    n_before = len([m for m in messages if m["role"] == "user"])

    async def empty_summarize(h):
        return "", None, 0.0

    with mock.patch.object(agent, "_summarize", empty_summarize):
        out = await agent._compress_history(messages, 500, trace=None)
    after = agent._messages_tokens(out)
    n_after = len([m for m in out if m["role"] == "user"])
    check("历史真的被压小了（不是静默原样保留）", after < before, f"{before} → {after}")
    check("轮次确实减少了", n_after < n_before, f"{n_before} → {n_after}")


async def test_normal_summary_keeps_entities():
    print("\n【③ 摘要正常时保留关键实体（区别于丢轮次）】")
    messages = [{"role": "system", "content": "sys"}]
    for i in range(6):
        messages.append({"role": "user", "content": f"问题{i}" * 200})
        messages.append({"role": "assistant", "content": f"回答{i}" * 200})
    messages.append({"role": "user", "content": "当前轮"})

    async def good_summarize(h):
        r = mock.Mock()
        return "用户查询过订单 20240818001，尚未得到答复。", None, 0.0

    with mock.patch.object(agent, "_summarize", good_summarize):
        out = await agent._compress_history(messages, 500, trace=None)
    text = "".join(str(m.get("content", "")) for m in out)
    check("注入的是摘要消息（带 SUMMARY_PREFIX）", agent.SUMMARY_PREFIX in text)
    check("关键实体（订单号）被保留", "20240818001" in text)


async def main():
    await test_summarize_request_shape()
    await test_extract_request_shape()
    await test_leak_not_shown()
    await test_leak_json_form()
    await test_no_newline_flush()
    await test_leak_react_degrade()
    await test_leak_routed_does_not_rerun_react()
    await test_empty_summary_falls_back_to_trim()
    await test_normal_summary_keeps_entities()
    print("\n" + "-" * 60)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return FAIL


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
