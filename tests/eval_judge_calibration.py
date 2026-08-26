# -*- coding: utf-8 -*-
"""裁判校准：验证 LLM 裁判 + 结构化层两条判定链路的职责边界

判两件事：
  1. struct 层（零付费）能否抓「有边界标记的硬事实编造」——假价格/假订单号/假品牌/假地点
  2. LLM 层（付费）能否抓「无边界标记的语义编造」——编造商品名/物流轨迹/政策条款

校准逻辑：先花小钱证明裁判可信，再大规模付费跑。「打分不精准≠功能缺陷，是提示词调优」。

用法：python tests/eval_judge_calibration.py（调 LLM 裁判，需用户明确说跑）
"""

import sys
import os
import asyncio

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# tests/ 目录（脚本所在目录）显式加进 path，让 `from eval_answer_quality import ...` 不依赖「脚本目录自动进 sys.path[0]」的隐式行为
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from judge import _judge, _structural_faithfulness
from cases import JUDGE_CALIBRATION_CASES as EVAL_SET


async def run_calibration():
    print("=" * 80)
    print(f"裁判校准：{len(EVAL_SET)} 条样本，验证 struct（硬事实）vs LLM（语义编造）两条链路")
    print("=" * 80)

    passed = 0
    for i, case in enumerate(EVAL_SET, 1):
        name = case["name"]
        query, reference, answer = case["query"], case["reference"], case["answer"]
        exp = case["expected"]

        # 两条独立链路分别判，不混判
        struct = _structural_faithfulness([query], answer)
        verdict = await _judge(query, answer, reference)

        parse_fail = verdict.get("parse_fail", False)
        acc = verdict.get("accuracy", -1)
        rel = verdict.get("relevance", -1)
        faith_llm = verdict.get("faithfulness", -1)
        struct_faithful = struct["faithful"]

        # 四项断言
        ok_acc = (not parse_fail) and acc == exp["accuracy"]
        ok_rel = (not parse_fail) and rel >= exp.get("relevance_min", -1) and rel <= exp.get("relevance_max", 6)
        ok_faith = (not parse_fail) and faith_llm == exp["faithfulness_llm"]
        ok_struct = struct_faithful == exp["struct_faithful"]

        ok = ok_acc and ok_rel and ok_faith and ok_struct
        if ok:
            passed += 1

        print(f"📍 [{i}/{len(EVAL_SET)}] {name}")
        print(f"    accuracy={acc}(期望{exp['accuracy']}) relevance={rel} faith_llm={faith_llm}(期望{exp['faithfulness_llm']}) struct_faithful={struct_faithful}(期望{exp['struct_faithful']})")
        if parse_fail:
            print(f"   ⚠️ 裁判解析失败：{verdict.get('reason', '')}")
        if struct["fake_orders"] or struct["fake_prices"] or struct["fake_brands"] or struct["fake_locations"]:
            print(f"   ⚠️ struct 抓到：订单号{struct['fake_orders']} 价格{struct['fake_prices']} 品牌{struct['fake_brands']} 地点{struct['fake_locations']}")
        print(f"   {'✅' if ok else '❌'} {'通过' if ok else '失败'}")

    print("=" * 80)
    print(f"结果：{passed}/{len(EVAL_SET)} 条通过")
    print("说明：夹带硬事实编造→struct 抓（faithfulness_llm 应=1）；夹带语义编造→LLM 抓（struct 应=True）。")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_calibration())
