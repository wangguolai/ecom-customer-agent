# -*- coding: utf-8 -*-
"""裁判模块 —— 回答质量评测的判定层（结构化层 + 语义层 LLM 裁判）

抽出来让 eval_answer_quality.py（端到端跑 agent）和 eval_judge_calibration.py（校准裁判）共用。
不抽的话，校准脚本 `from eval_answer_quality import _judge` 会连带执行 eval_answer_quality 的顶层代码
（FACTS 加载 + import AgentSession + tools 建 httpx client），把整条 agent 链拉起——校准脚本本该轻量。

职责：
- 结构化层（确定性规则，零付费）：_structural_faithfulness 抓「编造硬事实」（订单号/价格/品牌/物流地点）
- 语义层（LLM，付费）：_judge 判 accuracy + relevance + faithfulness（只判语义编造，硬事实不归它）

注意：函数保留下划线前缀是历史命名，跨模块 import 后不改名是为了少改调用点。
"""

import sys
import os
import json
import re

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.infra.llm import chat_with_usage


def _load_facts() -> dict:
    """评测白名单（从派生层生成：品牌/价格/商品名从 products.md，订单号/状态/物流从 backend 种子）"""
    from src.derived.facts import build_facts
    return build_facts()


FACTS = _load_facts()

# 品牌提取的功能词停用前缀（口语「什么牌/这个牌/哪个牌」不是品牌名，避免误判编造品牌）
_BRAND_STOP_PREFIXES = {"什么", "这个", "那个", "哪个", "一个", "这种", "那种", "哪种"}


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

    # 品牌校验：先按白名单整词扣除，再从残余里抓「XX牌」形态判编造。
    # 排除三类误抓：① 功能词前缀（什么/这个/哪个…）②「X品牌/X牌子」普通词（提取词以「品/子」结尾）③ 白名单品牌（已扣除）。
    #
    # ⚠️ 2026-09-18 修：原实现是「固定 2 字正则 ([一-龥]{2})牌 + 2 字前缀白名单」。
    #   旧品牌（皇家/冠能/渴望）恰好全 2 字，这个隐含假设一直没暴露；换 162 真实商品库后出现
    #   3-4 字品牌（麦富迪/伯纳天纯/疯狂小狗），「麦富迪牌」被正则截成「富迪牌」→ 不在白名单 →
    #   判成编造品牌 → 任何推荐这三个品牌的回答 faithfulness 结构性崩（实测三例全 fake）。
    #   为什么不直接把正则改成 {2,4}：会吞掉上下文——「不知道什么牌的好」从「知」起贪婪取 4 字
    #   命中「知道什么」+牌，捕出一个带上下文的假前缀（对抗测试实测踩到）。多长度扫描同理：
    #   「牌」前 3 字「道什么」也成候选，仍误报。
    #   故改为「先按白名单整词扣除、正则仍是固定 2 字」：白名单里 2/3/4 字品牌都被扣干净后，
    #   残余里出现的「XX牌」必是编造，此时按**紧邻 2 字**取最准（长候选只会多吞上下文）。
    residue = answer
    for _b in sorted((b for b in FACTS["brands"] if b in answer), key=len, reverse=True):
        residue = residue.replace(_b, "")
    answer_brand_tokens = set(re.findall(r"([一-龥]{2})牌", residue))
    fake_brands = sorted(
        f"{p}牌" for p in answer_brand_tokens
        if p not in _BRAND_STOP_PREFIXES and not p.endswith(("品", "子"))
    )

    # 物流地点编造：抓「XX中心」形态，和 FACTS["logistics_locations"] 精确比对。
    # 白名单地点是完整词（如「杭州分拨中心」「上海转运中心」），抓 answer 里「XX中心」前缀，
    # 排除功能词前缀（客服/购物等普通词），拼回「XX中心」不在白名单 → 编造。保守原则：宁可漏不可误报。
    # 停用词用「包含」而非「精确等于」：正则贪婪匹配会吃掉「我们客服」里的「我们」，
    # 精确等于「客服」会漏判（「我们客服」≠「客服」），必须 any(stop in p)。
    _FAKE_LOC_STOP = ("客服", "购物", "服务", "数据", "售后", "信息", "查询", "市中", "配送", "仓储")
    answer_loc_tokens = set(re.findall(r"([一-龥]{2,4})中心", answer))
    fake_locations = sorted(
        f"{p}中心" for p in answer_loc_tokens
        if f"{p}中心" not in FACTS["logistics_locations"] and not any(stop in p for stop in _FAKE_LOC_STOP)
    )

    return {
        "faithful": not (fake_orders or fake_prices or fake_brands or fake_locations),
        "fake_orders": fake_orders, "fake_prices": fake_prices, "fake_brands": fake_brands, "fake_locations": fake_locations,
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
        # ⚠️ 这里手工副本 = 裁判的「政策事实源」，必须与 data/policies.md 同步。
        # 不同步的后果：agent 正确答出新增条款会被判成「编造政策条款」（faithfulness=0）。
        # （政策文本目前有三处副本：data/policies.md（真源）、本节、config/prompts.py 的
        #  RETURN_POLICY 兜底常量——改一处要想着另外两处。）
        "退换政策：7天无理由退货（未拆封）、质量问题15天内退换、食品类拆封不退、"
        "退款需人工审批（生成待审批工单）、退款1-3个工作日到账；"
        "2025 旧版「15天无理由退货」已于 2026-01-01 停止执行，现行按 7 天为准，"
        "但若用户主张其订单适用旧版 15 天政策，属历史遗留，应建议转人工核实处理"
    )


