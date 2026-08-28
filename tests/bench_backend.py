# -*- coding: utf-8 -*-
"""后端接口压测脚本 —— 纯本地压测，零 LLM 成本（不调 agent、不调 LLM/embedding）

只压 FastAPI 后端读接口（src/backend/main.py）：
  GET /orders/{order_id}     GET /logistics/{order_id}     GET /products/{product_id}

三档场景：
  1. 热缓存档：重复打同一批真实 id，测 Redis 命中延迟。真实 id 批为 7 个 URL：
     3 订单 × /orders + 1 订单 × /logistics + 3 商品 × /products。
     logistics 种子只有 20240818001 有轨迹数据（seed.py），另两个订单的物流是合法 404，
     不是「命中」样本，不纳入批（否则成功率和延迟都被 404 污染）。
     分 4 轮 × 90 次 = 360 样本，轮间 sleep 11s 让限流滑动窗口（read 桶 10s）滑过，规避 429。
     （初稿的「300 样本」被分轮策略具体化为 4×90=360，以严格低于 read 桶 100 次/10s 阈值。）
  2. 冷缓存档：用真实 id，但每次请求前清 ecom:* 缓存（redis 库 scan_iter + delete，只清 ecom
     前缀，绝不 FLUSHDB——会误删 rl:* 限流计数和 mq:* 在途消息），串行 30 样本测 MySQL 回源延迟。
     小样本，只报 P50/P95，注明样本量小。
  3. 限流验证档（最后跑）：先清 rl:*，再 10s 内故意打 200 次真实 id，统计 429 起始位置与 429 率，
     证明 read 桶 100 次/10s 限流真实生效。

档间污染规避：
  - 热档分轮跑（每轮 90 < 100），轮间 sleep 11s 让滑动窗口滑过，共 4 轮 = 360 样本。
    热档统计排除 429 样本、单独报 429 数；若热档出现 429 醒目报警，但归因要二分（见下）。
  - 热档开始前清一次 rl:*（测试隔离：read 桶从 0 计数，分轮验证才有效；不动 ecom:* 数据缓存）。
  - 冷档开始前也清一次 rl:*（热档第 4 轮刚把 read 桶打到 ~99/100，不清冷档前两请求就被 429 淹没）。
  - 限流档跑之前清 rl:*。

环境注意（本次实测踩到，务必如实归因）：
  后端限流在路由层运行（_limit_read 在参数校验之前），所以对 /orders/* 的 400/404 攻击探针
  （SQLi / XSS / 路径穿越等安全扫描）同样消耗 read 桶 rl:read:demo。实测期间外部扫描器持续打
  /orders/*（docker logs 可见 172.18.0.1 的探针流），会与压测请求争抢同一个桶，造成间歇性 429。
  因此热档若报 429，先分两个可能原因排查：① 分轮策略失效（每轮 97 请求逼近 100/10s 阈值）；
  ② 外部扫描器污染共享限流桶（400/404 也计数）。不能只归因前者。

归因诚实（关键）：
  并发 50 时 P99 抬头是「AnyIO 线程池(40) + MySQL 连接池(5)」两个饱和点叠加，不能单独归因连接池。
  所以默认并发 30（<40，把连接池效应隔离出来——线程池未饱和，P99 变化主要反映连接池争用）；
  另可加 --conc 50 跑一档看两个饱和点叠加效应。归因时必须两因素一起说。

统计规范：
  - 分位用最近秩近似（同 src/infra/observability.py 的 MetricsStore._percentile）。
  - 热档 QPS = 实测样本数 / 活跃时长（活跃时长只计打请求的时间，排除轮间 sleep 与预热）。
  - 产品缓存 TTL 30s、热档总时长约 33s 会自然过期；每轮前先打 len(TARGET_URLS)=7 次预热请求
    （不计入统计）刷新全部键，保证热档测到的都是 Redis 命中延迟。

用法：
  python tests/bench_backend.py                   # 默认并发 30，三档全跑
  python tests/bench_backend.py --conc 50         # 看线程池+连接池叠加饱和效应
  python tests/bench_backend.py --skip-ratelimit  # 只测性能（跳过限流档）

  环境变量：BACKEND_URL 默认 http://localhost:8000
            REDIS_HOST / REDIS_PORT 默认 127.0.0.1:6379
  结果落盘：tests/eval_results/bench_backend.json（summary + 分档明细）
"""

