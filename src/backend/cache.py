# -*- coding: utf-8 -*-
"""Redis 缓存封装 —— 进程级单例连接池 + 统一 JSON + 空值哨兵 + 降级 + 击穿防护 + 雪崩抖动

缓存是旁路、可重建副本（真源 MySQL），不是第二真相：
- get 连接异常吞掉返回 miss，请求回落 MySQL，正确性不受影响
- set 失败只记日志不抛
空值哨兵（三元语义）：None=miss、__EMPTY__=命中空标记（不存在）、JSON=真实数据。

防护三件套（对应「缓存三兄弟」）：
- 穿透（不存在的数据反复查）→ 空值哨兵 set_empty
- 击穿（热点 key 过期瞬间并发回源）→ 互斥锁 get_or_rebuild
- 雪崩（大量 key 同一时刻集体失效）→ TTL 随机抖动 _jittered_ttl
"""

import sys
import os
import json
import time
import uuid
import random

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
import redis

load_dotenv(os.path.join(_project_root, ".env"))

# 空值哨兵（区分「不存在」和「真实数据」；qty=0 是真实数据，正常缓存 {"qty": 0}）
EMPTY = "__EMPTY__"

# 雪崩防护：TTL 随机抖动幅度（秒）。让同类/热点 key 的过期时间散开，
# 避免同一时刻集体失效 → 流量瞬间全部回源 DB（缓存雪崩）。
JITTER_SECONDS = 5


def _jittered_ttl(ttl: int) -> int:
    """TTL 加 ±JITTER_SECONDS 随机抖动（防雪崩），下限钳到 1s。

    用 randint 而非 int(uniform(...))：uniform 上界开区间 + int 向零截断会让实际范围
    变成 [ttl-5, ttl+4]，和「±5」注释差 1s。randint(-5,5) 闭区间精确对称。
    """
    return max(1, ttl + random.randint(-JITTER_SECONDS, JITTER_SECONDS))


