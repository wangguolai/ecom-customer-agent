# -*- coding: utf-8 -*-
"""agent 全链路压测 —— 并发多 session，量化端到端延迟 + 成本 + 找并发竞态

和 bench_backend.py 的区别：那个只压 FastAPI 接口（不走 LLM，零成本）；
这个走完整 agent 链路（意图路由 → 工具 → ReAct → LLM），会花钱，所以**规模刻意压小**。

看三件事：
  1. 端到端延迟分布（P50/P95）+ 每次 token 消耗 → 真实成本画像
  2. 前缀缓存命中率（多 session 串行/并发时 system prompt 前缀是否命中缓存）
  3. 并发下有无新竞态 —— asyncio 改造时踩过一次（Qdrant 懒加载单例无锁 → 文件锁冲突），
     这次验证多 session 并发无新问题

用法：python tests/bench_agent.py [--n N] [--conc C]   # 默认 3 路并发 × 3 轮 = 9 次
"""

import sys
import os
import asyncio
import time
import json

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.agent import AgentSession

# 固定用同一批 query，让 system prompt 前缀可命中 DeepSeek 前缀缓存（省钱 + 观测缓存命中率）
_QUERIES = [
    "查一下订单 20240818001 到哪了",
    "皇家牌的狗粮有哪些",
    "豆腐猫砂还有货吗",
]


async def _one(query):
    """单次完整链路，返回 (耗时, trace_summary, answer)"""
    t0 = time.perf_counter()
    session = AgentSession()
    answer = await session.chat(query)
    elapsed = time.perf_counter() - t0
    trace = session.get_last_trace()
    return elapsed, trace.summary() if trace else {}, answer


async def run(n_per_round: int, rounds: int):
    print("=" * 80)
    print(f"agent 全链路压测：{n_per_round} 路并发 × {rounds} 轮 = {n_per_round * rounds} 次（付费）")
    print("=" * 80)

    latencies = []
    tokens = []
    cache_hit = 0
    cache_miss = 0
    tool_counts = []
    end_reasons = []
    anomalies = []

    for r in range(1, rounds + 1):
        print(f"📍 [{r}/{rounds}] 第 {r} 轮（{n_per_round} 路并发）...")
        queries = [_QUERIES[i % len(_QUERIES)] for i in range(n_per_round)]
        results = await asyncio.gather(*[_one(q) for q in queries])
        for elapsed, ts, answer in results:
            latencies.append(elapsed)
            tokens.append(ts.get("总 token 消耗", 0))
            cache_hit += ts.get("缓存命中 token", 0)
            cache_miss += ts.get("缓存未命中 token", 0)
            tool_counts.append(ts.get("工具调用次数", 0))
            end = ts.get("结束原因", "未知")
            end_reasons.append(end)
            if end != "正常":
                anomalies.append({"end_reason": end, "answer": answer[:120]})

    def pct(vals, p):
        vals = sorted(vals)
        if not vals:
            return 0.0
        return vals[min(len(vals) - 1, int((len(vals) - 1) * p / 100))]

    total = len(latencies)
    cache_total = cache_hit + cache_miss
    print("=" * 80)
    print(f"样本数        = {total}")
    print(f"P50 延迟      = {pct(latencies, 50):.2f}s")
    print(f"P95 延迟      = {pct(latencies, 95):.2f}s")
    print(f"P99 延迟      = {pct(latencies, 99):.2f}s")
    print(f"平均延迟      = {sum(latencies)/total:.2f}s")
    print(f"平均 token    = {sum(tokens)/total:.1f}")
    print(f"前缀缓存命中率 = {cache_hit/cache_total:.1%}" if cache_total else "前缀缓存命中率 = 无数据")
    print(f"平均工具调用  = {sum(tool_counts)/total:.1f}")
    abnormal = [a for a in anomalies]
    print(f"技术成功率    = {(total - len(abnormal))/total:.1%}（结束原因=正常）")
    if abnormal:
        print(f"⚠️ 异常样本：{abnormal}")
    print("=" * 80)

    # 落盘
    out_dir = os.path.join(_project_root, "tests", "eval_results")
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n": total, "concurrency": n_per_round,
        "p50": round(pct(latencies, 50), 3), "p95": round(pct(latencies, 95), 3),
        "p99": round(pct(latencies, 99), 3), "avg": round(sum(latencies)/total, 3),
        "avg_tokens": round(sum(tokens)/total, 1),
        "cache_hit_rate": round(cache_hit/cache_total, 4) if cache_total else 0,
        "tech_success_rate": round((total - len(abnormal))/total, 4),
        "anomalies": anomalies,
    }
    path = os.path.join(out_dir, "bench_agent.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"📁 结果已落盘：{path}")


if __name__ == "__main__":
    from src.infra.warmup import warmup_models
    warmup_models()  # 主线程预热 embedding + rerank，避免并发 to_thread 里首次加载 CUDA 死锁
    n = 3
    rounds = 3
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    if "--conc" in sys.argv:
        n = int(sys.argv[sys.argv.index("--conc") + 1])
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])
    asyncio.run(run(n, rounds))
