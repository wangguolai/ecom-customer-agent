# -*- coding: utf-8 -*-
"""滑动窗口限流 —— Redis ZSET（模块 5）

用 Redis ZSET 实现滑动窗口：score=请求时间戳，member=唯一标识。
每次请求：删窗口外的 → 数窗口内 → 判断是否超限 → 放行则加当前请求。

为什么滑动窗口不用固定窗口（INCR + EXPIRE）：固定窗口有「临界点双倍流量」问题——
窗口边界两侧的请求各自计数，瞬时能放进 2 倍阈值。滑动窗口对任意时间窗都精确封顶。

原子性说明（诚实）：ZREMRANGEBYSCORE + ZCARD 用 pipeline 只减少 RTT + 保证这两步原子，
但「数 → Python 判断 → 决定是否 ZADD」的决策在 Python 侧，check-then-add 竞态是固有的，
pipeline 消不掉。demo 接受（并发 k 最多超 k-1），严格原子要 Lua 脚本。

fail-open：Redis 不可用放行（记日志不抛）——限流是「保护」不是「正确性」，
挂了宁可放行不能全挂（对齐 cache.py 旁路降级哲学）。
"""

import sys
import os
import time
import uuid

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import redis
from src.backend.cache import _redis as _redis_client


def rate_limit(bucket: str, client: str, window: int, max_req: int) -> bool:
    """滑动窗口限流。返回 True=放行，False=超限（429）。

    bucket: 桶名（"read" / "write"）
    client: 客户端标识（demo 固定，生产 per-IP；全局桶挡不住单客户端刷，防刷靠 per-IP 粒度）
    window: 窗口秒数
    max_req: 窗口内最大请求数
    """
    key = f"rl:{bucket}:{client}"
    now = time.time()
    window_start = now - window

    try:
        # 删窗口外 + 数窗口内（pipeline 减 RTT + 这两步原子）
        pipe = _redis_client.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        _, count = pipe.execute()
    except redis.exceptions.RedisError:
        # fail-open：Redis 挂放行，记日志。限流是保护不是正确性，宁可放行不能全挂。
        print(f"⚠️ 限流 Redis 不可用，放行（fail-open）：{key}")
        return True

    if count >= max_req:
        return False

    # 放行：加当前请求。member 必须每次唯一（uuid4），不能用 str(time.time())——
    # 同毫秒两请求共用 member 会互相覆盖 → 少计数 → 限流失效。
    try:
        pipe = _redis_client.pipeline()
        pipe.zadd(key, {str(uuid.uuid4()): now})
        # key 过期：rl:* 逃过 cache.flush() 的 ecom:* 清理，必须靠 EXPIRE 防无界增长
        pipe.expire(key, window + 60)
        pipe.execute()
    except redis.exceptions.RedisError:
        # 计数写失败 = 这次请求没被计数（少计一个），不影响放行决策，记日志即可
        print(f"⚠️ 限流计数写入失败（降级忽略）：{key}")
    return True
