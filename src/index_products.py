# -*- coding: utf-8 -*-
"""商品知识库索引——读 products.md 分块，向量化后写入 Qdrant"""

import sys
import os
import re
from pathlib import Path

# 把项目根目录加进 sys.path，让 from src.infra.xxx import 能找到
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def chunk_markdown(text: str) -> list[str]:
    """按 ## 标题分块，过滤掉文件一级标题等非商品块"""
    sections = re.split(r"\n(?=## )", text)
    # 只保留以 ## 开头的商品块（第一个块是 "# 宠物商品知识库" 文件标题，过滤掉）
    return [s for s in sections if s.startswith("##")]


def build_knowledge_base():
    """读 products.md → 分块 → embedding → 写入 Qdrant"""
    from src.infra.embedding import embed_texts
    from src.infra.vector_store import QdrantStore

    # 1. 读文件
    text = Path("data/products.md").read_text(encoding="utf-8")

    # 2. 分块（list[str]，每个商品一块）
    chunks = chunk_markdown(text)
    print(f"📦 分块：{len(chunks)} 块")

    # 3. 向量化
    vectors = embed_texts(chunks)

    # 4. 造 payload（每个商品块 → dict，含 category 供过滤检索）
    payloads = []
    for i, chunk in enumerate(chunks):
        m = re.match(r"## (.+)", chunk)
        title = m.group(1).strip() if m else "products"
        cm = re.search(r"类别：(\S+)", chunk)
        category = cm.group(1).strip() if cm else ""
        payloads.append({
            "chunk_id": f"products:{i}",
            "text": chunk,
            "title": title,
            "category": category,
            "source_file": "products.md",
            "chunk_index": i,
        })

    # 5. 写入 Qdrant（先删旧数据再写入，保证幂等、清残留）
    store = QdrantStore()
    store.ensure_collections()
    store.delete_by_source("product_knowledge", "products.md")
    store.upsert_knowledge(payloads, vectors)

    info = store.collection_info("product_knowledge")
    print(f"✅ product_knowledge: {info['points_count']} points")


if __name__ == "__main__":
    build_knowledge_base()
