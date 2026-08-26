# -*- coding: utf-8 -*-
"""回答质量评测（baseline）：端到端跑 agent，判「准确率 + 忠实度」，结果落盘可 diff

定位：本地回归测试脚本（不是「跑一次看个数的诊断脚本」）。
  短期：asyncio 改造前的 baseline。改造后 `--out after` 重跑，`--diff` 对比证明「异步化没改坏回答质量」。
  长期：演进成「Agent Eval 回归集」，后续每次后端改造（MySQL/Redis/高可用）都拿它回归。

忠实度判法（选项 A：结构化 + LLM 分层）：
  结构化层（确定性规则，非 LLM）—— 从回答抽取「订单号 / 价格」，和事实库精确比对，抓「编造硬事实」。
  语义层（LLM）—— 判「推荐对不对（accuracy）+ 语义层面有无编造（faithfulness）」。

用法：
  python tests/eval_answer_quality.py              # 跑 baseline，落盘 eval_results/answer_quality_baseline.json
  python tests/eval_answer_quality.py --out after  # 改造后跑，落盘 ..._after.json
  python tests/eval_answer_quality.py --diff       # 对比 baseline vs after 的逐 case 差异

指标：
  准确率（accuracy）    —— 回答正确回应问题的 case 比例
  忠实度（faithfulness） —— 结构化层无编造硬事实 AND 语义层无编造
依赖：订单/库存/物流类 case 依赖后端（先 uvicorn src.backend.main:app --port 8000，否则 SKIP）
"""

import sys
import os
import json
import re
import time
import asyncio
from urllib.parse import quote

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from src.agent import AgentSession
from judge import _judge, _structural_faithfulness
from cases import ANSWER_QUALITY_CASES as EVAL_SET

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
RESULT_DIR = os.path.join(_project_root, "tests", "eval_results")

# 事实库 / 裁判 / 结构化层判定已抽到 tests/judge.py（eval_judge_calibration.py 共用，避免拉起 agent 链）
# 回答质量评测集已集中到 cases.py（ANSWER_QUALITY_CASES），此处不再内联定义。


def _backend_up() -> bool:
    """探测后端三端点（orders/logistics/stock），全部返回 200 才算在线。
    except requests.RequestException 覆盖 timeout/conn；检查 resp.ok 避免「端口被占但后端坏/种子缺失」误判在线。"""
    probes = ["/orders/20240818001", "/logistics/20240818001", "/products/P001"]
    for ep in probes:
        try:
            resp = requests.get(f"{BACKEND_URL}{ep}", timeout=1.0)
            if not resp.ok:
                return False
        except requests.RequestException:
            return False
    return True


async def _run_case_once(name: str, turns: list, display_query: str, expected: str) -> dict:
    """单次跑一个 case：跑 agent（多步 ReAct）+ 结构化层 + 语义裁判，返回单次结果。

    异常不中断（和裁判异常同一处理路径），记 parse_fail 继续。
    """
    try:
        session = AgentSession()
        for t in turns:
            answer = await session.chat(t)
        trace = session.get_last_trace()
        end_reason = trace.end_reason if trace else "未知"
        struct = _structural_faithfulness(turns, answer)
        verdict = await _judge(display_query, answer, expected)
    except Exception as e:
        answer = f"（评测异常：{type(e).__name__}）"
        trace = None
        end_reason = "异常"
        struct = {"faithful": True, "fake_orders": [], "fake_prices": [], "fake_brands": [], "fake_locations": []}
        verdict = {"parse_fail": True, "reason": f"评测异常: {type(e).__name__}"}

    # 解析失败/字段非法 → 显式报错 + 按 0 计入（不静默从分母剔除，避免「裁判崩了」伪装成「没这个 case」）
    parse_fail = verdict.get("parse_fail", False)
    llm_acc = 0 if parse_fail else verdict["accuracy"]
    relevance = 0 if parse_fail else verdict.get("relevance", 0)
    llm_faith = 0 if parse_fail else verdict["faithfulness"]
    # faithfulness 分层：struct 抓硬事实（订单号/价格/品牌/物流地点）AND LLM 判语义编造，两层都忠实才算忠实
    final_faith = 1 if (llm_faith and struct["faithful"]) else 0

    return {
        "answer": answer, "accuracy": llm_acc, "faithfulness": final_faith, "relevance": relevance, "relevant": 1 if relevance >= 3 else 0,
        "faithfulness_llm": llm_faith, "faithfulness_struct": 1 if struct["faithful"] else 0,
        "fake_orders": struct["fake_orders"], "fake_prices": struct["fake_prices"], "fake_brands": struct["fake_brands"], "fake_locations": struct["fake_locations"],
        "end_reason": end_reason, "parse_fail": parse_fail,
        "reason": verdict.get("reason", ""),
        "usage": verdict.get("usage", {}),  # 裁判 token 消耗（成本可见）
        "trace": trace.summary() if trace else {},  # 过程指标全量落盘（工具次数/路由/缓存/token 等），一次付费多产出
    }


