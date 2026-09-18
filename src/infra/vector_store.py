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
import atexit
import uuid
import threading
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
        "payload_schema": ["chunk_id", "text", "title", "category", "source_file", "chunk_index", "product_id", "image", "kb_type"],
    },
    # 用户画像语义检索。**独立 collection 不是洁癖**：
    #   1. refresh / build_knowledge_base 会按源文件删 product_knowledge 的点 + 集合对账
    #      （index_products.py:81-94），画像混进去会被算成「残留 chunk」；
    #   2. 任何将来「重建整个 collection」的改动都会静默清空所有用户画像；
    #   3. 独立 collection 由 ensure_collections 自动创建，成本为零。
    # 它是**派生层**：真源在 MySQL user_memories，丢了可从真源重建（见 refresh_memory.py）。
    "user_memory": {
        "description": "用户画像语义检索（派生层，真源在 MySQL user_memories）",
        "payload_schema": ["memory_id", "user_id", "content", "category", "confidence"],
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
        atexit.register(self._safe_close)

    @property
    def client(self):
        return self._client

    def close(self):
        """显式关闭（释放文件锁）；正常退出由 atexit 兜底，无需手动调"""
        self._safe_close()

    def _safe_close(self):
        """退出前主动 close，避免 QdrantClient.__del__ 在解释器关闭时 import msvcrt 失败刷 ModuleNotFoundError。

        根因：QdrantClient.__del__（库代码）无异常保护调 close()，close 链里
        portalocker.unlock → import msvcrt（Windows 文件锁），而解释器关闭时
        msvcrt 已被清成 None，抛 ModuleNotFoundError；库的 close 只 except TypeError，
        没接住 ImportError。atexit 回调在模块清空前执行（import 还安全），
        主动 close 后 _flock_file.closed=True，退出 GC 时 __del__ 的 close 短路，不再 import。
        """
        client = getattr(self, "_client", None)
        if client is None:
            return
        try:
            client.close()
        except Exception:
            pass
        self._client = None  # 断开引用，让退出 GC 时不再触发 __del__ 的清理

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

    def delete_by_chunk_ids(self, collection: str, chunk_ids: list[str]):
        """按 chunk_id 列表删除对应 point（增量更新的「删除/下架」分支用）。

        只删指定的几个 chunk，不动其他——和 delete_by_source（删整个源文件）区分：
        这是增量语义「下架商品 = 删掉它自己的那条向量」的落地。
        """
        from qdrant_client.models import PointIdsList
        point_ids = [_point_id(cid) for cid in chunk_ids if cid]
        if point_ids:
            self._client.delete(
                collection_name=collection,
                points_selector=PointIdsList(points=point_ids),
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
        kb_type: str = None,
        limit: int = 5,
        score_threshold: Optional[float] = 0.5,
    ) -> list[SearchHit]:
        """语义搜索知识库，可按 category / kb_type 过滤（都检索前 filter，多知识域隔离）"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        # 两个过滤维度都进 must 列表；都为空时 query_filter=None（不过滤）
        must = []
        if category:
            must.append(FieldCondition(key="category", match=MatchValue(value=category)))
        if kb_type:
            must.append(FieldCondition(key="kb_type", match=MatchValue(value=kb_type)))
        query_filter = Filter(must=must) if must else None

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
        """scroll 出所有 chunk 的 (chunk_id, text, title, category, product_id, image, kb_type)，用于 BM25 建索引"""
        results = []
        offset = None
        while True:
            points, next_offset = self._client.scroll(
                collection_name=collection, limit=100, offset=offset,
                with_payload=["chunk_id", "text", "title", "category", "product_id", "image", "kb_type"],
            )
            for p in points:
                if p.payload:
                    cid = p.payload.get("chunk_id")
                    text = p.payload.get("text")
                    if cid and text:  # 过滤缺失字段，避免 None 混入下游分词
                        results.append((cid, text, p.payload.get("title", ""), p.payload.get("category", ""), p.payload.get("product_id", ""), p.payload.get("image", ""), p.payload.get("kb_type", "")))
            if next_offset is None:
                break
            offset = next_offset
        return results


    # ── 用户画像（派生层，真源在 MySQL） ──────────────────────

    def upsert_memory(self, payloads: list[dict], vectors: list[list[float]]):
        """批量写入画像向量。point id 直接用 memory_id——
        它由 `uuid5(user_id + ":" + 归一化content)` 生成（见 src/memory.py），
        天然确定性幂等：同一条事实重复抽取 → 同一个 id → upsert 覆盖，不产生重复点。
        """
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(id=p["memory_id"], vector=v, payload=p)
            for p, v in zip(payloads, vectors)
        ]
        if points:
            self._client.upsert(collection_name="user_memory", points=points)

    def search_memory(
        self,
        query_vector: list[float],
        user_id: str,
        limit: int = 5,
        score_threshold: Optional[float] = None,
    ) -> list[SearchHit]:
        """按语义检索该用户的画像。

        **user_id 强制过滤**：这是「跨用户串号」（MINJA 那类投毒/越权读 PII）的落地点。
        user_id 为空直接返回空——绝不做「不带过滤的全库检索」兜底（那等于把所有人的
        画像混在一起喂给当前用户）。
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        if not user_id:
            return []

        kwargs = {}
        if score_threshold is not None:
            kwargs["score_threshold"] = score_threshold
        results = self._client.query_points(
            collection_name="user_memory",
            query=query_vector,
            query_filter=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
            limit=limit,
            **kwargs,
        )
        return [SearchHit(score=r.score, payload=r.payload) for r in results.points]

    def delete_by_memory_ids(self, memory_ids: list[str]):
        """按 memory_id 删除画像点（缓冲淘汰 / 用户删除单条用）"""
        from qdrant_client.models import PointIdsList
        ids = [m for m in memory_ids if m]
        if ids:
            self._client.delete(
                collection_name="user_memory",
                points_selector=PointIdsList(points=ids),
            )

    def count_memory_points(self, user_id: str = None) -> int:
        """数画像点数（重建时的真源↔派生对账用）。带 user_id 则只数该用户。"""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = None
        if user_id:
            query_filter = Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            )
        result = self._client.count(
            collection_name="user_memory", count_filter=query_filter, exact=True
        )
        return result.count


