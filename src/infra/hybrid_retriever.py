# -*- coding: utf-8 -*-
"""混合检索 —— BM25（关键词）+ 向量（语义）+ RRF 融合

解决纯向量对精确词（品牌名/特征词）不敏感的坑。
RRF 公式：score(d) = 1/(k + rank_bm25) + 1/(k + rank_vec)，k=60，rank 0-based。
"""

import sys
import os

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import jieba
from rank_bm25 import BM25Okapi

from src.infra.embedding import get_embedding_model
from src.infra.vector_store import QdrantStore

RRF_K = 60
DEFAULT_TOP_K = 3

# 品牌名等专有名词加进 jieba 词典，避免被切成单字（如「贝乐牌」→「贝/乐/牌」）
_CUSTOM_WORDS = ["贝乐牌", "优宠牌", "喵趣牌"]
for _w in _CUSTOM_WORDS:
    jieba.add_word(_w)


class HybridRetriever:
    """BM25 + 向量 + RRF 混合检索"""

    def __init__(self):
        self._store = QdrantStore()
        self._model = get_embedding_model()
        self._build_bm25()

    def _build_bm25(self):
        """从 Qdrant scroll 出所有 chunk 建 BM25 索引，保证和向量库数据对齐"""
        chunks = self._store.scroll_all()  # [(chunk_id, text)]
        self._chunk_ids = [cid for cid, _ in chunks]
        self._chunk_texts = [text for _, text in chunks]
        if not self._chunk_texts:
            self._bm25 = None  # 知识库为空，跳过 BM25（search 里退化为纯向量）
            return
        # jieba 分词建 BM25 索引（建索引和查询用同一 jieba 配置）
        self._tokenized = [jieba.lcut(t) for t in self._chunk_texts]
        self._bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> list:
        """混合检索，返回 [(chunk_id, rrf_score, text)]"""
        q_vec = self._model.encode(query, normalize_embeddings=True).tolist()

        # 1. 向量检索（score_threshold=None 全量返回，保证排名对称）
        hits = self._store.search_knowledge(q_vec, limit=20, score_threshold=None)
        vec_rank = {h.payload.get("chunk_id"): rank
                    for rank, h in enumerate(hits) if h.payload.get("chunk_id")}

        # 2. BM25 检索（知识库空 / token 空则跳过，退化为纯向量）
        tokens = jieba.lcut(query)
        bm25_rank = {}
        if self._bm25 is not None and tokens:
            scores = self._bm25.get_scores(tokens)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            for rank, idx in enumerate(order):
                if scores[idx] > 0:  # 0 分文档不入排名，让 fallback_rank 承担「不在结果里」语义
                    bm25_rank[self._chunk_ids[idx]] = rank

        # 3. RRF 融合（取两个检索器结果的并集）
        all_cids = set(bm25_rank.keys()) | set(vec_rank.keys())
        fallback_rank = len(self._chunk_ids)  # 没进检索结果的兜底排名
        fused = []
        for cid in all_cids:
            r_bm = bm25_rank.get(cid, fallback_rank)
            r_vec = vec_rank.get(cid, fallback_rank)
            score = 1.0 / (RRF_K + r_bm) + 1.0 / (RRF_K + r_vec)
            fused.append((cid, score))
        fused.sort(key=lambda x: x[1], reverse=True)

        # 4. 返回 top_k（附 text）
        text_map = dict(zip(self._chunk_ids, self._chunk_texts))
        result = []
        for cid, score in fused[:top_k]:
            result.append((cid, score, text_map.get(cid, "")))
        return result


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    retriever = HybridRetriever()
    for q in ["贝乐牌有哪些粮", "肠胃敏感的粮", "幼犬吃什么"]:
        print("=" * 50)
        print(f"Q: {q}")
        for cid, score, text in retriever.search(q):
            title = text.split("\n")[0].strip("# ").strip() if text else ""
            print(f"  ({score:.4f}) {title}")
