# -*- coding: utf-8 -*-
"""统一 refresh 入口：从源数据重建所有「持久化派生」（向量库 + SQLite）

改源数据（products.md / seed.py）后跑 `python -m src.refresh`，一次性重建：
- SQLite（seed 表）：backend._init_db() 的 DROP 重建
- 向量库（Qdrant）：index_products.build_knowledge_base()

内存派生（jieba 词典 / 意图触发词 / 评测白名单）不需要 refresh——运行时从源生成，重启即同步。
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def refresh():
    """重建所有持久化派生数据"""
    print("📍 [1/3] MySQL — 重建 seed 表（DROP + 灌种子）...")
    from src.backend.main import _init_db
    _init_db()
    print("    MySQL 已重建。")

    print("📍 [2/3] Redis — 清空缓存副本（FLUSHDB）...")
    from src.backend import cache
    cache.flush()
    print("    Redis 缓存已清空。")

    print("📍 [3/3] 向量库 — 重建 product_knowledge（embedding + 写 Qdrant）...")
    from src.index_products import build_knowledge_base
    build_knowledge_base()

    print("✅ refresh 完成：MySQL + Redis + 向量库 已从源数据重建。")


if __name__ == "__main__":
    refresh()
