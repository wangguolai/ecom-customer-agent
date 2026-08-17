# -*- coding: utf-8 -*-
"""Qdrant 向量存储 —— 本地文件模式，3 个 Collection

Collection:
  scene_experiences  — 历史场景→提示词语义检索
  knowledge_base     — references/ 参考文档 RAG
  failure_patterns   — 失败模式语义匹配

用法：
  store = QdrantStore("guolaimokaStudio/qdrant_data")
  store.ensure_collections()
  store.upsert_scenes(payloads, vectors)
  results = store.search_scenes(query_vec, limit=5)
"""

import sys
import os
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


COLLECTIONS = {
    "product_knowledge": {
        "description": "宠物商品知识库 RAG",
        "payload_schema": ["chunk_id", "text", "title", "source_file", "category", "chunk_index"],
    },
}


@dataclass
class SearchHit:
    """单条搜索结果"""
    score: float
    payload: dict


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
        """按 source_file 删除指定文件的所有分块向量"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        self._client.delete(
            collection_name=collection,
            points_selector=Filter(
                must=[FieldCondition(key="source_file", match=MatchValue(value=source_file))]
            ),
        )

    def get_source_mtimes(self, collection: str) -> dict:
        """获取 collection 中每个 source_file 的 file_mtime 映射

        Returns:
            {source_file: file_mtime}
        """
        if not self._client.collection_exists(collection):
            return {}
        mtimes = {}
        offset = None
        while True:
            points, next_offset = self._client.scroll(
                collection_name=collection, limit=100, offset=offset,
                with_payload=["source_file", "file_mtime"],
            )
            for p in points:
                if p.payload and "source_file" in p.payload:
                    src = p.payload["source_file"]
                    if src not in mtimes:
                        mtimes[src] = p.payload.get("file_mtime", 0)
            if next_offset is None:
                break
            offset = next_offset
        return mtimes

    def drop_collection(self, name: str):
        """删除 Collection（调试用）"""
        self._client.delete_collection(name)

    # ── 分层检索 ────────────────────────────────────────────

    def search_with_tiers(
        self,
        collection: str,
        query_vector: list[float],
        limit: int = 5,
        series_name: str = None,
        category: str = None,
    ) -> dict:
        """分层检索——高置信度 (>0.6) + 低置信度 (0.5-0.6)

        Returns:
            {"high": [...], "low": [...]}
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        conditions = []
        if series_name:
            conditions.append(FieldCondition(key="series_name", match=MatchValue(value=series_name)))
        if category:
            conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
        if conditions:
            query_filter = Filter(must=conditions)

        # 一次查询拿全部，再分层
        results = self._client.query_points(
            collection_name=collection,
            query=query_vector,
            query_filter=query_filter,
            limit=limit * 2,  # 多拿一倍，确保每层都有
            score_threshold=0.5,
        )

        high = [SearchHit(score=r.score, payload=r.payload) for r in results.points if r.score > 0.6]
        low = [SearchHit(score=r.score, payload=r.payload) for r in results.points if 0.5 <= r.score <= 0.6]

        return {"high": high[:limit], "low": low[:limit]}

    # ── 场景经验 CRUD ────────────────────────────────────────

    def upsert_scenes(self, payloads: list[dict], vectors: list[list[float]]):
        """批量写入场景经验

        Args:
            payloads: 每条含 scene_id, description, characters, felt_intent 等
            vectors: 对应向量列表，长度需与 payloads 一致
        """
        from qdrant_client.models import PointStruct

        points = []
        for i, (p, v) in enumerate(zip(payloads, vectors)):
            point_id = str(uuid.uuid4())
            points.append(PointStruct(id=point_id, vector=v, payload=p))

        self._client.upsert(collection_name="scene_experiences", points=points)

    def search_scenes(
        self,
        query_vector: list[float],
        series_name: str = None,
        limit: int = 5,
        score_threshold: float = 0.5,
    ) -> list[SearchHit]:
        """语义搜索场景经验

        score 含义（BGE normalize 后）：
          > 0.6  语义相关，直接使用
          0.5-0.6 边缘相关，低置信度
          < 0.5  不相关，已丢弃（score_threshold=0.5）
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if series_name:
            query_filter = Filter(
                must=[FieldCondition(key="series_name", match=MatchValue(value=series_name))]
            )

        results = self._client.query_points(
            collection_name="scene_experiences",
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
        )
        return [SearchHit(score=r.score, payload=r.payload) for r in results.points]

    # ── 知识库 CRUD ──────────────────────────────────────────

    def upsert_knowledge(self, payloads: list[dict], vectors: list[list[float]]):
        """批量写入知识库块"""
        from qdrant_client.models import PointStruct

        points = []
        for i, (p, v) in enumerate(zip(payloads, vectors)):
            point_id = str(uuid.uuid4())
            points.append(PointStruct(id=point_id, vector=v, payload=p))

        self._client.upsert(collection_name="product_knowledge", points=points)

    def search_knowledge(
        self,
        query_vector: list[float],
        category: str = None,
        limit: int = 5,
        score_threshold: float = 0.5,
    ) -> list[SearchHit]:
        """语义搜索知识库"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if category:
            query_filter = Filter(
                must=[FieldCondition(key="category", match=MatchValue(value=category))]
            )

        results = self._client.query_points(
            collection_name="product_knowledge",
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
        )
        return [SearchHit(score=r.score, payload=r.payload) for r in results.points]

    # ── 失败模式 CRUD ────────────────────────────────────────

    def upsert_failures(self, payloads: list[dict], vectors: list[list[float]]):
        """批量写入失败模式"""
        from qdrant_client.models import PointStruct

        points = []
        for i, (p, v) in enumerate(zip(payloads, vectors)):
            point_id = str(uuid.uuid4())
            points.append(PointStruct(id=point_id, vector=v, payload=p))

        self._client.upsert(collection_name="failure_patterns", points=points)

    def search_failures(
        self,
        query_vector: list[float],
        series_name: str = None,
        limit: int = 5,
        score_threshold: float = 0.5,
    ) -> list[SearchHit]:
        """语义搜索失败模式"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if series_name:
            query_filter = Filter(
                must=[FieldCondition(key="series_name", match=MatchValue(value=series_name))]
            )

        results = self._client.query_points(
            collection_name="failure_patterns",
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            score_threshold=score_threshold,
        )
        return [SearchHit(score=r.score, payload=r.payload) for r in results.points]


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from src.memory.embedding import get_embedding_model, embed_texts

    print("=" * 50)
    print("Qdrant 向量存储自测")
    print("=" * 50)

    # 1. 加载模型 + 初始化 Qdrant
    model = get_embedding_model()
    store = QdrantStore("guolaimokaStudio/qdrant_data")
    store.ensure_collections(vector_size=512)

    # 2. 写入测试数据
    texts = [
        "橘猫在窗台上晒太阳，眯着眼睛打盹",
        "美短追着激光笔的光点满屋子跑",
    ]
    vecs = embed_texts(texts)

    store.upsert_scenes(
        payloads=[
            {"scene_id": 1, "description": texts[0], "characters": ["过来"],
             "felt_intent": "满足放松", "series_name": "work_diary"},
            {"scene_id": 2, "description": texts[1], "characters": ["摩卡"],
             "felt_intent": "兴奋好奇", "series_name": "work_diary"},
        ],
        vectors=vecs,
    )
    print(f"  ✅ 写入 {len(texts)} 条测试数据")

    # 3. 语义搜索
    query = "猫咪在阳光下睡觉"
    query_vec = model.encode(query, normalize_embeddings=True).tolist()
    results = store.search_scenes(query_vec, series_name="work_diary", limit=2)

    print(f"\n  🔍 查询: \"{query}\"")
    for i, r in enumerate(results):
        desc = r.payload.get("description", "")[:60]
        chars = ", ".join(r.payload.get("characters", []))
        print(f"     {i+1}. (score={r.score:.3f}) [{chars}] {desc}")

    # 4. Collection 状态
    print(f"\n  📊 Collections:")
    for name in COLLECTIONS:
        info = store.collection_info(name)
        print(f"     {name}: {info['points_count']} points")

    print(f"\n  ✅ Qdrant 向量存储正常")
    print(f"  数据目录: {store._path.resolve()}")
