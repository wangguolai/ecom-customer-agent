# -*- coding: utf-8 -*-
"""Redis 缓存封装 —— 进程级单例连接池 + 统一 JSON + 空值哨兵 + 降级

缓存是旁路、可重建副本（真源 MySQL），不是第二真相：
- get 连接异常吞掉返回 miss，请求回落 MySQL，正确性不受影响
- set 失败只记日志不抛
空值哨兵（三元语义）：None=miss、__EMPTY__=命中空标记（不存在）、JSON=真实数据。
"""

import sys
import os
import json

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
    """
    if value is None:
        raise ValueError(f"set_json 不支持 None 值（会与空值哨兵冲突）：{key}")
    try:
        _redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
    except redis.exceptions.RedisError:
        print(f"⚠️ Redis 写入失败（降级忽略）：{key}")


def set_empty(key: str, ttl: int):
    """存空标记（表示「不存在」，防穿透）。"""
    try:
        _redis.set(key, EMPTY, ex=ttl)
    except redis.exceptions.RedisError:
        # 对齐 set_json：空标记写失败 = 多查一次 MySQL（不降级），但要打日志可排障
        print(f"⚠️ Redis 空标记写入失败（降级忽略）：{key}")


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