import sys
import os
import json
import time
import asyncio
import argparse
import datetime

import httpx
import redis

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REQUEST_TIMEOUT = 5.0  # 秒；本地压测宽裕点，避免误判超时

# 种子真实 id（见 src/backend/seed.py）：3 订单 + 3 商品
ORDER_IDS = ["20240818001", "20240817002", "20240816003"]
PRODUCT_IDS = ["P001", "P002", "P003"]

# 读接口 URL 批（真实 id，全部返回 200 的「命中」样本）：
#   3 订单 × /orders + 1 订单 × /logistics + 3 商品 × /products = 7 个 URL
# logistics 种子只有 20240818001 有轨迹数据（src/backend/seed.py 的 _SEED_LOGISTICS），
# 另两个订单的物流是合法 404（无轨迹记录），不是「命中」样本，不纳入热/冷缓存批。
LOGISTICS_ORDER = "20240818001"
TARGET_URLS = (
    [f"{BACKEND_URL}/orders/{oid}" for oid in ORDER_IDS]
    + [f"{BACKEND_URL}/logistics/{LOGISTICS_ORDER}"]
    + [f"{BACKEND_URL}/products/{pid}" for pid in PRODUCT_IDS]
)

# 结果落盘路径
OUT_PATH = os.path.join(_project_root, "tests", "eval_results", "bench_backend.json")

# Redis 客户端（模块级，连接池线程安全；冷档清缓存经 asyncio.to_thread 跨线程复用）
# retry=None：失败不重试（同 src/backend/cache.py 的降级哲学），避免连接失败被退避放大卡顿
_redis = redis.Redis(
    host=REDIS_HOST, port=REDIS_PORT, decode_responses=True,
    socket_connect_timeout=1, socket_timeout=1, retry=None,
)


def _percentile(values, p):
    """最近秩近似 P 分位（同 src/infra/observability.py MetricsStore._percentile）"""
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int((len(values) - 1) * p / 100))
    return values[idx]


def _ms(seconds):
    return round(seconds * 1000, 2)


def _clear_prefix(prefix):
    """只清指定前缀的 key。绝不 FLUSHDB —— rl:* 限流计数 / mq:* 在途消息会被误删。"""
    try:
        keys = list(_redis.scan_iter(prefix + "*", count=500))
        if keys:
            _redis.delete(*keys)
        return len(keys)
    except redis.RedisError as e:
        print(f"⚠️ Redis 清 {prefix} 失败: {e}")
        return -1


def _redis_alive() -> bool:
    try:
        return bool(_redis.ping())
    except redis.RedisError:
        return False


async def _fire(client, urls, conc):
    """并发打一批 URL，返回 [(status_code, elapsed)]；status=0 表示网络/异常。

    elapsed 从任务创建（含排队等信号量）到响应回来，是「负载下的请求感知延迟」——
    并发 50 时 P99 抬头同时反映排队（AnyIO 线程池饱和）+ 处理（MySQL 连接池争用）。
    """
    sem = asyncio.Semaphore(conc)
    out = [None] * len(urls)

    async def _one(i, url):
        t0 = time.perf_counter()
        try:
            async with sem:
                resp = await client.get(url)
            out[i] = (resp.status_code, time.perf_counter() - t0)
        except httpx.HTTPError:
            out[i] = (0, time.perf_counter() - t0)

    await asyncio.gather(*[_one(i, u) for i, u in enumerate(urls)])
    return out


# ═══════════════════════════════════════════════════════════════
# 三档场景
# ═══════════════════════════════════════════════════════════════

