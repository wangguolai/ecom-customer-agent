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
from src.infra.llm import chat_with_usage

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
RESULT_DIR = os.path.join(_project_root, "tests", "eval_results")

# ═══════════════════════════════════════════════════════════════
# 事实库（结构化层 ground truth，从 data/products.md + src/backend/main.py 提取）
# ═══════════════════════════════════════════════════════════════
FACTS = {
    # 合法订单号（backend _SEED_ORDERS）
    "order_ids": {"20240818001", "20240817002", "20240816003"},
    # 合法价格数值（products.md 全部价格 + backend 库存价格，去重）
    "prices": {
        15, 18, 19, 22, 25, 28, 29, 32, 35, 39, 45, 49, 55, 59, 69, 79, 89,
        99, 109, 119, 129, 139, 149, 159, 219, 259, 289, 299, 319, 359, 379,
        389, 399, 459,
    },
}

# ═══════════════════════════════════════════════════════════════
# 裁判（语义层）：判 accuracy + faithfulness
# 硬事实（订单号/价格）的编造不归 LLM 判，由结构化层精确比对——让两个维度正交：
#   accuracy 只判「答对没」，faithfulness 只判「语义编造没」，不混「该拒答却编造」进 accuracy。
# ═══════════════════════════════════════════════════════════════
JUDGE_SYSTEM = """你是电商客服 Agent 回答质量的评测裁判。根据「用户问题」「客服回答」「参考答案」判两个维度：

1. accuracy（准确率，0/1）：客服回答是否正确回应了用户问题、是否与参考答案一致（答对了该答的）。
2. faithfulness（忠实度，0/1）：客服回答在语义层面是否忠实、没有凭空编造。重点看：有没有编造不存在的商品名、编造库存状态（有货/缺货说反）、编造物流轨迹、编造政策条款。

注意：回答里的「订单号」「价格数值」这类硬事实的编造不归你判，由外部规则层精确比对。你只判语义层面的编造。

评分规则：
- accuracy=1：回答正确回应了用户问题、与参考答案一致；accuracy=0：答非所问、答错、漏答关键信息。
- faithfulness=1：语义层面没有凭空编造；faithfulness=0：编造了不存在的商品 / 状态 / 轨迹 / 政策条款。

只输出一个 JSON 对象，不要 markdown 代码块、不要任何多余文字，格式：
{"accuracy": 0或1, "faithfulness": 0或1, "reason": "一句话理由"}
"""

# 评测集：query -> 参考答案（expected 是「回答应包含的关键事实」，供语义层裁判判 accuracy/faithfulness）
# needs_backend=True 的 case 依赖后端，后端离线时 SKIP（不计分，避免把「环境问题」误判成「答错」）
EVAL_SET = [
    # ═══ 模糊语义（信息不足，正确行为是反问澄清）═══
    {"name": "模糊-猫狗", "query": "肠胃不好，猫和狗吃啥好", "expected": "反问是猫还是狗还是都养（信息不足），不默认推荐某一种", "needs_backend": False},
    {"name": "模糊-掉毛", "query": "掉毛特别厉害，吃啥能缓解", "expected": "反问是猫还是狗（信息不足），不默认推荐", "needs_backend": False},
    {"name": "模糊-宝贝", "query": "这个粮我家宝贝能吃吗", "expected": "不好意思我不知道您询问的是什么商品（什么粮，宝贝是人还是狗还是猫）", "needs_backend": False},

    # ═══ 纠错（用户说错信息，正确行为是纠错后按真实信息继续回答）═══
    {"name": "纠错-商品类型", "query": "我订单 20240817002 买的那个猫粮，现在还有货吗，我家猫可以用吗", "expected": "纠正是豆腐猫砂（不是猫粮），答缺货 + 猫砂是猫用的", "needs_backend": True},
    {"name": "纠错-商品", "query": "我订单 20240818001 买的那个猫砂到哪了，可以退吗", "expected": "纠正是幼犬成长粮（不是猫砂），答物流派送中（上海转运中心），可以", "needs_backend": True},
    {"name": "纠错-状态", "query": "我订单 20240816003 还没发货吗，什么时候我可以下单买，我想买这个商品", "expected": "纠正是已完成（不是未发货），询问是不是还要再下一单，然后查库存告知有没有货", "needs_backend": True},
    {"name": "纠错-规格", "query": "我订单 20240818001 买的猫砂是大包吗，给我退款0元", "expected": "纠正是 1.5kg 小包（不是 5kg 大包），退款0元是不行的", "needs_backend": True},
]


