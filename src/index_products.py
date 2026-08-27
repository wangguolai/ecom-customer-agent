# -*- coding: utf-8 -*-
"""知识库索引——读 products.md + policies.md 分块，向量化后写入 Qdrant（商品 + 政策两个知识域）"""

import sys
import os

# 把项目根目录加进 sys.path，让 from src.infra.xxx import 能找到
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_knowledge_base():
    """读 products.md + policies.md → 分块 → embedding → 写入 Qdrant"""
    from src.infra.embedding import embed_texts
    from src.infra.vector_store import QdrantStore
    from src.domain.products import parse_products
    from src.domain.policies import parse_policies

    # 1. 解析（唯一 parser：products.md → Product 实体；policies.md → Policy 实体）
    products = parse_products()
    product_chunks = [p.raw_chunk for p in products]
    print(f"📦 商品分块：{len(product_chunks)} 块")

    policies = parse_policies()
    policy_chunks = [p.raw_chunk for p in policies]
    print(f"📦 政策分块：{len(policy_chunks)} 块")

    chunks = product_chunks + policy_chunks

    # 2. 向量化（同构：BGE-small-zh 512 维 COSINE，逻辑多库共享 collection）
    vectors = embed_texts(chunks)

    # 3. 造 payload（商品块含 category/product_id 供过滤检索；政策块 category/product_id 置空）
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
            "kb_type": "product",
        })
    for i, p in enumerate(policies):
        payloads.append({
            "chunk_id": f"policies:{i}",
            "text": p.raw_chunk,
            "title": p.title,
            "category": "",
            "source_file": "policies.md",
            "chunk_index": i,
            "product_id": "",
            "kb_type": "policy",
        })

    # 4. 写入 Qdrant（分别按两个源删旧数据再写入，幂等重建、清残留）
    store = QdrantStore()
    store.ensure_collections()
    store.delete_by_source("product_knowledge", "products.md")
    store.delete_by_source("product_knowledge", "policies.md")
    store.upsert_knowledge(payloads, vectors)

    info = store.collection_info("product_knowledge")
    print(f"✅ product_knowledge: {info['points_count']} points（商品 {len(products)} + 政策 {len(policies)}）")


if __name__ == "__main__":
    build_knowledge_base()
