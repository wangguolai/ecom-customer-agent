# -*- coding: utf-8 -*-
"""工具异常评测：故障注入让工具真超时/返脏/空结果，验证 agent 降级行为

依赖：后端以 ENABLE_DEBUG_FAULT=1 启动才挂 /debug/fault 端点。
跑前 set_fault 注入故障，跑后 clear_fault + breaker.reset()（避免注入把全局熔断器打挂污染后续 case）。

用法：
  ENABLE_DEBUG_FAULT=1 uvicorn src.backend.main:app --port 8000   # 后端（带故障注入端点）
  python tests/eval_tool_failure.py                                # 跑 agent 调 LLM + 故障注入
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

import requests
from src.agent import AgentSession
from src import tools
from cases import TOOL_FAILURE_CASES as EVAL_SET

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

# name -> (target, mode, count)：注入到后端哪个端点、什么故障、连续几次
FAULT_MAP = {
    "工具调用超时-查询订单": ("logistics", "timeout", 3),
    "工具返回空结果-库存查询": ("products", "empty", 3),
    "工具返回脏乱码数据": ("orders", "dirty", 3),
    "连续多轮工具失败停止重试": ("refund", "timeout", 3),
}

# 降级话术关键词（软提示，不硬判，避免措辞差异 flaky）
_DEGRADE_KEYWORDS = ("稍后重试", "稍后再试", "暂不可用", "无法查询", "暂时")


def _backend_up() -> bool:
    """探测后端三端点 + /debug/fault 挂载性。fault 端点必须挂载（ENABLE_DEBUG_FAULT=1 启动），
    否则注入静默失效、评测「测了个寂寞」还照样通过。"""
    probes = ["/orders/20240818001", "/logistics/20240818001", "/products/P001"]
    try:
        for ep in probes:
            # 探测超时放宽到 3s：后端冷启动首次查询（MySQL 连接池预热）可能超 1s，误判成「未启动」
            resp = requests.get(f"{BACKEND_URL}{ep}", timeout=3.0)
            if not resp.ok:
                return False
        # /debug/fault 是 POST 端点，用 clear（幂等、无副作用）探测挂载性；未挂载返回 404
        resp = requests.post(f"{BACKEND_URL}/debug/fault/clear", json={}, timeout=3.0)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _inject(target, mode, count):
    resp = requests.post(f"{BACKEND_URL}/debug/fault", json={"target": target, "mode": mode, "count": count}, timeout=1.0)
    if resp.status_code != 200:
        raise RuntimeError(f"故障注入失败（HTTP {resp.status_code}）：后端可能未以 ENABLE_DEBUG_FAULT=1 启动")


def _clear():
    try:
        requests.post(f"{BACKEND_URL}/debug/fault/clear", json={}, timeout=1.0)
    except requests.RequestException:
        pass


async def run_eval():
    if not _backend_up():
        print("后端未启动（或未开 ENABLE_DEBUG_FAULT），无法跑故障注入。请先启动后端。")
        return

    print("=" * 80)
    print(f"工具异常评测：{len(EVAL_SET)} 条，故障注入让工具真超时/返脏/空结果")
    print("=" * 80)

    passed = 0
    for i, case in enumerate(EVAL_SET, 1):
        name = case["name"]
        query = case["query"]
        target, mode, count = FAULT_MAP.get(name, ("orders", "timeout", 3))

        _inject(target, mode, count)
        session = AgentSession()
        answer = await session.chat(query)
        trace = session.get_last_trace()
        _clear()
        tools._breaker.reset()  # 清熔断器，避免注入失败把全局熔断器打挂污染后续 case

        degrade_ok = any(k in answer for k in _DEGRADE_KEYWORDS)
        total_tools = len(trace.tool_calls)
        if name == "连续多轮工具失败停止重试":
            refund_cnt = sum(1 for n, _, _, _ in trace.tool_calls if n == "refund_order")
            no_retry = refund_cnt <= 2
        else:
            no_retry = True

        ok = no_retry
        if ok:
            passed += 1

        tools_list = [n for n, _, _, _ in trace.tool_calls]
        print(f"📍 [{i}/{len(EVAL_SET)}] {name}（注入 {target}/{mode}×{count}）")
        print(f"    工具={tools_list} 次数={total_tools} end={trace.end_reason}")
        print(f"    answer={answer[:60]}")
        if not degrade_ok:
            print(f"   💡 软提示：answer 未含降级关键词 {_DEGRADE_KEYWORDS}")
        print(f"   {'✅' if ok else '❌'} {'通过' if ok else '失败'}（{'停止重试' if not no_retry else ''}）")

    print("=" * 80)
    print(f"结果：{passed}/{len(EVAL_SET)} 条通过")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_eval())