async def run_eval(out_suffix: str = "baseline", only_names=None, runs: int = 3):
    backend_up = _backend_up()
    print("=" * 80)
    print(f"后端状态：{'在线' if backend_up else '离线（订单/库存/物流类 case 将 SKIP）'}")
    print(f"采样方式：每个 case 跑 {runs} 次取多数票（消除单次采样噪声）")
    print("=" * 80)

    eval_cases = EVAL_SET if not only_names else [c for c in EVAL_SET if c["name"] in only_names]
    cases, skipped = [], []
    for i, case in enumerate(eval_cases, 1):
        name = case["name"]
        expected, needs_backend = case["expected"], case["needs_backend"]
        # turns：单轮 = [query]，多轮 = [t1, t2, ...]；answer 取最后一轮（指代消解考的是最后一轮）。
        # 边界：多轮 case 只判最后一轮 answer，前几轮答对答错不计分；要测多轮全链路另开专项评测。
        turns = case.get("turns", [case.get("query", "")])
        display_query = case.get("query") or " → ".join(turns)
        if needs_backend and not backend_up:
            skipped.append(name)
            print(f"📍 [{i}/{len(eval_cases)}] {name} — SKIP（后端未启动）")
            continue

        runs_detail = []
        for r in range(1, runs + 1):
            print(f"📍 [{i}/{len(eval_cases)}] {name} — 第 {r}/{runs} 次...")
            once = await _run_case_once(name, turns, display_query, expected)
            once["run"] = r
            runs_detail.append(once)
            print(f"    acc={once['accuracy']} faith={once['faithfulness']} | {once['reason'][:60]}")
            if once["parse_fail"]:
                print(f"   ⚠️ 解析失败：{once['reason']}")
            if once["fake_orders"] or once["fake_prices"] or once["fake_brands"] or once["fake_locations"]:
                print(f"   ⚠️ 结构化层抓到编造：订单号{once['fake_orders']} 价格{once['fake_prices']} 品牌{once['fake_brands']} 地点{once['fake_locations']}")

        # 多数票：> N/2 才算通过（N=3 时 2 票通过，1 票不通过）
        acc_votes = sum(1 for d in runs_detail if d["accuracy"])
        faith_votes = sum(1 for d in runs_detail if d["faithfulness"])
        parse_fail_count = sum(1 for d in runs_detail if d["parse_fail"])
        final_acc = 1 if acc_votes * 2 > runs else 0
        final_faith = 1 if faith_votes * 2 > runs else 0
        # 取「代表多数票」的一轮 answer 展示（优先取 acc/faith 都和多数票一致的，否则取第一轮）
        rep = next((d for d in runs_detail if d["accuracy"] == final_acc and d["faithfulness"] == final_faith), runs_detail[0])

        record = {
            "name": name, "query": display_query, "turns": turns,
            "answer": rep["answer"], "expected": expected,
            "accuracy": final_acc, "faithfulness": final_faith,
            "relevance": rep["relevance"], "relevant": rep["relevant"],
            "accuracy_votes": f"{acc_votes}/{runs}", "faithfulness_votes": f"{faith_votes}/{runs}",
            "runs": runs_detail,
            "end_reason": " | ".join(d["end_reason"] for d in runs_detail),
            "parse_fail": parse_fail_count,
        }
        cases.append(record)
        print(f"    👉 多数票: accuracy={acc_votes}/{runs} faithfulness={faith_votes}/{runs}")

    total = len(cases)
    acc_sum = sum(c["accuracy"] for c in cases)
    faith_sum = sum(c["faithfulness"] for c in cases)
    rel_sum = sum(c["relevance"] for c in cases)
    relevant_sum = sum(c["relevant"] for c in cases)
    parse_fail_count = sum(c["parse_fail"] for c in cases)

    summary = {
        "total": total, "skipped": len(skipped), "parse_fail": parse_fail_count,
        "runs": runs,
        "accuracy": round(acc_sum / total, 4) if total else 0,
        "faithfulness": round(faith_sum / total, 4) if total else 0,
        "relevance": round(rel_sum / total, 2) if total else 0,  # 平均相关性（0-5）
        "relevant_rate": round(relevant_sum / total, 4) if total else 0,  # 相关率（≥3 的比例）
    }

    print("=" * 80)
    print(f"准确率   accuracy     = {acc_sum}/{total} = {summary['accuracy']:.2%}")
    print(f"忠实度   faithfulness = {faith_sum}/{total} = {summary['faithfulness']:.2%}")
    print(f"相关性   relevance    = {summary['relevance']}/5（相关率 {summary['relevant_rate']:.2%}）")
    if skipped:
        print(f"SKIP（后端离线）：{len(skipped)} 条 {skipped}")
    if parse_fail_count:
        print(f"⚠️ 解析失败（{runs} 次采样累计）：{parse_fail_count} 次")
    print("=" * 80)
    print(f"✅ baseline 已定稿：每个 case 跑 {runs} 次取多数票，结果可信。")

    _save_results(summary, cases, out_suffix)


