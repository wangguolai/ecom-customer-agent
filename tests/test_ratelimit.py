# -*- coding: utf-8 -*-
"""限流测试 —— 滑动窗口（Redis ZSET）：窗口内超阈值拒绝 + 窗口滑动恢复放行

前置：Redis 已起。直接测 rate_limit 函数（不经 FastAPI），窗口用 1s + 阈值 3，不真等长时间。
"""

import sys
import os
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.backend.ratelimit import rate_limit
from src.backend.cache import _redis as _redis_client


def main():
    bucket = "test"
    client = "test_client"
    key = f"rl:{bucket}:{client}"
    _redis_client.delete(key)  # 清残留，保证测试干净

    window = 1  # 1 秒窗口
    max_req = 3  # 最多 3 次

    print("📍 [1/2] 窗口内超阈值 → 拒绝")
    results = [rate_limit(bucket, client, window, max_req) for _ in range(5)]
    assert results[:3] == [True, True, True], f"前 3 次应放行，实际 {results[:3]}"
    assert results[3:] == [False, False], f"后 2 次应拒绝，实际 {results[3:]}"
    print(f"  ✅ 窗口内 5 连发：前 3 放行，后 2 拒绝（{results}）")

    print("📍 [2/2] 窗口滑动后 → 恢复放行")
    time.sleep(window + 0.1)  # 等窗口滑过（1s 窗口，睡 1.1s）
    assert rate_limit(bucket, client, window, max_req) is True, "窗口滑过后应恢复放行"
    print("  ✅ 窗口滑过后恢复放行")

    _redis_client.delete(key)  # 清理测试 key
    print("🎉 滑动窗口限流：全部通过")


if __name__ == "__main__":
    main()
