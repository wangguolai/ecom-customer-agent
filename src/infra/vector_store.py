# -*- coding: utf-8 -*-
"""Qdrant 向量存储 —— 本地文件模式，电商客服商品知识库 RAG

Collection:
  product_knowledge  — 商品知识库（products.md 分块后的商品语义检索）

用法：
  store = QdrantStore()          # 默认 data/qdrant/
  store.ensure_collections()
  store.upsert_knowledge(payloads, vectors)
  results = store.search_knowledge(query_vec, limit=5)
"""

import sys
import os
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


COLLECTIONS = {
    "product_knowledge": {
        "description": "宠物商品知识库 RAG",
        "payload_schema": ["chunk_id", "text", "title", "category", "source_file", "chunk_index"],
    },
}


@dataclass
class SearchHit:
    """单条搜索结果"""
    score: float
    payload: dict


# 稳定 point id 的命名空间（固定值，保证 chunk_id → UUID 映射稳定可复现）
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "ecom-customer-agent:product_knowledge")


def _point_id(chunk_id: str) -> str:
    """由 chunk_id 生成确定性 UUID —— 同一条内容永远得到同一个 id，upsert 时覆盖旧向量（幂等）"""
    return str(uuid.uuid5(_ID_NAMESPACE, chunk_id))


class QdrantStore:
    """Qdrant 向量存储封装"""

    def __init__(self, path: Optional[str] = None):
        from qdrant_client import QdrantClient
        if path is None:
            self._path = Path(_project_root) / "data" / "qdrant"
        else:
            p = Path(path)
            if not p.is_absolute():
                p = Path(_project_root) / p
            self._path = p
        self._path.mkdir(parents=True, exist_ok=True)
        self._client = QdrantClient(path=str(self._path))

    @property
    def client(self):
        return self._client

    # ── Collection 管理 ──────────────────────────────────────

    def ensure_collections(self, vector_size: int = 512):
        """幂等创建所有 Collection"""
        from qdrant_client.models import Distance, VectorParams

        for name, meta in COLLECTIONS.items():
            if self._client.collection_exists(name):
                continue
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            print(f"  📦 Collection 已创建: {name} ({meta['description']})")

    def collection_info(self, name: str) -> dict:
        """获取 Collection 信息"""
        info = self._client.get_collection(name)
        return {
            "name": name,
            "points_count": info.points_count,
        }

    def delete_by_source(self, collection: str, source_file: str):
        """按 source_file 删除指定文件的所有分块向量（重建索引前清空旧数据用）"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        self._client.delete(
            collection_name=collection,
            points_selector=Filter(
                must=[FieldCondition(key="source_file", match=MatchValue(value=source_file))]
            ),
        )

    def drop_collection(self, name: str):
        """删除 Collection（调试用）"""
        self._client.delete_collection(name)

    # ── 知识库 CRUD ──────────────────────────────────────────

    def upsert_knowledge(self, payloads: list[dict], vectors: list[list[float]]):
        """批量写入商品知识库块（用 payload 的 chunk_id 作 point id，同 id 覆盖，保证幂等）"""
        from qdrant_client.models import PointStruct

        points = []
        for i, (p, v) in enumerate(zip(payloads, vectors)):
            # 稳定 id：由 chunk_id 生成确定性 UUID，重复写入覆盖旧向量（幂等）
            chunk_id = p.get("chunk_id")
            point_id = _point_id(chunk_id) if chunk_id else str(uuid.uuid4())
            points.append(PointStruct(id=point_id, vector=v, payload=p))

        self._client.upsert(collection_name="product_knowledge", points=points)

    def search_knowledge(
        self,
        query_vector: list[float],
        category: str = None,
        limit: int = 5,
        score_threshold: Optional[float] = 0.5,
    ) -> list[SearchHit]:
        """语义搜索商品知识库，可按 category 过滤"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if category:
            query_filter = Filter(
                must=[FieldCondition(key="category", match=MatchValue(value=category))]
            )

        # score_threshold=None 时不过滤（混合检索需要全量返回，保证排名对称）
        kwargs = {}
        if score_threshold is not None:
            kwargs["score_threshold"] = score_threshold
        results = self._client.query_points(
            collection_name="product_knowledge",
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            **kwargs,
        )
        return [SearchHit(score=r.score, payload=r.payload) for r in results.points]

    def scroll_all(self, collection: str = "product_knowledge") -> list:
        """scroll 出所有 chunk 的 (chunk_id, text)，用于 BM25 建索引"""
        results = []
        offset = None
        while True:
            points, next_offset = self._client.scroll(
                collection_name=collection, limit=100, offset=offset,
                with_payload=["chunk_id", "text"],
            )
            for p in points:
                if p.payload:
                    cid = p.payload.get("chunk_id")
                    text = p.payload.get("text")
                    if cid and text:  # 过滤缺失字段，避免 None 混入下游分词
                        results.append((cid, text))
            if next_offset is None:
                break
            offset = next_offset
        return results


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from src.infra.embedding import get_embedding_model, embed_texts

    print("=" * 50)
    print("Qdrant 向量存储自测")
    print("=" * 50)

    # 1. 加载模型 + 初始化 Qdrant
    model = get_embedding_model()
    store = QdrantStore()
    store.ensure_collections(vector_size=512)

    # 2. 写入测试数据（source_file 用 test.md，与正式 products.md 隔离）
    texts = [
        "幼犬成长粮（贝乐牌），鸡肉糙米配方，2-12 月龄幼犬适用",
        "猫薄荷玩具球，内置猫薄荷，吸引猫咪玩耍",
    ]
    vecs = embed_texts(texts)

    store.upsert_knowledge(
        payloads=[
            {"chunk_id": "test:0", "text": texts[0], "title": "幼犬成长粮",
             "category": "狗粮", "source_file": "test.md", "chunk_index": 0},
            {"chunk_id": "test:1", "text": texts[1], "title": "猫薄荷玩具球",
             "category": "玩具", "source_file": "test.md", "chunk_index": 1},
        ],
        vectors=vecs,
    )
    print(f"  ✅ 写入 {len(texts)} 条测试数据")

    # 3. 语义搜索
    query = "小狗吃什么粮"
    query_vec = model.encode(query, normalize_embeddings=True).tolist()
    results = store.search_knowledge(query_vec, limit=2)

    print(f'\n  🔍 查询: "{query}"')
    for i, r in enumerate(results):
        title = r.payload.get("title", "")
        category = r.payload.get("category", "")
        print(f"     {i+1}. (score={r.score:.3f}) [{category}] {title}")

    # 4. 清理测试数据
    store.delete_by_source("product_knowledge", "test.md")
    print("\n  🧹 已清理测试数据")

    # 5. Collection 状态
    print("\n  📊 Collections:")
    for name in COLLECTIONS:
        info = store.collection_info(name)
        print(f"     {name}: {info['points_count']} points")

    print("\n  ✅ Qdrant 向量存储正常")
    print(f"  数据目录: {store._path.resolve()}")
