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
# 事实库（结构化层 ground truth）
# 品牌/商品名/价格从 products.md 自动提取（单一数据源，避免手写白名单和知识库不同步）；
# 订单号来自 backend 种子（不在 products.md，手写）。数据迁移时只需改 products.md，白名单自动更新。
# ═══════════════════════════════════════════════════════════════
def _load_facts() -> dict:
    """评测白名单（从派生层生成：品牌/价格/商品名从 products.md，订单号/状态/物流从 backend 种子）"""
    from src.derived.facts import build_facts
    return build_facts()


FACTS = _load_facts()

# 品牌提取的功能词停用前缀（口语「什么牌/这个牌/哪个牌」不是品牌名，避免误判编造品牌）
_BRAND_STOP_PREFIXES = {"什么", "这个", "那个", "哪个", "一个", "这种", "那种", "哪种"}

# ═══════════════════════════════════════════════════════════════
# 裁判（语义层）：判 accuracy + faithfulness
# 硬事实（订单号/价格）的编造不归 LLM 判，由结构化层精确比对——让两个维度正交：
#   accuracy 只判「答对没」，faithfulness 只判「语义编造没」，不混「该拒答却编造」进 accuracy。
# ═══════════════════════════════════════════════════════════════
JUDGE_SYSTEM = """你是电商客服 Agent 回答质量的评测裁判。根据「知识库事实」「用户问题」「客服回答」「参考答案」判两个维度：

1. accuracy（准确率，0/1）：客服回答是否正确回应了用户问题、是否与参考答案一致（答对了该答的）。
2. faithfulness（忠实度，0/1）：客服回答是否忠于「知识库事实」，有没有编造知识库里不存在的商品名/品牌/物流轨迹/政策条款。

重要：faithfulness 判的是「编造」，不是「答错」。以下情况都是 accuracy 问题（faithfulness 应判 1），不要误判成编造：
- 答非所问、漏答关键信息
- 没有纠正用户的错误说法
- 推荐了「不相关但知识库里确实存在」的商品
- 该反问时没反问、直接推荐了知识库里的商品

只有「说了知识库里不存在的商品名 / 品牌 / 物流轨迹 / 政策条款」这类凭空捏造，才判 faithfulness=0。

注意：回答里的「订单号」「价格数值」这类硬事实的编造由外部规则层精确比对，你主要判「商品名/品牌/物流/政策」这类语义编造。

评分规则：
- accuracy=1：回答正确回应了用户问题、与参考答案一致；accuracy=0：答非所问、答错、漏答关键信息。
- faithfulness=1：没有编造知识库不存在的商品/品牌/物流/政策；faithfulness=0：编造了知识库不存在的商品名/品牌/物流轨迹/政策条款。

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

    # 品牌校验：抓 answer 里的「XX牌」（2 字），不在白名单的判编造。
    # 排除三类误抓：① 功能词前缀（什么/这个/哪个…）②「X品牌/X牌子」普通词（提取词以「品/子」结尾）③ 已知品牌前缀。
    known_brand_prefixes = {b[:-1] for b in FACTS["brands"]}  # 贝乐/优宠/喵趣
    answer_brand_tokens = set(re.findall(r"([一-龥]{2})牌", answer))
    fake_brands = sorted(
        f"{p}牌" for p in answer_brand_tokens
        if p not in known_brand_prefixes and p not in _BRAND_STOP_PREFIXES and not p.endswith(("品", "子"))
    )

    return {
        "faithful": not (fake_orders or fake_prices or fake_brands),
        "fake_orders": fake_orders, "fake_prices": fake_prices, "fake_brands": fake_brands,
    }


def _kb_context() -> str:
    """裁判的事实源上下文（判断「编造」的唯一依据）。喂给裁判，避免它拿「现实世界品牌存在性」误判 demo 虚构品牌。"""
    return (
        "【知识库事实（判断「编造」的唯一依据，不是现实世界）】\n"
        f"品牌：{('、'.join(sorted(FACTS['brands']))) or '（无）'}\n"
        f"商品：{'、'.join(sorted(FACTS['product_names']))}\n"
        f"订单号：{'、'.join(sorted(FACTS['order_ids']))}\n"
        f"订单状态：{'、'.join(sorted(FACTS['order_statuses']))}\n"
        "库存状态：有货、缺货\n"
        f"物流状态：{'、'.join(sorted(FACTS['logistics_statuses']))}\n"
        f"物流地点：{'、'.join(sorted(FACTS['logistics_locations']))}\n"
        "退换政策：7天无理由退货（未拆封）、质量问题15天内退换、食品类拆封不退、退款需人工审批（生成待审批工单）、退款1-3个工作日到账"
    )


async def _judge(query: str, answer: str, expected: str) -> dict:
    """语义层裁判：判 accuracy + faithfulness（LLM，喂 KB 事实源做 ground truth）。
    异常保护：裁判 LLM 调用失败返回 parse_fail=True（走显式报错路径），不让脚本整体崩溃、丢已跑结果。"""
    user = f"{_kb_context()}\n\n用户问题：{query}\n客服回答：{answer}\n参考答案：{expected}"
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


async def _run_case_once(name: str, turns: list, display_query: str, expected: str) -> dict:
    """单次跑一个 case：跑 agent（多步 ReAct）+ 结构化层 + 语义裁判，返回单次结果。

    异常不中断（和裁判异常同一处理路径），记 parse_fail 继续。
    """
    try:
        session = AgentSession()
        for t in turns:
            answer = await session.chat(t)
        end_reason = session.get_last_trace().end_reason if session.get_last_trace() else "未知"
        struct = _structural_faithfulness(turns, answer)
        verdict = await _judge(display_query, answer, expected)
    except Exception as e:
        answer = f"（评测异常：{type(e).__name__}）"
        end_reason = "异常"
        struct = {"faithful": True, "fake_orders": [], "fake_prices": [], "fake_brands": []}
        verdict = {"parse_fail": True, "reason": f"评测异常: {type(e).__name__}"}

    # 解析失败/字段非法 → 显式报错 + 按 0 计入（不静默从分母剔除，避免「裁判崩了」伪装成「没这个 case」）
    parse_fail = verdict.get("parse_fail", False)
    llm_acc = 0 if parse_fail else verdict["accuracy"]
    llm_faith = 0 if parse_fail else verdict["faithfulness"]
    final_faith = 1 if (llm_faith and struct["faithful"]) else 0

    return {
        "answer": answer, "accuracy": llm_acc, "faithfulness": final_faith,
        "faithfulness_llm": llm_faith, "faithfulness_struct": 1 if struct["faithful"] else 0,
        "fake_orders": struct["fake_orders"], "fake_prices": struct["fake_prices"], "fake_brands": struct["fake_brands"],
        "end_reason": end_reason, "parse_fail": parse_fail,
        "reason": verdict.get("reason", ""),
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
            if once["fake_orders"] or once["fake_prices"] or once["fake_brands"]:
                print(f"   ⚠️ 结构化层抓到编造：订单号{once['fake_orders']} 价格{once['fake_prices']} 品牌{once['fake_brands']}")

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
    parse_fail_count = sum(c["parse_fail"] for c in cases)

    summary = {
        "total": total, "skipped": len(skipped), "parse_fail": parse_fail_count,
        "runs": runs,
        "accuracy": round(acc_sum / total, 4) if total else 0,
        "faithfulness": round(faith_sum / total, 4) if total else 0,
    }

    print("=" * 80)
    print(f"准确率   accuracy     = {acc_sum}/{total} = {summary['accuracy']:.2%}")
    print(f"忠实度   faithfulness = {faith_sum}/{total} = {summary['faithfulness']:.2%}")
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