async def run_hot_cache(client, conc, scenario_idx, scenario_total):
    """档 1：热缓存（Redis 命中延迟）。4 轮 × 90，轮间 sleep 11s。"""
    rounds = 4
    per_round = 90
    total = rounds * per_round
    lat = []       # 成功(200) 样本延迟（秒）
    n_429 = 0
    n_other = 0
    active_time = 0.0

    print(f"📍 [{scenario_idx}/{scenario_total}] 热缓存档 — {rounds} 轮 × {per_round} 次 = {total} 样本；先清 rl:* 做测试隔离，轮间 sleep 11s")
    await asyncio.to_thread(_clear_prefix, "rl:*")

    for rnd in range(1, rounds + 1):
        print(f"📍 [{rnd}/{rounds}] 热缓存第 {rnd} 轮 — 预热 {len(TARGET_URLS)} 键 + 打 {per_round} 次（并发 {conc}）")
        # 预热（不计入统计）：len(TARGET_URLS) 个 URL 各打 1 次，刷新 30s TTL 的产品键，保证实测全是命中
        await _fire(client, TARGET_URLS, 1)
        urls = [TARGET_URLS[i % len(TARGET_URLS)] for i in range(per_round)]
        t0 = time.perf_counter()
        results = await _fire(client, urls, conc)
        active_time += time.perf_counter() - t0
        for status, elapsed in results:
            if status == 429:
                n_429 += 1
            elif status == 200:
                lat.append(elapsed)
            else:
                n_other += 1
        if rnd < rounds:
            await asyncio.sleep(11)

    ok = len(lat)
    success_rate = ok / total if total else 0.0
    qps = total / active_time if active_time else 0.0

    if n_429:
        alarm = ("⚠️ 热档出现 429 —— 二分归因：① 分轮策略失效（每轮 97 逼近 read 桶 100/10s）"
                 "？② 外部扫描器污染共享桶（400/404 探针也走 _limit_read 计数）？")
    else:
        alarm = "无（分轮策略有效）"

    return {
        "scenario": "热缓存档",
        "rounds": rounds,
        "per_round": per_round,
        "total": total,
        "warmup_per_round": len(TARGET_URLS),
        "ok": ok,
        "r429": n_429,
        "r_other_error": n_other,
        "success_rate": round(success_rate, 4),
        "p50_ms": _ms(_percentile(lat, 50) or 0),
        "p95_ms": _ms(_percentile(lat, 95) or 0),
        "p99_ms": _ms(_percentile(lat, 99) or 0),
        "avg_ms": _ms((sum(lat) / len(lat)) if lat else 0),
        "qps": round(qps, 1),
        "active_seconds": round(active_time, 3),
        "alarm": alarm,
    }


async def run_cold_cache(client, scenario_idx, scenario_total):
    """档 2：冷缓存（MySQL 回源）。串行 30 样本，每次请求前清 ecom:*。"""
    n = 30
    lat = []
    n_429 = 0
    n_other = 0
    cleared_total = 0
    t0 = time.perf_counter()

    print(f"📍 [{scenario_idx}/{scenario_total}] 冷缓存档 — 串行 {n} 样本，每次请求前清 ecom:*（只清 ecom 前缀，不 FLUSHDB）")
    # 测试隔离：清 rl:* —— 热档第 4 轮刚把 read 桶打到 ~99/100，不清会立刻被 429 淹没，冷档测不到回源延迟
    await asyncio.to_thread(_clear_prefix, "rl:*")
    for i in range(n):
        cleared = await asyncio.to_thread(_clear_prefix, "ecom:*")
        cleared_total += cleared
        url = TARGET_URLS[i % len(TARGET_URLS)]
        t_start = time.perf_counter()
        try:
            resp = await client.get(url)
            status = resp.status_code
        except httpx.HTTPError:
            status = 0
        elapsed = time.perf_counter() - t_start
        if status == 429:
            n_429 += 1
        elif status == 200:
            lat.append(elapsed)
        else:
            n_other += 1

    wall = time.perf_counter() - t0
    ok = len(lat)
    return {
        "scenario": "冷缓存档",
        "samples": n,
        "ok": ok,
        "r429": n_429,
        "r_other_error": n_other,
        "success_rate": round(ok / n, 4) if n else 0.0,
        "p50_ms": _ms(_percentile(lat, 50) or 0),
        "p95_ms": _ms(_percentile(lat, 95) or 0),
        "avg_ms": _ms((sum(lat) / len(lat)) if lat else 0),
        "qps_serial": round(n / wall, 2) if wall else 0.0,
        "wall_seconds": round(wall, 3),
        "total_cache_cleared": cleared_total,
        "note": "样本量小（30），P50/P95 仅供参考；含每次清缓存开销",
    }


