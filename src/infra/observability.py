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
import uuid


class Trace:
    """一次对话的 trace"""

    def __init__(self, trace_id: str = None):
        # trace_id：贯穿「SSE 首帧 → 前端评分 → 落库 → 后台回溯」的那条线。
        # 请求开始时由调用方（main.py）生成并传入，保证「SSE 首帧发给前端的 id」
        # 与「落库的 id」是同一个；不传则自生成（CLI / 测试路径）。
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.start = time.perf_counter()
        self.steps = []       # [(step, elapsed, tokens, prompt_tokens)] 每次 LLM 决策
        self.tool_calls = []  # [(name, elapsed, step, is_empty)] 每次工具调用
        self.summaries = []   # [(elapsed, prompt_tokens, completion_tokens)] 摘要压缩调用
        self.cache_hit_tokens = 0   # DeepSeek 前缀缓存命中 token（按命中价计费，~1/30 输入价）
        self.cache_miss_tokens = 0  # 缓存未命中 token（按未命中价计费）
        # 思考模式的推理 token（思维链）。**它是 completion_tokens 的子集，不要重复累加**。
        # 为什么单列：不列的话 `total_tokens` 解释不了耗时——实测一次 20 秒的对话里
        # reasoning=1908 / completion=1929，99% 的输出预算花在用户看不见的地方，
        # 而"答案为什么这么慢"在看板上完全不可见。
        self.reasoning_tokens = 0
        # 路由来源（**改动这里必须同步下面的 RULE_ROUTE_LABELS**）：
        #   LLM       —— 走 ReAct 决策（默认）
        #   规则      —— 意图路由层直通工具（省一次决策调用）
        #   规则降级  —— 规则路由撞上工具调用文本泄漏，降级重跑了 ReAct（**没省下**）
        #   菜单      —— 伪菜单零 LLM 直通（缺参固定反问，省得最多）
        self.route_source = "LLM"
        self.end_reason = "未知"  # 正常 / 死循环 / 超步数 / 异常 / 断开
        # 本轮检索命中的 chunk_id（含没图的）。落盘用——**不带它就无法复现「检索错了」**
        # 这类失败：看不到当时命中了哪些块，只能看到最终答案。
        self.retrieved_ids = []

    def add_llm(self, step: int, elapsed: float, tokens, prompt_tokens=None, cache_hit=0, cache_miss=0,
                reasoning_tokens=0):
        self.steps.append((step, elapsed, tokens or 0, prompt_tokens or 0))
        self.cache_hit_tokens += cache_hit or 0
        self.cache_miss_tokens += cache_miss or 0  # tokens 可能为 None，防御
        self.reasoning_tokens += reasoning_tokens or 0

    def add_tool(self, name: str, elapsed: float, step: int, is_empty: bool, category=None):
        """记录一次工具调用。`category` = 该次调用的**检索类别**（含「类别下沉」注入进来的）。

        ⚠️ 为什么要记它：线上一旦又出现「跨轮混类」（狗粮混进猫粮），排障表里必须能看到
        **当时到底锁了什么类别**——否则只能看到「检索错了」，看不到「为什么锁错了」。
        这正是本批要能复现的那类失败。`category` 为 None 表示没锁（不过滤）。
        """
        self.tool_calls.append((name, elapsed, step, is_empty, category))

    def add_summary(self, elapsed: float, prompt_tokens, completion_tokens, reasoning_tokens=0):
        """记录一次摘要压缩调用（输入 / 输出 / 推理 token，用于算摘要成本）

        ⚠️ `reasoning_tokens` 必须记：摘要调用是一个**独立的推理黑洞**——思考模式下
        `SUMMARY_MAX_TOKENS=300` 会被推理整段吃光（实测 `completion=300, reasoning=300,
        content=''`）。只记主循环的推理量，这条路径的花费与失败就永远不会出现在看板上。

        ⚠️ 元组取值一律走**下标**（见 `summary()`），不要再加解包式的三元组身份名——
        这个元组已经加过两次字段，解包写法每加一次就要全文改一遍。
        """
        self.summaries.append((elapsed, prompt_tokens or 0, completion_tokens or 0, reasoning_tokens or 0))

    def summary(self) -> dict:
        total_time = time.perf_counter() - self.start
        total_tokens = sum(s[2] for s in self.steps)
        prompt_tokens = sum(s[3] for s in self.steps)
        n_sum = len(self.summaries)
        summary_in = sum(self.summaries[i][1] for i in range(n_sum))
        summary_out = sum(self.summaries[i][2] for i in range(n_sum))
        summary_reasoning = sum(self.summaries[i][3] for i in range(n_sum))
        tool_freq = {}
        empty_count = 0
        # ⚠️ 按下标取值，不用解包：元组已加过字段（第 5 个是 category），
        # 解包写法每加一次字段就要全文改一遍，且漏改会 ValueError 崩在看板上。
        for _tc in self.tool_calls:
            name, is_empty = _tc[0], _tc[3]
            tool_freq[name] = tool_freq.get(name, 0) + 1
            if is_empty:
                empty_count += 1
        cache_total = self.cache_hit_tokens + self.cache_miss_tokens
        cache_hit_rate = round(self.cache_hit_tokens / cache_total, 4) if cache_total else 0.0
        # 推理 token 占比的**分母必须是 completion，不是 total**：
        # total 里大头是 prompt（且被前缀缓存大量命中），拿它做分母会把 99% 的推理占比
        # 稀释成个位数，看板上反而看不出问题。主循环各步的 completion 由 total - prompt 推出。
        completion_tokens = sum(max(0, s[2] - s[3]) for s in self.steps)
        # ⚠️ **分子分母都要含摘要调用**：摘要是一个独立的推理黑洞（思考模式下
        # `SUMMARY_MAX_TOKENS=300` 被推理吃光、content 为空）。只统计主循环的话，
        # 这类花费与失败永远不会出现在这一列——而这正是 2026-09-20 那次线上功能
        # 失效（压缩从未生效）最难查的原因。
        reasoning_total = self.reasoning_tokens + summary_reasoning
        completion_total = completion_tokens + summary_out
        # 无 completion 数据（如全部调用失败）时返回 None 而不是 0——「没有数据」和
        # 「推理占比为零」是两回事。注意口径对齐的是**聚合层** `MetricsStore.snapshot()`
        # 的 `cache_hit_rate`（无数据返回 None）；`summary()` 里的 `cache_hit_rate` 键
        # 返回的是 0.0，两者刻意不同，别把它们当成同一处规范。
        reasoning_ratio = round(reasoning_total / completion_total, 4) if completion_total else None
        return {
            "总耗时(秒)": round(total_time, 3),
            "LLM 调用次数": len(self.steps) + len(self.summaries),  # 含摘要调用，和「总 token 消耗」规范一致
            "总 token 消耗": total_tokens + summary_in + summary_out,  # 计费规范：主循环各步 + 摘要（含重复历史）
            "输入 token 规模": prompt_tokens,  # 主循环各步 prompt_tokens 求和（含重复历史），压缩率分析用
            "缓存命中 token": self.cache_hit_tokens,  # DeepSeek 前缀缓存命中（省钱核心指标）
            "缓存未命中 token": self.cache_miss_tokens,
            "缓存命中率": cache_hit_rate,  # hit / (hit+miss)，0 表示无缓存数据或未命中
            "推理 token": reasoning_total,                # 思考模式思维链消耗（主循环 + 摘要）
            "推理占 completion": reasoning_ratio,          # None = 无 completion 数据
            # 单列 completion 量：聚合层要按「总量算率」（项目既有规范，见 MetricsStore.summary
            # 的注释——各次比率求平均在有偏样本下会失真），所以分母必须跟着一起落进 records。
            "completion token": completion_total,
            "摘要调用次数": len(self.summaries),
            "摘要输入 token": summary_in,  # 把旧历史发给摘要 LLM 花的 prompt token
            "摘要输出 token": summary_out,  # 摘要正文 completion token
            "工具调用次数": len(self.tool_calls),
            "工具频次": tool_freq,
            "召回端空返回次数": empty_count,
            "路由来源": self.route_source,
            "结束原因": self.end_reason,
        }

    def __repr__(self):
        s = self.summary()
        return (
            # 带 trace_id 短前缀：排障时能从服务端日志直接拿 id 去 traces 表里查，
            # 不用先按时间/会话反查（这是落盘带来的顺带收益）
            f"<Trace {self.trace_id[:8]} 耗时={s['总耗时(秒)']}s LLM={s['LLM 调用次数']}次 "
            f"token={s['总 token 消耗']} 缓存命中率={s['缓存命中率']:.1%} "
            f"工具={s['工具调用次数']}次 空返回={s['召回端空返回次数']} 结束={s['结束原因']}>"
        )


