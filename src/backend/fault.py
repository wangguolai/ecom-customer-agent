# -*- coding: utf-8 -*-
"""故障注入状态管理 —— 评测用调试设施

让后端在「接下来 N 次请求」返回故障（超时/脏数据/空数据），
供评测脚本让 TOOL_FAILURE_CASES 跑出「工具超时/返脏/空结果」的标称场景。

和 ratelimit.py 同类：进程级内存状态，不进 DB/Redis。
生产关闭：由 main.py 用 ENABLE_DEBUG_FAULT 环境变量门控路由，本模块本身无副作用。
"""

import sys
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 全局故障状态：{target: {"mode": "timeout"|"dirty"|"empty", "count": N}}
# 线程安全：FastAPI 同步端点跑线程池，apply_fault 的「读-改-写」非原子，多请求并发会丢计数。
# demo 评测串行不触发，但和 circuit_breaker.py 的原子性说明对照，这里显式加锁（成本极低）。
_FAULTS = {}
_lock = threading.Lock()


def set_fault(target: str, mode: str, count: int):
    """设置 target 接下来 count 次请求注入 mode 故障。target ∈ orders/logistics/products/refund"""
    with _lock:
        _FAULTS[target] = {"mode": mode, "count": count}


def apply_fault(target: str):
    """每次请求前调用。返回要注入的 mode（str）或 None（不注入）。命中则递减 count，用完自动清除。"""
    with _lock:
        f = _FAULTS.get(target)
        if not f:
            return None
        f["count"] -= 1
        if f["count"] <= 0:
            _FAULTS.pop(target, None)
        return f["mode"]


def clear_fault(target: str = None):
    """清除故障。target=None 清全部。"""
    with _lock:
        if target is None:
            _FAULTS.clear()
        else:
            _FAULTS.pop(target, None)