# 进程级单例连接（类似 db.py 的 _pool；redis-py 连接池线程安全，不每请求重建）
# decode_responses=True：返回 str 而非 bytes，json.loads 直接可用
_redis = redis.Redis(
    host=os.environ.get("REDIS_HOST", "127.0.0.1"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    decode_responses=True,
    protocol=2,  # Redis 5.x 不支持 RESP3（HELLO 3 报错），强制 RESP2 兼容
    # 超时必须设：redis-py 默认 socket_timeout=None（无限阻塞），Redis 半开连接会挂死线程池。
    # 缓存是旁路，超时后降级查库，不能拖垮真源读。
    socket_connect_timeout=0.5,
    socket_timeout=1,
    # 必须关掉默认重试：redis-py 8.1 的 Redis() 默认 retry=Retry(ExponentialWithJitterBackoff(), 10)，
    # 连接失败会退避重试 10 次（实测每次操作 ~9s）。降级场景下重试只会放大卡顿，违背旁路原则——
    # Redis 恢复后连接池自会建新连接，不需要重试。
    retry=None,
)


def get_json(key: str):
    """读缓存，返回 (hit, value)：
    - (False, None) = miss（未命中，或 Redis 不可用降级）
    - (True, None)  = 命中空标记（表示「不存在」）
    - (True, data)  = 命中真实数据
    Redis 连接异常吞掉返回 miss——缓存是旁路，挂了降级查库，不拖垮真源读。
    """
    try:
        raw = _redis.get(key)
    except redis.exceptions.RedisError:
        # 放宽到 RedisError：TimeoutError 不是 ConnectionError 子类，Redis 半开连接/网络分区会抛它，
        # 只捕 ConnectionError 会让超时冒出去，不再降级反而挂死线程池。
        return False, None
    if raw is None:
        return False, None
    if raw == EMPTY:
        return True, None
    try:
        return True, json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False, None


def set_json(key: str, value, ttl: int):
    """json.dumps 后存真实数据。失败只记日志不抛（缓存写失败不影响正确性）。

    显式拒绝 None：json.dumps(None)='null'，读回 loads('null')=None 会和空值哨兵 (True, None)
    撞车，被调用方误判成「不存在」。None 语义由 set_empty 独占。

    ttl 经 _jittered_ttl 加随机抖动（防雪崩），调用方传的是基准 TTL，实际写入 ±JITTER_SECONDS。
    """
    if value is None:
        raise ValueError(f"set_json 不支持 None 值（会与空值哨兵冲突）：{key}")
    try:
        _redis.set(key, json.dumps(value, ensure_ascii=False), ex=_jittered_ttl(ttl))
    except redis.exceptions.RedisError:
        print(f"⚠️ Redis 写入失败（降级忽略）：{key}")


def set_empty(key: str, ttl: int):
    """存空标记（表示「不存在」，防穿透）。"""
    try:
        _redis.set(key, EMPTY, ex=_jittered_ttl(ttl))
    except redis.exceptions.RedisError:
        # 对齐 set_json：空标记写失败 = 多查一次 MySQL（不降级），但要打日志可排障
        print(f"⚠️ Redis 空标记写入失败（降级忽略）：{key}")


# ── 击穿防护（互斥锁 + 双重检查）──
# 热点 key 过期瞬间，N 个并发请求同时 miss → 同时回源 DB，把 DB 打爆（缓存击穿）。
# 用 Redis SET NX EX 做重建锁：同一时刻只有一个线程回源，其余线程等待重试读缓存。
#
# 为什么用 Redis 锁而不是 threading.Lock：demo 单进程内线程锁够用，但生产多实例部署
# （uvicorn 多 worker / 多容器）进程内锁互不可见，击穿防护失效——必须用 Redis 做跨进程
# 分布式锁，这是「分布式锁」的动机。锁 value 存 token，释放时校验 value 才 DEL（Lua 原子），
# 防「持锁线程 A 超时被抢锁线程 B 顶替，A 又回来 DEL 掉 B 的锁」。
LOCK_PREFIX = "ecom:lock:"
LOCK_TTL = 5          # 重建锁超时：持锁线程崩溃，锁最多卡 5s 自动释放，不永久阻塞
LOCK_WAIT = 0.05      # 没抢到锁的线程重试读缓存间隔
LOCK_RETRY = 40       # 最多重试 40 次（约 2s），仍 miss 则回源兜底（正确性优先）
# 重试窗口 2s < 锁 TTL 5s 的权衡：持锁线程回源超过 2s（慢 SQL）时，等待线程会提前放弃、
# 回源兜底 → 多线程并发回源，击穿防护退化。demo 回源是毫秒级本地 MySQL 查询，2s 绰绰有余；
# 生产慢回源要么调大 LOCK_RETRY 对齐 LOCK_TTL，要么给回源加超时/熔断。这是「分布式锁
# 重试窗口 vs 锁超时」的经典要点，要能说明这个退化边界。

_LOCK_FALLBACK = "__fallback__"  # Redis 不可用时锁降级标记：表示「直接回源，无锁」


def _lock_key(key: str) -> str:
    return f"{LOCK_PREFIX}{key}"


def acquire_rebuild_lock(key: str):
    """抢重建锁（SET NX EX）。返回 token（锁 value）/ None（没抢到）/ _LOCK_FALLBACK（Redis 不可用降级）。

    Redis 不可用返回 _LOCK_FALLBACK（≠None）→ 调用方直接回源：击穿防护让位于正确性，
    缓存本来就是旁路，锁失效不能挡着业务不查库。
    """
    token = uuid.uuid4().hex
    try:
        acquired = _redis.set(_lock_key(key), token, nx=True, ex=LOCK_TTL)
    except redis.exceptions.RedisError:
        return _LOCK_FALLBACK
    return token if acquired else None


def release_rebuild_lock(key: str, token: str):
    """释放重建锁（Lua 原子：value == 自己的 token 才 DEL，防误删别人重抢的锁）。

    释放失败（Redis 挂/锁已被顶替）静默：锁靠 EX 超时兜底，不影响正确性。
    """
    if token == _LOCK_FALLBACK:
        return
    lua = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    else
        return 0
    end
    """
    try:
        _redis.eval(lua, 1, _lock_key(key), token)
    except redis.exceptions.RedisError:
        pass


def get_or_rebuild(key: str, ttl: int, rebuild, empty_ttl: int = 30):
    """击穿防护读：命中直接返回；miss 后抢重建锁 + 双重检查，同一时刻只有一个线程回源 DB。

    rebuild() -> (found, data)：
      found=True  → data 是真实数据，写缓存返回 (True, data)
      found=False → 写空标记返回 (True, None)（命中「不存在」，调用方照旧判 data is None 抛 404）
    返回值对齐 get_json 的 (hit, value) 语义（此处 hit 恒 True：要么命中，要么刚回源重建写缓存）。

    击穿防护三段（设计要点）：
      1. 抢重建锁（SET NX EX）——只有一个线程负责回源，其余等待重试
      2. 双重检查——拿到锁后再读一次缓存，可能等待期间别的线程已写好，避免重复回源
      3. 锁超时 + 降级——锁 EX 超时兜底（持锁崩溃不卡死）；Redis 不可用锁降级为直接回源
    """
    hit, data = get_json(key)
    if hit:
        return hit, data

    token = acquire_rebuild_lock(key)
    if token is None:
        # 没抢到锁：别的线程在回源重建，有限重试读缓存，避免无脑回源
        for _ in range(LOCK_RETRY):
            time.sleep(LOCK_WAIT)
            hit, data = get_json(key)
            if hit:
                return hit, data
        # 重试仍 miss：锁可能超时/持锁线程极慢，直接回源兜底（正确性优先，击穿防护让位）

    # 到这里 token 可能是 _LOCK_FALLBACK（锁降级）、真实 token（抢到锁）、或 None（重试超时）。
    # 双重检查：抢到锁的线程回源前再读一次缓存，等待期间别的线程可能已写好。
    if token and token != _LOCK_FALLBACK:
        hit, data = get_json(key)
        if hit:
            release_rebuild_lock(key, token)
            return hit, data

    try:
        found, data = rebuild()
        if found:
            set_json(key, data, ttl)
            result = (True, data)
        else:
            set_empty(key, empty_ttl)
            result = (True, None)
    finally:
        # rebuild 抛异常（MySQL 挂/池耗尽）也必须释放锁——锁有 EX 超时兜底，
        # 但「持锁必须释放」不变量不能破，否则锁残留 LOCK_TTL 秒内其它线程白等。
        if token and token != _LOCK_FALLBACK:
            release_rebuild_lock(key, token)
    return result


def flush():
    """清空本业务缓存（refresh / backend 启动重建真源时调用，SSOT 单向链路闭环）。

    只清 ecom:* 命名空间，不用 flushdb() 全清——未来模块 4/5/6 复用同一 Redis
    （MQ list / 限流计数 / 分布式锁）时，全清会误删别家数据，「ecom: 前缀防冲突」就白设计了。
    """
    try:
        keys = list(_redis.scan_iter("ecom:*"))
        if keys:
            _redis.delete(*keys)
    except redis.exceptions.RedisError:
        print("⚠️ Redis 清空失败（降级忽略）")


def ping() -> bool:
    """探活（供 lifespan 启动时打日志）。失败返回 False 不抛——缓存是旁路，起不来就降级查库。"""
    try:
        return bool(_redis.ping())
    except redis.exceptions.RedisError:
        return False