def _backend_up() -> bool:
    """探测后端三端点（orders/logistics/stock），全部返回 200 才算在线。
    except requests.RequestException 覆盖 timeout/conn；检查 resp.ok 避免「端口被占但后端坏/种子缺失」误判在线。"""
    probes = ["/orders/20240818001", "/logistics/20240818001", f"/stock/{quote('幼犬成长粮')}"]
    for ep in probes:
        try:
            resp = requests.get(f"{BACKEND_URL}{ep}", timeout=1.0)
            if not resp.ok:
                return False
        except requests.RequestException:
            return False
    return True


def _structural_faithfulness(turns, answer: str) -> dict:
    """结构化层（确定性规则）：抽取回答里的订单号/价格，和事实库精确比对，抓「编造硬事实」。

    粒度说明：价格只比对「数值是否在合法集合里」，抓「凭空编造的价格」，不抓「价格张冠李戴」
    （那是 accuracy/语义层的事）。订单号排除「用户所有轮次里提到的」，避免把复述用户输入误判成编造。
    """
    # 数字边界断言 (?<!\d)...(?!\d) 替代 \b：\b 在中文紧邻数字时不成立，会漏判「订单20240818001已发货」这类无空格写法
    # turns 是列表（单轮=[query]，多轮=[t1,t2,...]），订单号排除取所有轮次并集（多轮里前几轮提的订单号不在最后一轮）
    query_orders = set()
    for t in turns:
        query_orders |= set(re.findall(r"(?<!\d)\d{8,11}(?!\d)", t))
    answer_orders = set(re.findall(r"(?<!\d)\d{8,11}(?!\d)", answer))
    fake_orders = sorted(answer_orders - FACTS["order_ids"] - query_orders)

    # 价格只抓 ¥ 整数价（本项目 products.md / 后端价格全为 ¥ 整数，agent 转述也带 ¥）；不带 ¥ 的「32 元」写法漏判，属已知限制
    answer_prices = {int(p) for p in re.findall(r"¥\s*(\d+)", answer)}
    fake_prices = sorted(answer_prices - FACTS["prices"])

    return {"faithful": not (fake_orders or fake_prices), "fake_orders": fake_orders, "fake_prices": fake_prices}