# 「规则路由占比」的口径：**真的省下了决策调用**的那些轮。
#   · "规则"     —— 规则层直通工具 ✅ 省一次决策
#   · "菜单"     —— 菜单零 LLM 直通 ✅ 省得更多
#   · "规则降级" —— 规则路由撞上工具调用文本泄漏、降级重跑了 ReAct ❌ **没省下**，
#                  计入会高估规则路由的收益（2026-09-20 引入该标签时特意排除）
RULE_ROUTE_LABELS = ("规则", "菜单")


class MetricsStore:
    """聚合多次对话的指标：技术成功率、P50/P95/P99 延迟、平均 token、缓存命中率

    ⚠️ 三条使用边界（`/admin/metrics` 必须在返回体里标注，否则是误导）：
      1. **进程内单例**（src/agent.py:487）—— 多 worker 各自持一份，**不聚合**
      2. **只统计 Web 链路** —— 只有 `/chat/stream` 路径会调 `record()`；
         CLI demo / 评测脚本跑的对话**不计入**
      3. **重启清零** —— records 是内存 list，没有落盘
    """

    def __init__(self):
        # (耗时, 总token, 结束原因, 路由来源, 缓存命中token, 缓存未命中token,
        #  推理token, completion_token)
        # ⚠️ 新字段**只能追加在末尾**：`summary()` 里全部按下标 r[0]..r[5] 取值，
        # 插到中间会让「缓存命中率 / 技术成功率」等既有指标**全线错位**且不报错。
        self.records = []
        self.since = None   # 首个样本时间，给前端标注「统计起点」

    def record(self, trace: Trace):
        s = trace.summary()
        if self.since is None:
            self.since = time.strftime("%Y-%m-%d %H:%M:%S")
        self.records.append((
            s["总耗时(秒)"], s["总 token 消耗"], s["结束原因"], s["路由来源"],
            s["缓存命中 token"], s["缓存未命中 token"],
            s["推理 token"], s["completion token"],
        ))

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
        route_rule = sum(1 for r in self.records if r[3] in RULE_ROUTE_LABELS)
        # 缓存命中率＝**总命中 / 总 token**，不是「各次命中率的平均」：
        # 后者在样本量差异大时会有偏（一次 1000 token 全命中和一次 10 token 全未命中，
        # 平均各次比率会得到 50%，而真实命中率是 99%）。聚合率永远用总量算。
        cache_hit = sum(r[4] for r in self.records)
        cache_total = cache_hit + sum(r[5] for r in self.records)
        # 推理占比同样按**总量算**（不是各次比率的平均）——理由同上。
        reasoning_total = sum(r[6] for r in self.records)
        completion_total = sum(r[7] for r in self.records)
        return {
            "样本数": len(self.records),
            "技术成功率": round(success / len(self.records), 4),
            "规则路由占比": round(route_rule / len(self.records), 4),
            "平均延迟(秒)": round(sum(times) / len(times), 3),
            "P50延迟(秒)": round(self._percentile(times, 50), 3),
            "P95延迟(秒)": round(self._percentile(times, 95), 3),
            "P99延迟(秒)": round(self._percentile(times, 99), 3),
            "平均token": round(sum(tokens) / len(tokens), 1),
            "缓存命中率": round(cache_hit / cache_total, 4) if cache_total else None,
            "推理token": reasoning_total,          # 思考模式思维链消耗总量（含摘要调用）
            "推理占比": round(reasoning_total / completion_total, 4) if completion_total else None,
        }

    def snapshot(self) -> dict:
        """给 `/admin/metrics` 的快照：**英文字段名当 API 契约** + 口径标注。

        为什么不直接用中文键：中文键是给人看的（CLI / __repr__），
        当成 API 契约会让前端依赖中文 key，易碎且别扭。这里做一层显式映射。

        `cache_hit_rate` 无数据时返回 **None 而不是 0**——两者语义不同：
        「没有缓存数据」和「命中率为零」是两回事，前端据此显示 `—` 而不是误导性的 `0%`。
        """
        s = self.summary()
        n = s.get("样本数", 0)
        base = {
            "samples": n,
            "scope": "single-process",   # 进程内单例，多 worker 不聚合
            "sample_source": "web",      # 只有 /chat/stream 写入，CLI/评测不计入
            "sample_since": self.since,
        }
        if not n:
            return base
        base.update({
            "tech_success_rate": s["技术成功率"],
            "rule_route_rate": s["规则路由占比"],
            "latency_avg": s["平均延迟(秒)"],
            "latency_p50": s["P50延迟(秒)"],
            "latency_p95": s["P95延迟(秒)"],
            "latency_p99": s["P99延迟(秒)"],
            "tokens_avg": s["平均token"],
            "cache_hit_rate": s["缓存命中率"],   # None = 无缓存数据
            # 推理 token 占总 completion 的比例。口径含摘要调用（那是一条独立的推理黑洞）。
            # None = 无 completion 数据，与 cache_hit_rate 同规范（区别「没有数据」和「比率为零」）。
            "reasoning_token_ratio": s["推理占比"],
        })
        return base

    def __repr__(self):
        s = self.summary()
        if s.get("样本数", 0) == 0:
            return "<MetricsStore 无样本>"
        return (
            f"<Metrics 样本={s['样本数']} 成功率={s['技术成功率']} "
            f"平均延迟={s['平均延迟(秒)']}s P99={s['P99延迟(秒)']}s 平均token={s['平均token']}>"
        )
