# -*- coding: utf-8 -*-
"""缓存击穿防护 + 雪崩 TTL 抖动对抗测试（cache 层单元测试，直连 Redis）

前置：Redis 在跑（无需 backend HTTP，本测试只 import cache 直连 Redis）。

对抗式断言（testing-default-happy-path，证明机制生效而非「能跑」）：
  1. 击穿收敛：20 线程并发 miss 同一热点 key，rebuild 只被调用 1 次（其余 19 在锁重试里读缓存）
  2. 快路径：缓存命中不触发 rebuild（不抢锁不回源）
  3. 雪崩抖动：写缓存后 TTL 落在 [base-5, base+5]
"""
import sys
import os
import time
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 确保项目根目录在 Python 路径中（直跑 python tests/xxx.py 时 sys.path[0]=tests/）
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.backend import cache


def _clean(key: str):
    cache._redis.delete(key)
    cache._redis.delete(cache._lock_key(key))


def test_stampede():
    """20 并发 miss → 回源只 1 次（互斥锁 + 双重检查）"""
    key = "ecom:test:stampede"
    _clean(key)
    counter = {"n": 0}
    counter_lock = threading.Lock()

    def rebuild():
        with counter_lock:
            counter["n"] += 1
        time.sleep(0.3)  # 放大回源窗口，模拟慢 DB，给其余线程重试读缓存留时间
        return True, {"v": 1}

    threads = [threading.Thread(target=lambda: cache.get_or_rebuild(key, 60, rebuild)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counter["n"] == 1, f"击穿防护失效：20 并发 miss 回源 {counter['n']} 次（应 1 次）"
    print(f"  ✅ 20 并发 miss，回源仅 {counter['n']} 次（互斥锁 + 双重检查收敛）")
    _clean(key)


def test_hit_fastpath():
    """缓存命中不触发 rebuild"""
    key = "ecom:test:fastpath"
    cache._redis.delete(key)
    cache.set_json(key, {"v": "pre"}, 60)
    counter = {"n": 0}

    def rebuild():
        counter["n"] += 1
        return True, {"v": "new"}

    hit, data = cache.get_or_rebuild(key, 60, rebuild)
    assert hit and data == {"v": "pre"}, f"命中缓存却返回回源值：{data}"
    assert counter["n"] == 0, f"缓存命中仍调用 rebuild {counter['n']} 次"
    print("  ✅ 缓存命中快路径不抢锁不回源")
    cache._redis.delete(key)


def test_jitter():
    """TTL 抖动落在 [60-5, 60+5]（防雪崩）"""
    key = "ecom:test:jitter"
    cache._redis.delete(key)
    cache.set_json(key, {"v": 1}, 60)
    ttl = cache._redis.ttl(key)
    assert ttl is not None and 55 <= ttl <= 65, f"TTL 抖动越界：{ttl}（应 60±5）"
    print(f"  ✅ TTL 抖动 {ttl}s（60±5 防雪崩，避免同类 key 同一秒集体过期）")
    cache._redis.delete(key)


def main():
    print("📍 [1/3] 击穿：并发回源收敛")
    test_stampede()
    print("📍 [2/3] 快路径：命中不回源")
    test_hit_fastpath()
    print("📍 [3/3] 雪崩：TTL 抖动")
    test_jitter()
    print("\n✅ 击穿防护 + 雪崩抖动对抗测试全部通过")


if __name__ == "__main__":
    main()