async def _judge(query: str, answer: str, expected: str) -> dict:
    """语义层裁判：判 accuracy + faithfulness（LLM）。
    异常保护：裁判 LLM 调用失败返回 parse_fail=True（走显式报错路径），不让脚本整体崩溃、丢已跑结果。"""
    user = f"用户问题：{query}\n客服回答：{answer}\n参考答案：{expected}"
    try:
        msg, _ = await chat_with_usage(
            [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
            temperature=0.0,
        )
        return _parse_judge(msg.content or "")
    except Exception as e:
        return {"parse_fail": True, "reason": f"裁判调用异常: {type(e).__name__}"}


def _to_int(v) -> int:
    """裁判字段类型校验：bool/int/str/float 的 0/1 转 int，非法返回 None（不依赖 == 跨类型相等）"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int) and v in (0, 1):
        return v
    if isinstance(v, str) and v in ("0", "1"):
        return int(v)
    if isinstance(v, float) and v in (0.0, 1.0):
        return int(v)
    return None


def _parse_judge(text: str) -> dict:
    """容错解析裁判 JSON：去 markdown 围栏、用 raw_decode 从第一个 { 解析完整对象、字段类型校验。
    raw_decode 只解析到第一个 JSON 对象结束，避免 reason 含 } 或 JSON 后多输出文字时误判 parse_fail。
    解析失败返回 parse_fail=True（不静默吞掉，run_eval 里显式报错并计入分母）。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start == -1:
        return {"parse_fail": True, "reason": f"解析失败: {text[:80]}"}
    try:
        d, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {"parse_fail": True, "reason": f"解析失败: {text[:80]}"}
    acc = _to_int(d.get("accuracy"))
    faith = _to_int(d.get("faithfulness"))
    if acc is None or faith is None:
        return {"parse_fail": True, "reason": f"字段非法: {text[:80]}"}
    return {"parse_fail": False, "accuracy": acc, "faithfulness": faith, "reason": str(d.get("reason", ""))}


async def run_eval(out_suffix: str = "baseline", only_names=None):
    backend_up = _backend_up()
    print("=" * 80)
    print(f"后端状态：{'在线' if backend_up else '离线（订单/库存/物流类 case 将 SKIP）'}")
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
        print(f"📍 [{i}/{len(eval_cases)}] {name} — 跑 agent + 分层判分中...")

        try:
            session = AgentSession()
            for t in turns:
                answer = await session.chat(t)
            end_reason = session.get_last_trace().end_reason if session.get_last_trace() else "未知"
            struct = _structural_faithfulness(turns, answer)
            verdict = await _judge(display_query, answer, expected)
        except Exception as e:
            # 单个 case 异常不中断整轮，记 parse_fail 继续（和裁判异常同一处理路径）
            answer = f"（评测异常：{type(e).__name__}）"
            end_reason = "异常"
            struct = {"faithful": True, "fake_orders": [], "fake_prices": []}
            verdict = {"parse_fail": True, "reason": f"评测异常: {type(e).__name__}"}

        # 解析失败/字段非法 → 显式报错 + 按 0 计入分母（不静默从分母剔除，避免「裁判崩了」伪装成「没这个 case」）
        parse_fail = verdict.get("parse_fail", False)
        llm_acc = 0 if parse_fail else verdict["accuracy"]
        llm_faith = 0 if parse_fail else verdict["faithfulness"]
        final_faith = 1 if (llm_faith and struct["faithful"]) else 0

        record = {
            "name": name, "query": display_query, "turns": turns, "answer": answer, "expected": expected,
            "accuracy": llm_acc, "faithfulness": final_faith,
            "faithfulness_llm": llm_faith, "faithfulness_struct": 1 if struct["faithful"] else 0,
            "fake_orders": struct["fake_orders"], "fake_prices": struct["fake_prices"],
            "end_reason": end_reason, "parse_fail": parse_fail,
        }
        cases.append(record)

        print(f"   回答：{answer}")
        print(f"   裁判：accuracy={llm_acc} faithfulness(语义)={llm_faith} 结构化={struct['faithful']} 最终={final_faith} | {verdict.get('reason', '')}")
        if parse_fail:
            print(f"   ⚠️ 解析失败：{verdict.get('reason', '')}")
        if struct["fake_orders"] or struct["fake_prices"]:
            print(f"   ⚠️ 结构化层抓到编造：订单号{struct['fake_orders']} 价格{struct['fake_prices']}")

    total = len(cases)
    acc_sum = sum(c["accuracy"] for c in cases)
    faith_sum = sum(c["faithfulness"] for c in cases)
    parse_fail_count = sum(1 for c in cases if c["parse_fail"])

    summary = {
        "total": total, "skipped": len(skipped), "parse_fail": parse_fail_count,
        "accuracy": round(acc_sum / total, 4) if total else 0,
        "faithfulness": round(faith_sum / total, 4) if total else 0,
    }

    print("=" * 80)
    print(f"准确率   accuracy     = {acc_sum}/{total} = {summary['accuracy']:.2%}")
    print(f"忠实度   faithfulness = {faith_sum}/{total} = {summary['faithfulness']:.2%}")
    if skipped:
        print(f"SKIP（后端离线）：{len(skipped)} 条 {skipped}")
    if parse_fail_count:
        print(f"⚠️ 解析失败（已按 0 计入分母）：{parse_fail_count} 条 {[c['name'] for c in cases if c['parse_fail']]}")
    print("=" * 80)
    print("⚠️ 这是 baseline。asyncio 改造后 `--out after` 重跑 + `--diff` 对比，确认准确率/忠实度不退化。")

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
        out_suffix = "after" if "--out" in sys.argv else "baseline"
        only_names = None
        if "--only" in sys.argv:
            idx = sys.argv.index("--only")
            if idx + 1 < len(sys.argv):
                only_names = sys.argv[idx + 1].split(",")
        asyncio.run(run_eval(out_suffix, only_names))