# ═══════════════════════════════════════════════════════════════
# 进程级单例
# ═══════════════════════════════════════════════════════════════

_store_singleton: Optional["QdrantStore"] = None
_store_lock = threading.Lock()


def get_qdrant_store() -> "QdrantStore":
    """进程级 Qdrant store 单例 —— **所有在线路径必须走这里**。

    为什么必须单例：Qdrant 本地文件模式用 portalocker 加文件锁，同一进程内新建第二个
    QdrantClient 会抛 AlreadyLocked（hybrid_retriever.py 的注释已记录过这个坑）。
    而画像（src/memory.py）和商品检索（src/infra/hybrid_retriever.py）是两个模块，
    各自 new 一个 store 就会撞锁——且是在 Web 链路上「先搜商品再存画像」时才炸，
    属于最难排查的时序型故障。

    双检锁：多线程（asyncio.to_thread 的 worker）并发首次调用时只建一个实例。
    这是项目第二次踩「懒加载单例无锁」——第一次是 asyncio 并发初始化 Qdrant 冲突。

    例外：离线重建入口（refresh_memory.py / index_products.py）允许自建实例 +
    显式 close()，因为它们要在服务停止时独占文件。
    """
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:  # 双检：拿到锁后再确认一次
                _store_singleton = QdrantStore()
                # 幂等建 collection（已存在直接 continue，成本为零）。
                # **必须在这里兜底**：全仓 grep 过，ensure_collections 只在 index_products /
                # refresh_memory 这些**离线建库入口**里被调用，没有任何在线路径调它。
                # 不建的话，全新 clone 且没跑过建库时，user_memory 不存在 →
                # 画像写入抛「collection 不存在」被降级吞掉、检索永远返回 [] →
                # 用户视角是「记忆功能不工作」却**没有任何显式报错**（最坏的一类失败）。
                _store_singleton.ensure_collections()
    return _store_singleton


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
        "幼犬成长粮（皇家牌），鸡肉糙米配方，2-12 月龄幼犬适用",
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
