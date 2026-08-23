# -*- coding: utf-8 -*-
"""可观测性 —— 单次 Trace + 多次聚合指标

单次 Trace：记录一次对话的每次 LLM 决策（step/耗时/token）、每次工具调用（工具名/耗时/step/是否空返回）、结束原因。
MetricsStore：聚合多次 Trace，算技术成功率、P99 延迟、平均延迟、平均 token。

和标准方案（OpenTelemetry / LangSmith）的差异在哪：
- OTel 是跨服务的标准协议（trace_id/span_id 传播、自动采集、接 Jaeger/Grafana 等生态）
- LangSmith 是 LangChain 生态专用（prompt 版本管理、数据集评测、线上监控）
- 这里是手写最小版，目的不是替代标准方案，而是把「埋点发生在哪」说明白——就是 LLM 调用处 + 工具调用处两处。生产量级上去该换 OTel，但埋点位置和这套一样。
"""

import time


class Trace:
    """一次对话的 trace"""

    def __init__(self):
        self.start = time.perf_counter()
        self.steps = []       # [(step, elapsed, tokens, prompt_tokens)] 每次 LLM 决策
        self.tool_calls = []  # [(name, elapsed, step, is_empty)] 每次工具调用
        self.summaries = []   # [(elapsed, prompt_tokens, completion_tokens)] 摘要压缩调用
        self.cache_hit_tokens = 0   # DeepSeek 前缀缓存命中 token（按命中价计费，~1/30 输入价）
        self.cache_miss_tokens = 0  # 缓存未命中 token（按未命中价计费）
        self.end_reason = "未知"  # 正常 / 死循环 / 超步数 / 异常

    def add_llm(self, step: int, elapsed: float, tokens, prompt_tokens=None, cache_hit=0, cache_miss=0):
        self.steps.append((step, elapsed, tokens or 0, prompt_tokens or 0))
        self.cache_hit_tokens += cache_hit or 0
        self.cache_miss_tokens += cache_miss or 0  # tokens 可能为 None，防御

    def add_tool(self, name: str, elapsed: float, step: int, is_empty: bool):
        self.tool_calls.append((name, elapsed, step, is_empty))

    def add_summary(self, elapsed: float, prompt_tokens, completion_tokens):
        """记录一次摘要压缩调用（输入 prompt_tokens + 输出 completion_tokens，用于算摘要成本）"""
        self.summaries.append((elapsed, prompt_tokens or 0, completion_tokens or 0))

    def summary(self) -> dict:
        total_time = time.perf_counter() - self.start
        total_tokens = sum(s[2] for s in self.steps)
        prompt_tokens = sum(s[3] for s in self.steps)
        summary_in = sum(p for _, p, _ in self.summaries)
        summary_out = sum(c for _, _, c in self.summaries)
        tool_freq = {}
        empty_count = 0
        for name, _, _, is_empty in self.tool_calls:
            tool_freq[name] = tool_freq.get(name, 0) + 1
            if is_empty:
                empty_count += 1
        cache_total = self.cache_hit_tokens + self.cache_miss_tokens
        cache_hit_rate = round(self.cache_hit_tokens / cache_total, 4) if cache_total else 0.0
        return {
            "总耗时(秒)": round(total_time, 3),
            "LLM 调用次数": len(self.steps) + len(self.summaries),  # 含摘要调用，和「总 token 消耗」规范一致
            "总 token 消耗": total_tokens + summary_in + summary_out,  # 计费规范：主循环各步 + 摘要（含重复历史）
            "输入 token 规模": prompt_tokens,  # 主循环各步 prompt_tokens 求和（含重复历史），压缩率分析用
            "缓存命中 token": self.cache_hit_tokens,  # DeepSeek 前缀缓存命中（省钱核心指标）
            "缓存未命中 token": self.cache_miss_tokens,
            "缓存命中率": cache_hit_rate,  # hit / (hit+miss)，0 表示无缓存数据或未命中
            "摘要调用次数": len(self.summaries),
            "摘要输入 token": summary_in,  # 把旧历史发给摘要 LLM 花的 prompt token
            "摘要输出 token": summary_out,  # 摘要正文 completion token
            "工具调用次数": len(self.tool_calls),
            "工具频次": tool_freq,
            "召回端空返回次数": empty_count,
            "结束原因": self.end_reason,
        }

    def __repr__(self):
        s = self.summary()
        return (
            f"<Trace 耗时={s['总耗时(秒)']}s LLM={s['LLM 调用次数']}次 "
            f"token={s['总 token 消耗']} 缓存命中率={s['缓存命中率']:.1%} "
            f"工具={s['工具调用次数']}次 空返回={s['召回端空返回次数']} 结束={s['结束原因']}>"
        )


class MetricsStore:
    """聚合多次对话的指标：技术成功率、P99/平均延迟、平均 token"""

    def __init__(self):
        self.records = []  # [(total_time, total_tokens, end_reason)]

    def record(self, trace: Trace):
        s = trace.summary()
        self.records.append((s["总耗时(秒)"], s["总 token 消耗"], s["结束原因"]))

    def _percentile(self, values: list, p: int) -> float:
        """最近秩近似 P 分位"""
        if not values:
            return 0.0
        values = sorted(values)
        idx = min(len(values) - 1, int((len(values) - 1) * p / 100))
        return values[idx]

    def summary(self) -> dict:
        if not self.records:
            return {"样本数": 0}
        times = [r[0] for r in self.records]
        tokens = [r[1] for r in self.records]
        # 技术成功率：结束原因 == 正常（没死循环/没超步数/没异常）。答案对不对归评测体系，不归这里。
        success = sum(1 for r in self.records if r[2] == "正常")
        return {
            "样本数": len(self.records),
            "技术成功率": round(success / len(self.records), 4),
            "平均延迟(秒)": round(sum(times) / len(times), 3),
            "P99延迟(秒)": round(self._percentile(times, 99), 3),
            "平均token": round(sum(tokens) / len(tokens), 1),
        }

    def __repr__(self):
        s = self.summary()
        if s.get("样本数", 0) == 0:
            return "<MetricsStore 无样本>"
        return (
            f"<Metrics 样本={s['样本数']} 成功率={s['技术成功率']} "
            f"平均延迟={s['平均延迟(秒)']}s P99={s['P99延迟(秒)']}s 平均token={s['平均token']}>"
        )