def _save_results(summary: dict, cases: list, out_suffix: str):
    """落盘结果 JSON（跨时间对比的可 diff 产物）"""
    os.makedirs(RESULT_DIR, exist_ok=True)
    path = os.path.join(RESULT_DIR, f"answer_quality_{out_suffix}.json")
    payload = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "summary": summary, "cases": cases}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"📁 结果已落盘：{path}")


def diff(before_path: str, after_path: str):
    """对比 baseline vs after：打印 summary 差异 + 逐 case 变化"""
    for p in (before_path, after_path):
        if not os.path.exists(p):
            print(f"❌ 结果文件不存在：{p}")
            print("   请先 `python tests/eval_answer_quality.py` 跑 baseline，再 `--out after` 跑改造后。")
            return
    with open(before_path, encoding="utf-8") as f:
        before = json.load(f)
    with open(after_path, encoding="utf-8") as f:
        after = json.load(f)

    print("=" * 80)
    print(f"summary 对比：before={before['summary']} → after={after['summary']}")
    print("-" * 80)
    before_cases = {c["name"]: c for c in before["cases"]}
    after_cases = {c["name"]: c for c in after["cases"]}
    changed = 0
    for name in before_cases:
        b, a = before_cases[name], after_cases.get(name)
        if a is None:
            print(f"[消失] {name}：after 里没有这个 case")
            changed += 1
            continue
        if (b["accuracy"], b["faithfulness"]) != (a["accuracy"], a["faithfulness"]):
            print(f"[变化] {name}: acc {b['accuracy']}→{a['accuracy']}, faith {b['faithfulness']}→{a['faithfulness']} | end_reason {b['end_reason']}→{a['end_reason']}")
            changed += 1
    for name in after_cases:
        if name not in before_cases:
            print(f"[新增] {name}：before 里没有这个 case")
            changed += 1
    if changed == 0:
        print("✅ 无退化：所有 case 的 accuracy/faithfulness 前后一致。")
    print("=" * 80)


if __name__ == "__main__":
    if "--diff" in sys.argv:
        diff(
            os.path.join(RESULT_DIR, "answer_quality_baseline.json"),
            os.path.join(RESULT_DIR, "answer_quality_after.json"),
        )
    else:
        # --out 接受值：--out baseline 落盘 baseline，--out after 落盘 after，裸 --out 默认 after（保持旧开关行为）
        out_suffix = "baseline"
        if "--out" in sys.argv:
            idx = sys.argv.index("--out")
            if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
                out_suffix = sys.argv[idx + 1]
            else:
                out_suffix = "after"
        only_names = None
        runs = 3
        if "--only" in sys.argv:
            idx = sys.argv.index("--only")
            if idx + 1 < len(sys.argv):
                only_names = sys.argv[idx + 1].split(",")
        if "--runs" in sys.argv:
            idx = sys.argv.index("--runs")
            if idx + 1 < len(sys.argv):
                runs = int(sys.argv[idx + 1])
        asyncio.run(run_eval(out_suffix, only_names, runs))