async def run_ratelimit(client, scenario_idx, scenario_total):
    """档 3：限流验证（最后跑）。先清 rl:*，再 10s 内串行打 200 次，统计 429 起始位置与 429 率。

    串行原因：并发会引入 check-then-add 竞态，允许至多 conc-1 个额外请求通过（ratelimit.py 已诚实
    标注），会污染「阈值位置」的验证。串行下 read 桶 100/10s 应精确表现为前 100 次放行、之后 429。
    """
    hits = 200
    seq = []
    print(f"📍 [{scenario_idx}/{scenario_total}] 限流验证档 — 先清 rl:*，再 10s 内串行打 {hits} 次真实 id，统计 429 起始位置与 429 率")
    cleared = await asyncio.to_thread(_clear_prefix, "rl:*")
    t0 = time.perf_counter()
    for i in range(hits):
        url = TARGET_URLS[i % len(TARGET_URLS)]
        try:
            resp = await client.get(url)
            seq.append(resp.status_code)
        except httpx.HTTPError:
            seq.append(0)
    wall = time.perf_counter() - t0

    ok = sum(1 for s in seq if s == 200)
    n_429 = sum(1 for s in seq if s == 429)
    n_other = sum(1 for s in seq if s not in (200, 429))
    first_429 = seq.index(429) if 429 in seq else -1  # 0-based
    rl_rate = n_429 / hits if hits else 0.0

    return {
        "scenario": "限流验证档",
        "hits": hits,
        "ok": ok,
        "r429": n_429,
        "r_other_error": n_other,
        "rl_rate": round(rl_rate, 4),
        "first_429_index": first_429,  # read 桶 100/10s，期望 ≈ 100（放行 100 次后开始限流）
        "ok_before_first_429": first_429 if first_429 >= 0 else None,
        "elapsed_seconds": round(wall, 3),
        "window_slid_warning": "⚠️ 打满 200 次耗时超过 10s，滑动窗口已滑过，429 率可能偏低" if wall > 10.0 else "无",
        "rl_cleared_keys": cleared,
        "read_bucket": {"max_req": 100, "window_s": 10},
        "status_seq": seq,
    }


# ═══════════════════════════════════════════════════════════════
# 输出
# ═══════════════════════════════════════════════════════════════

def _print_table(title, rows):
    print(f"\n===== {title} =====")
    w = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {k:<{w}}  {v}")
    print()


def parse_args():
    p = argparse.ArgumentParser(description="后端接口压测（纯本地，零 LLM 成本）")
    p.add_argument("--conc", type=int, default=30,
                   help="并发数（默认 30 < AnyIO 线程池 40，隔离连接池效应；50 可看线程池+连接池叠加效应）")
    p.add_argument("--skip-ratelimit", action="store_true", help="跳过第 3 档限流验证（只测性能）")
    return p.parse_args()


