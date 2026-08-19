# -*- coding: utf-8 -*-
"""测 rerank 真实耗时：本地 CPU 跑一次 CrossEncoder 打分要多久

用法：python tests/bench_rerank.py
"""

import sys
import os
import time

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.infra.reranker import get_reranker


def bench():
    model = get_reranker()
    query = "幼犬吃什么粮"
    candidates = [
        "幼犬成长粮（贝乐牌），鸡肉糙米配方，2-12 月龄幼犬适用",
        "成犬均衡粮（贝乐牌），牛肉燕麦配方，1 岁以上成犬",
        "全阶段鸡肉粮（优宠牌），鸡肉大米配方，全阶段犬",
        "幼猫奶糕粮（喵趣牌），鸡肉羊奶粉，幼猫离乳期",
        "成猫美毛粮（喵趣牌），三文鱼鸡胸肉，改善毛发光泽",
    ]
    pairs = [(query, c) for c in candidates]

    # 预热一次（甩掉首次调用的模型加载/懒加载开销）
    model.predict(pairs)

    N = 10
    times = []
    for _ in range(N):
        t0 = time.time()
        model.predict(pairs)
        times.append(time.time() - t0)

    avg = sum(times) / N
    print(f"候选数：{len(candidates)}")
    print(f"测 {N} 次取平均：单次 {avg*1000:.1f} ms")
    print(f"最快 {min(times)*1000:.1f} ms / 最慢 {max(times)*1000:.1f} ms")


if __name__ == "__main__":
    bench()