def _usage_dict(usage) -> dict:
    """usage 对象转 dict（token 消耗，供落盘让评测成本可见）。异常保护：usage 可能为 None。"""
    if usage is None:
        return {}
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
        "total_tokens": getattr(usage, "total_tokens", 0),
    }


# 裁判（语义层）：判 accuracy + relevance + faithfulness
# 裁判提示词已移到 src/config/prompts.py（提示词统一处），此处只引用
from src.config.prompts import JUDGE_SYSTEM
from src.config.settings import REASONING_EFFORT_MECHANICAL


async def _judge(query: str, answer: str, expected: str) -> dict:
    """语义层裁判：判 accuracy + relevance + faithfulness（LLM，喂 KB 事实源做 ground truth）。
    异常保护：裁判 LLM 调用失败返回 parse_fail=True（走显式报错路径），不让脚本整体崩溃、丢已跑结果。"""
    user = f"{_kb_context()}\n\n用户问题：{query}\n客服回答：{answer}\n参考答案：{expected}"
    try:
        msg, usage = await chat_with_usage(
            [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
            temperature=0.0,
            # ⚠️ 裁判是**测量仪器，必须钉死**，不能跟着被测系统的旋钮走。
            # 曾经这里不传 effort → 走 `settings.REASONING_EFFORT` 全局值 → 跑
            # `--effort low` 做 A/B 时**裁判本身也被切到 low**，两臂的**评分标准不同**，
            # 分数不可比（这是个方法学缺陷，不是风格问题）。
            # 传常量而非默认值，才真正解耦。
            reasoning_effort=REASONING_EFFORT_MECHANICAL,
        )
        verdict = _parse_judge(msg.content or "")
        verdict["usage"] = _usage_dict(usage)  # token 消耗落盘，让评测成本可见
        return verdict
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


def _to_relevance(v) -> int:
    """relevance 字段类型校验：int/str/float 的 0-5 转 int，bool 排除，非法返回 None"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and 0 <= v <= 5:
        return v
    if isinstance(v, str):
        try:
            n = int(v.strip())
        except ValueError:
            return None
        return n if 0 <= n <= 5 else None
    if isinstance(v, float) and v.is_integer() and 0 <= v <= 5:
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
    rel = _to_relevance(d.get("relevance"))
    faith = _to_int(d.get("faithfulness"))
    if acc is None or rel is None or faith is None:
        return {"parse_fail": True, "reason": f"字段非法: {text[:80]}"}
    return {"parse_fail": False, "accuracy": acc, "relevance": rel, "faithfulness": faith, "reason": str(d.get("reason", ""))}
