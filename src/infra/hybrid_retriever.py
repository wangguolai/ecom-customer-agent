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

# 品牌名 + 品类词加进 jieba 词典，避免在 query 语境里被切成单字/错误切分，导致 BM25 跨类误匹配。
# 依据：放进「我想买{词}」语境实测（不是单独测词——单独测会漏掉「化毛膏」这类单独完整、语境里碎成「买化/毛膏」的）。
# 切成合理多词的（如「训练饼干」→训练/饼干）不补，两边 token 一致照样匹配。
_CUSTOM_WORDS = [
    # 品牌名
    "贝乐牌", "优宠牌", "喵趣牌",
    # 品类词（语境实测会碎）
    "猫粮", "狗粮", "猫砂", "钙磷",
    "磨牙棒", "猫薄荷", "逗猫棒", "猫抓板",
    "猫窝", "猫爬架",
    "老年猫", "老年犬", "小型犬",
    "化毛膏", "洁齿", "猫条", "漏食球", "梳毛",
]
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

    def search(self, query: str, top_k: int = DEFAULT_TOP_K, category: str = None):
        """混合检索，返回 (label, results)。

        label ∈ {'双高','单高一致','单高冲突','双低'} —— 四维置信度（判断层），上层据此做策略映射。
        results = [(chunk_id, score, text)]，双低时为空列表。
        category 不为 None 时，向量 + BM25 都只在指定类别内检索（锁类别优先，避免跨类误命中）。
        注意：score 语义随路径变化——rerank 可用时是 sigmoid 概率(0-1)，降级时是 RRF 分数(~1/(60+rank))。
        下游只依赖相对排序，不要依赖 score 的绝对阈值。top_k 超出 RERANK_CANDIDATE_N 会被截断。
        """
        q_vec = self._model.encode(query, normalize_embeddings=True).tolist()

        # 1. 向量检索（score_threshold=None 全量返回；category 过滤锁类别）
        hits = self._store.search_knowledge(q_vec, limit=20, score_threshold=None, category=category)
        vec_rank = {h.payload.get("chunk_id"): rank
                    for rank, h in enumerate(hits) if h.payload.get("chunk_id")}
        vec_top1_score = hits[0].score if hits else 0.0  # 向量 top1 的 cosine 分数，供四维判断
        vec_top1 = hits[0].payload.get("chunk_id") if hits else None  # 向量 top1 的 chunk_id

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
        bm25_top1 = min(bm25_rank, key=bm25_rank.get) if bm25_rank else None  # BM25 top1 的 chunk_id

        # 3. 四维置信度判断（判断层）——召回层从「双低拒答」升级为「四维分类」
        # 不用 rerank 分数做绝对阈值——口语 query 的 rerank 分数整体偏低（贴 0.5），绝对阈值会误杀。
        # 「召回优先、拒答兜底」：只要有一路命中就继续，只有双低才拒答。
        label = self._classify(vec_top1, vec_top1_score, bm25_top1)
        if label == "双低":
            return label, []

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
            # Rerank 只负责排序，不做绝对分数阈值拒答（拒答已移到召回层四维判断）。
            return label, reranked

        # 6. 降级：Rerank 不可用则退回 RRF 排序（附 text）
        result = []
        for cid, score in candidates[:top_k]:
            result.append((cid, score, text_map.get(cid, "")))
        return label, result

    def _classify(self, vec_top1: str, vec_top1_score: float, bm25_top1: str) -> str:
        """四维置信度判断（判断层，硬编码规则，与策略映射/话术生成分层）。

        - 双高：向量≥VEC_SCORE_LOW 且 BM25 有召回 且两路 top1 相同 → 直接推
        - 单高一致：只有一路强（向量高 BM25 无，或两路 top1 一致但向量弱）→ 软推 + 确认
        - 单高冲突：两路都有 top1 但不同 → 列候选 / 优先信精确词那一路
        - 双低：向量<VEC_SCORE_LOW 且 BM25 无 → 拒答 / 转人工
        """
        vec_high = vec_top1_score >= VEC_SCORE_LOW
        bm25_has = bm25_top1 is not None

        if not vec_high and not bm25_has:
            return "双低"
        if not bm25_has:
            return "单高一致"  # 只有向量一路强
        if vec_top1 == bm25_top1:
            return "双高" if vec_high else "单高一致"
        return "单高冲突"  # 两路 top1 不同

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
        label, results = retriever.search(q)
        print(f"  置信度: {label}")
        for cid, score, text in results:
            title = text.split("\n")[0].strip("# ").strip() if text else ""
            print(f"  ({score:.4f}) {title}")