async def main(args):
    if args.conc < 1:
        args.conc = 1

    print(f"压测目标: {BACKEND_URL}")
    print(f"并发: {args.conc}  跳过限流档: {args.skip_ratelimit}  Redis: {REDIS_HOST}:{REDIS_PORT}")
    print()

    if not _redis_alive():
        print("⚠️⚠️ Redis 不可用 —— 冷缓存档无法清缓存、限流档无法清 rl:*，结果不可信！请先启动 Redis 容器。")

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        # 后端探活
        try:
            probe = await client.get(f"{BACKEND_URL}/products/P001")
        except httpx.HTTPError:
            print("⚠️ 后端不可达，请先启动后端（docker compose up -d backend 或 uvicorn src.backend.main:app --port 8000）")
            return
        if probe.status_code == 404:
            print("⚠️ 后端探活返回 404 —— 检查 BACKEND_URL 是否指向 FastAPI 根（默认 http://localhost:8000）")

        total_nodes = 3 if not args.skip_ratelimit else 2
        scenarios = {}

        # 档 1：热缓存
        hot = await run_hot_cache(client, args.conc, 1, total_nodes)
        scenarios["hot_cache"] = hot
        _print_table("1. 热缓存档（Redis 命中延迟）", [
            ("样本数", f"{hot['total']}（{hot['rounds']} 轮 × {hot['per_round']}，另含 {hot['warmup_per_round']}×{hot['rounds']} 次预热不计入）"),
            ("成功(200)", f"{hot['ok']}"),
            ("成功率", f"{hot['success_rate']:.2%}"),
            ("P50", f"{hot['p50_ms']} ms"),
            ("P95", f"{hot['p95_ms']} ms"),
            ("P99", f"{hot['p99_ms']} ms"),
            ("平均", f"{hot['avg_ms']} ms"),
            ("QPS(活跃时段)", f"{hot['qps']} req/s"),
            ("429 数", f"{hot['r429']}"),
            ("其他错误", f"{hot['r_other_error']}"),
            ("警报", hot["alarm"]),
        ])

        # 档 2：冷缓存
        cold = await run_cold_cache(client, 2, total_nodes)
        scenarios["cold_cache"] = cold
        _print_table("2. 冷缓存档（MySQL 回源延迟，串行）", [
            ("样本数", f"{cold['samples']}（小样本，分位仅供参考）"),
            ("成功(200)", f"{cold['ok']}"),
            ("成功率", f"{cold['success_rate']:.2%}"),
            ("P50", f"{cold['p50_ms']} ms"),
            ("P95", f"{cold['p95_ms']} ms"),
            ("平均", f"{cold['avg_ms']} ms"),
            ("串行吞吐(含清缓存)", f"{cold['qps_serial']} req/s"),
            ("总耗时(含清缓存)", f"{cold['wall_seconds']} s"),
            ("累计清缓存 key 数", f"{cold['total_cache_cleared']}"),
            ("429 数", f"{cold['r429']}"),
        ])

        # 档 3：限流验证（默认跑，可 --skip-ratelimit 跳过）
        if not args.skip_ratelimit:
            rl = await run_ratelimit(client, 3, total_nodes)
            scenarios["ratelimit"] = rl
            _print_table("3. 限流验证档（read 桶 100 次/10s）", [
                ("总请求", f"{rl['hits']}（10s 内）"),
                ("200 放行", f"{rl['ok']}"),
                ("429 限流", f"{rl['r429']}"),
                ("429 率", f"{rl['rl_rate']:.2%}"),
                ("首次 429 位置(0-based)", f"{rl['first_429_index']}（期望 ≈ 100：放行 100 次后开始限流）"),
                ("放行数至首次 429", f"{rl['ok_before_first_429']}"),
                ("打满耗时", f"{rl['elapsed_seconds']} s"),
                ("滑窗告警", rl["window_slid_warning"]),
            ])

        # 落盘（summary + 分档明细）
        payload = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "config": {
                "backend_url": BACKEND_URL,
                "conc": args.conc,
                "redis": f"{REDIS_HOST}:{REDIS_PORT}",
                "skip_ratelimit": args.skip_ratelimit,
            },
            "summary": {
                "hot_cache": {k: hot[k] for k in ("total", "ok", "success_rate", "p50_ms", "p95_ms", "p99_ms", "qps", "r429")},
                "cold_cache": {k: cold[k] for k in ("samples", "ok", "success_rate", "p50_ms", "p95_ms")},
                **({"ratelimit": {k: rl[k] for k in ("hits", "ok", "r429", "rl_rate", "first_429_index", "elapsed_seconds")}} if not args.skip_ratelimit else {}),
            },
            "scenarios": scenarios,
        }
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"✅ 结果已落盘: {OUT_PATH}")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
