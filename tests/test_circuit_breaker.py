# -*- coding: utf-8 -*-
"""熔断器测试 —— 三态流转 + 连续失败语义 + 半开单飞

纯单元测试，不需要后端/Redis，直接测 CircuitBreaker 状态机。
cooldown 用构造参数控制：cooldown=0 让「冷却立即到」，cooldown=999 测「冷却内拒绝」，不真等时间。
"""

import sys
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.circuit_breaker import CircuitBreaker


def main():
    print("📍 [1/5] 初始 closed：allow 放行")
    cb = CircuitBreaker(fail_threshold=3, cooldown=0)
    assert cb.state == "closed"
    assert cb.allow() is True
    print("  ✅ closed 状态 allow=True")

    print("📍 [2/5] 连续失败达阈值 → open")
    cb.record_failure()  # 1
    assert cb.state == "closed", "失败 1 次还在 closed"
    cb.record_failure()  # 2
    cb.record_failure()  # 3 → open
    assert cb.state == "open", f"失败 3 次应 open，实际 {cb.state}"
    print("  ✅ 连续失败 3 次 → open")

    print("📍 [3/5] open 冷却内 → allow 拒绝（快速失败）")
    cb2 = CircuitBreaker(fail_threshold=3, cooldown=999)  # cooldown 大，冷却不到
    for _ in range(3):
        cb2.record_failure()
    assert cb2.state == "open"
    assert cb2.allow() is False, "open 冷却内应拒绝"
    print("  ✅ open 冷却内 allow=False（快速失败，不调下游）")

    print("📍 [4/5] 冷却到 → 半开单飞 → 成功回 closed")
    assert cb.allow() is True, "cooldown=0，open 应转 half_open 放探针"
    assert cb.state == "half_open"
    assert cb.allow() is False, "half_open 探针在飞，第二个请求应拒绝"
    cb.record_success()
    assert cb.state == "closed", "半开试探成功应回 closed"
    print("  ✅ 半开单飞（第二个请求被拒）+ 成功回 closed")

    print("📍 [5/5] 成功重置失败计数（连续失败）+ 半开失败重新 open")
    cb3 = CircuitBreaker(fail_threshold=3, cooldown=0)
    cb3.record_failure()  # 1
    cb3.record_failure()  # 2
    cb3.record_success()  # 重置 → 0（连续失败语义：成功清零，不是累计失败）
    cb3.record_failure()  # 1
    cb3.record_failure()  # 2
    assert cb3.state == "closed", "失败 2-成功-失败 2 不应 open（成功重置了计数）"
    cb4 = CircuitBreaker(fail_threshold=3, cooldown=0)
    for _ in range(3):
        cb4.record_failure()
    assert cb4.state == "open"
    assert cb4.allow() is True  # → half_open
    cb4.record_failure()  # 半开试探失败 → 重新 open
    assert cb4.state == "open", "半开试探失败应重新 open"
    print("  ✅ 成功重置计数（连续失败）+ 半开失败重新 open")

    print("🎉 熔断器三态流转：全部通过")


if __name__ == "__main__":
    main()
