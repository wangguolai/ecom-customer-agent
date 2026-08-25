# -*- coding: utf-8 -*-
"""商品知识库索引——读 products.md 分块，向量化后写入 Qdrant"""

import sys
import os

# 把项目根目录加进 sys.path，让 from src.infra.xxx import 能找到
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_knowledge_base():
    """读 products.md → 分块 → embedding → 写入 Qdrant"""
    from src.infra.embedding import embed_texts
    from src.infra.vector_store import QdrantStore
    from src.domain.products import parse_products

    # 1. 解析（唯一 parser：products.md → Product 实体）
    products = parse_products()
    chunks = [p.raw_chunk for p in products]
    print(f"📦 分块：{len(chunks)} 块")

    # 2. 向量化
    vectors = embed_texts(chunks)

    # 3. 造 payload（每个商品块 → dict，含 title/category 供过滤检索）
    payloads = []
    for i, p in enumerate(products):
        payloads.append({
            "chunk_id": f"products:{i}",
            "text": p.raw_chunk,
            "title": p.title,
            "category": p.category,
            "source_file": "products.md",
            "chunk_index": i,
            "product_id": p.id,  # 稳定实体 ID，打通「向量检索 → MySQL 查价」链路
        })

    # 4. 写入 Qdrant（先删旧数据再写入，保证幂等、清残留）
    store = QdrantStore()
    store.ensure_collections()
    store.delete_by_source("product_knowledge", "products.md")
    store.upsert_knowledge(payloads, vectors)

    info = store.collection_info("product_knowledge")
    print(f"✅ product_knowledge: {info['points_count']} points")


if __name__ == "__main__":
    build_knowledge_base()
