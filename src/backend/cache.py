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
    except redis.exceptions.ConnectionError:
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
    """json.dumps 后存真实数据。失败只记日志不抛（缓存写失败不影响正确性）。"""
    try:
        _redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
    except redis.exceptions.ConnectionError:
        print(f"⚠️ Redis 写入失败（降级忽略）：{key}")


def set_empty(key: str, ttl: int):
    """存空标记（表示「不存在」，防穿透）。"""
    try:
        _redis.set(key, EMPTY, ex=ttl)
    except redis.exceptions.ConnectionError:
        pass


def flush():
    """清空缓存（refresh 重建真源时调用，SSOT 单向链路闭环）。"""
    try:
        _redis.flushdb()
    except redis.exceptions.ConnectionError:
        print("⚠️ Redis FLUSHDB 失败（降级忽略）")
