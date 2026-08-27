# -*- coding: utf-8 -*-
"""统一 refresh 入口：从源数据重建所有「持久化派生」（向量库 + SQLite）

改源数据（products.md / policies.md / seed.py）后跑 `python -m src.refresh`，一次性重建：
- SQLite（seed 表）：backend._init_db() 的 DROP 重建
- 向量库（Qdrant）：index_products.build_knowledge_base()（商品 + 政策两个知识域）

⚠️ 部署约束：先 `python -m src.refresh` 再启动服务进程——单例 BM25 索引在进程启动时
从 Qdrant scroll 全量建，政策必须在启动前已入库，否则 kb_type 过滤会使商品/政策检索整体失效。

内存派生（jieba 词典 / 意图触发词 / 评测白名单）不需要 refresh——运行时从源生成，重启即同步。
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _validate_product_ids():
    """校验商品 ID 一致性（防人工加漏/加重 ID 导致 products 表主键冲突、md↔seed 漂移）。

    - ID 非空 + 集合内唯一（防加漏/加重）
    - md 的 id+title ↔ seed 的 product_id+name 双向一致（防两处漂移）
    """
    from collections import Counter
    from src.domain.products import parse_products
    from src.backend.seed import _SEED_PRODUCTS

    products = parse_products()
    md_ids = [p.id for p in products]
    # 先判缺 ID（空串），再判重复——否则 ≥2 个缺 ID 会被误报成「重复 ID {''}」
    empty = [p.title for p in products if not p.id]
    if empty:
        raise ValueError(f"products.md 有商品缺 ID：{empty}")
    dupes = {pid for pid, n in Counter(md_ids).items() if n > 1}
    if dupes:
        raise ValueError(f"products.md 有重复 ID：{dupes}")

    md = {p.id: p.title for p in products}
    seed = {sp[0]: sp[1] for sp in _SEED_PRODUCTS}
    md_only = set(md) - set(seed)
    seed_only = set(seed) - set(md)
    if md_only or seed_only:
        raise ValueError(f"md ↔ seed 不一致：md 有 seed 无 {md_only}，seed 有 md 无 {seed_only}")
    drift = {pid: (md[pid], seed[pid]) for pid in md if md[pid] != seed[pid]}
    if drift:
        raise ValueError(f"md title ↔ seed name 漂移：{drift}")


def refresh():
    """重建所有持久化派生数据"""
    print("📍 [0/3] 校验商品 ID 一致性（md ↔ seed 双向）...")
    _validate_product_ids()
    print("    ID 校验通过。")

    print("📍 [1/3] MySQL — 重建 seed 表（DROP + 灌种子）...")
    from src.backend.main import _init_db
    _init_db()
    print("    MySQL 已重建。")

    print("📍 [2/3] Redis — 清空缓存副本（ecom:* 前缀删）...")
    from src.backend import cache
    cache.flush()
    print("    Redis 缓存已清空。")

    print("📍 [3/3] 向量库 — 重建知识库（商品 + 政策）...")
    from src.index_products import build_knowledge_base
    build_knowledge_base()

    print("✅ refresh 完成：MySQL + Redis + 向量库 已从源数据重建。")


if __name__ == "__main__":
    refresh()
