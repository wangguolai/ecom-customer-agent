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
from src.infra import reranker

RRF_K = 60
DEFAULT_TOP_K = 3
RERANK_CANDIDATE_N = 20  # RRF 融合后先取这么多候选，交给 Rerank 精排
VEC_SCORE_LOW = 0.4  # 召回层双低拒答：向量 top1 分数 < 此值且 BM25 无召回 → 判超知识库，拒答

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
        chunks = self._store.scroll_all()  # [(chunk_id, text, category)]
        self._chunk_ids = [cid for cid, _, _ in chunks]
        self._chunk_texts = [text for _, text, _ in chunks]
        self._chunk_categories = [cat for _, _, cat in chunks]
        if not self._chunk_texts:
            self._bm25 = None  # 知识库为空，跳过 BM25（search 里退化为纯向量）
            return
        # jieba 分词建 BM25 索引（建索引和查询用同一 jieba 配置）
        self._tokenized = [jieba.lcut(t) for t in self._chunk_texts]
        self._bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, top_k: int = DEFAULT_TOP_K, category: str = None) -> list:
        """混合检索，返回 [(chunk_id, score, text)]。

        category 不为 None 时，向量 + BM25 都只在指定类别内检索（锁类别优先，避免跨类误命中）。
        注意：score 语义随路径变化——rerank 可用时是 sigmoid 概率(0-1)，降级时是 RRF 分数(~1/(60+rank))。
        下游只依赖相对排序，不要依赖 score 的绝对阈值。top_k 超出 RERANK_CANDIDATE_N 会被截断。
        """
        q_vec = self._model.encode(query, normalize_embeddings=True).tolist()

        # 1. 向量检索（score_threshold=None 全量返回；category 过滤锁类别）
        hits = self._store.search_knowledge(q_vec, limit=20, score_threshold=None, category=category)
        vec_rank = {h.payload.get("chunk_id"): rank
                    for rank, h in enumerate(hits) if h.payload.get("chunk_id")}
        vec_top1_score = hits[0].score if hits else 0.0  # 向量 top1 的 cosine 分数，供「双低拒答」判断

        # 2. BM25 检索（知识库空 / token 空则跳过，退化为纯向量）
        tokens = jieba.lcut(query)
        bm25_rank = {}
        if self._bm25 is not None and tokens:
            scores = self._bm25.get_scores(tokens)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            rank = 0
            for idx in order:
                if scores[idx] > 0:  # 0 分文档不入排名，让 fallback_rank 承担「不在结果里」语义
                    # category 过滤：类别不匹配的 chunk 跳过（锁类别，避免跨类误命中）
                    if category and self._chunk_categories[idx] != category:
                        continue
                    bm25_rank[self._chunk_ids[idx]] = rank
                    rank += 1  # 紧凑计数，保证和向量侧过滤后的连续 rank 语义一致

        # 召回层双低拒答：向量 + BM25 都认为不相关 → 判超知识库，直接返回空。
        # 不用 rerank 分数做绝对阈值——口语 query 的 rerank 分数整体偏低（贴 0.5），绝对阈值会误杀。
        # 「召回优先、拒答兜底」：只要有一路命中就继续，只有双低才拒答。
        if vec_top1_score < VEC_SCORE_LOW and not bm25_rank:
            return []

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

        # 4. Rerank 精排（先取 top_n 候选，用 CrossEncoder 精排到 top_k）
        text_map = dict(zip(self._chunk_ids, self._chunk_texts))
        top_n = min(RERANK_CANDIDATE_N, len(fused))
        candidates = fused[:top_n]
        reranked = self._rerank(query, candidates, top_k, text_map)
        if reranked is not None:
            # Rerank 只负责排序，不做绝对分数阈值拒答（拒答已移到召回层双低判断）。
            return reranked

        # 5. 降级：Rerank 不可用则退回 RRF 排序（附 text）
        result = []
        for cid, score in candidates[:top_k]:
            result.append((cid, score, text_map.get(cid, "")))
        return result

    def _rerank(self, query: str, candidates: list, top_k: int, text_map: dict):
        """CrossEncoder 精排候选，返回 [(chunk_id, rerank_score, text)]；不可用返回 None"""
        texts = [text_map.get(cid, "") for cid, _ in candidates]
        ranked = reranker.rerank(query, texts, top_k=top_k)
        if ranked is None:
            return None
        result = []
        for score, idx in ranked:
            cid = candidates[idx][0]
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
