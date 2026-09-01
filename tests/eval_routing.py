# -*- coding: utf-8 -*-
"""意图路由评测：量化 route_by_rule 命中率 + 数据飞轮（失败 case 自动回流 + 复测毕业）

用法：python tests/eval_routing.py

与 eval_retrieval.py 对称：检索线测「召回对不对」（Recall/MRR），本脚本测「路由对不对」（三元组对比）。
纯规则层、零依赖（不调 embedding/LLM/后端），失败判据 = 程序化 (kind, tool, order_id) 对比，无 LLM 打分。

指标：
  路由命中率 —— route_by_rule 输出与期望三元组一致的占比
  数据飞轮   —— 失败 case 回流 regression_routing.json，复测通过毕业（复用 regression.py，name="routing"）

为什么需要这条线：数据飞轮原本只在检索线（regression_retrieval.json 兜「召回错」），
兜不到「路由错」——被 browse 劫持/订单泛词吞写意图这类 case 不产生召回失败，进不了池。
路由层坏 case 之前只能靠人发现 + 手动补词，这里给它补上对称的自动回流闭环。
"""

import sys
import os
import time

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.intent_router import route_by_rule
import regression
from cases import ROUTING_CASES as EVAL_SET


def _parse(result):
    """把 route_by_rule 返回值解析成 (kind, tool, order_id) 三元组，方便与期望对比。

    None → (None, None, None)（交 LLM）
    ("tool", name, args) → ("tool", name, args.get("order_id"))
    ("browse"/"summarize", None, None) → (kind, None, None)
    """
    if result is None:
        return (None, None, None)
    kind = result[0]
    if kind == "tool":
        tool = result[1]
        args = result[2] or {}
        return ("tool", tool, args.get("order_id"))
    return (kind, None, None)


def run_eval():
    failures = []
    passed = 0
    total = len(EVAL_SET)

    print("=" * 70)
    print("意图路由评测（route_by_rule 命中率 + 数据飞轮）")
    print("-" * 70)

    for name, query, exp_kind, exp_tool, exp_oid in EVAL_SET:
        got = _parse(route_by_rule(query))
        expect = (exp_kind, exp_tool, exp_oid)
        ok = (got == expect)
        if ok:
            passed += 1
        else:
            # 归类失败类型：kind 不对=路由错；kind 对 tool 不对=工具错；其余=参数错
            if got[0] != exp_kind:
                fail_type = "路由错"
            elif got[1] != exp_tool:
                fail_type = "工具错"
            else:
                fail_type = "参数错"
            failures.append({
                "query": query,
                "expected": list(expect),
                "fail_type": fail_type,
                "first_actual": str(got),
                "note": name,
                "added_at": time.strftime("%Y-%m-%d"),
            })
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}: 期望 {expect} → 实际 {got}")

    print("-" * 70)
    print(f"路由命中率 = {passed}/{total} = {passed/total:.2%}")
    print("=" * 70)

    _run_regression(failures)


def _run_regression(failures):
    """数据飞轮：① 失败 case 自动回流进池 ② 池 open case 复测（回归）③ fix 型毕业 / lock 型防误修"""
    # 先取池 open case（本次回流前），再回流——避免本次刚失败的 case 又复测一遍（和检索线同规范）
    pool_cases = regression.open_cases("routing")
    added = regression.add_failures("routing", failures)

    print()
    print("=" * 70)
    print(f"数据飞轮：本次回流 +{added} 条 → 回归池 open {len(pool_cases)} 条（不含本次）")
    print("-" * 70)
    if not pool_cases:
        print("📊 回归池：空（无待回归 case）")
        print("=" * 70)
        return

    passed = set()
    for c in pool_cases:
        query = c["query"]
        exp = c.get("expected") or []
        expect = tuple(exp) if isinstance(exp, (list, tuple)) else (exp,)
        got = _parse(route_by_rule(query))

        if c.get("expect_type") == "lock":
            # 行为锁定：锁「当前应保持的路由行为」（修复后固化），防误改词表让行为漂移
            first_actual = c.get("first_actual", "")
            if str(got) == first_actual:
                print(f"  ✅ [lock 锁定] {query}：保持（{got}）")
            else:
                print(f"  ⚠️ [lock 漂移] {query}：{first_actual} → {got}，行为变了，检查路由词表")
        else:  # fix
            if got == expect:
                passed.add(query)
                print(f"  ✅ [fix 修复] {query}：{got} 达标")
            else:
                print(f"  ❌ [fix 仍坏] {query}：期望 {expect}，实际 {got}")

    graduated = regression.graduate("routing", passed)
    if graduated:
        print(f"🎓 毕业 {len(graduated)} 条：{graduated}")
    open_left = len(regression.open_cases("routing"))
    print(f"📊 回归汇总：open {len(pool_cases)} / 通过 {len(passed)} / 毕业 {len(graduated)} / 剩余 open {open_left}")
    print("=" * 70)


if __name__ == "__main__":
    run_eval()
