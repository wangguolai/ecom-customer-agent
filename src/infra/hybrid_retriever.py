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
from src.derived.categories import build_jieba_words

RRF_K = 60
DEFAULT_TOP_K = 3
RERANK_CANDIDATE_N = 10  # RRF 融合后先取这么多候选交给 Rerank 精排。
# 实测 Recall@3：N=5→0.7667、N=10→0.9333、N=15→0.9667、N=20→0.9667。
# 选 N=10 是成本收益权衡（不是 Recall 最大化）：0.9333 对电商客服够用，省一半 rerank 推理；
# 为 0.033 升到 N=15 多花 50% 成本不值（正确性投入匹配错误成本）。N 跟数据规模+业务容忍度走，不拍脑袋。
VEC_SCORE_LOW = 0.4  # 召回层双低拒答：向量 top1 分数 < 此值且 BM25 无召回 → 判超知识库，拒答

# jieba 词典从派生层生成：品牌名（自动，从 products.md）+ 特征词（人维护，映射表 category_synonyms.md）。
# 特征词依据「我想买{词}」语境实测会碎才加（切成合理多词的如「训练饼干」不补）。
for _w in build_jieba_words():
    jieba.add_word(_w)


class HybridRetriever:
    """BM25 + 向量 + RRF 混合检索"""

    def __init__(self, rrf_k: int = RRF_K, store=None, model=None):
        # rrf_k 实例化可配（默认 = 模块级 RRF_K，生产行为零变化）。
        # 参数化的目的是做 k 值扫描实验：k 决定 RRF 融合后哪些 chunk 进入 rerank 候选集，
        # 所以它通过「相关 chunk 是否落在候选边界内」间接影响最终 top_k。
        # ⚠️ 融合计算必须用 self._rrf_k，绝不能用模块级 RRF_K —— 那样扫描会静默测同一个 k，
        # 八组结果全同，得出「k 没影响」的假结论且毫无察觉。
        # store/model 可注入：k 扫描要在同一份数据上换 k 重跑，每个实例都新建 QdrantStore 会
        # 触发本地文件锁冲突（AlreadyLocked）。注入共享实例，让「换 k」不碰「数据/模型」。
        self._rrf_k = rrf_k
        self._store = store if store is not None else QdrantStore()
        self._model = model if model is not None else get_embedding_model()
        self._build_bm25()

    def _build_bm25(self):
        """从 Qdrant scroll 出所有 chunk 建 BM25 索引，保证和向量库数据对齐"""
        chunks = self._store.scroll_all()  # [(chunk_id, text, title, category, product_id, kb_type)]
        self._chunk_ids = [cid for cid, _, _, _, _, _ in chunks]
        self._chunk_texts = [text for _, text, _, _, _, _ in chunks]
        self._chunk_titles = [title for _, _, title, _, _, _ in chunks]
        self._chunk_categories = [cat for _, _, _, cat, _, _ in chunks]
        self._chunk_product_ids = [pid for _, _, _, _, pid, _ in chunks]
        self._chunk_kb_types = [kb for _, _, _, _, _, kb in chunks]
        if not self._chunk_texts:
            self._bm25 = None  # 知识库为空，跳过 BM25（search 里退化为纯向量）
            return
        # jieba 分词建 BM25 索引（建索引和查询用同一 jieba 配置）
        self._tokenized = [jieba.lcut(t) for t in self._chunk_texts]
        self._bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, top_k: int = DEFAULT_TOP_K, category: str = None, kb_type: str = None,
               use_rerank: bool = True):
        """混合检索，返回 (label, results)。

        label ∈ {'双高','单高一致','单高冲突','双低'} —— 四维置信度（判断层），上层据此做策略映射。
        results = [(chunk_id, score, text, title, product_id)]，双低时为空列表。
        category 不为 None 时，向量 + BM25 都只在指定类别内检索（锁类别优先，避免跨类误命中）。
        kb_type 不为 None 时，向量 + BM25 都只在指定知识域内检索（默认 None = 全库不过滤，多知识域隔离）。
        注意：score 语义随路径变化——rerank 可用时是 sigmoid 概率(0-1)，降级时是 RRF 分数(~1/(60+rank))。
        下游只依赖相对排序，不要依赖 score 的绝对阈值。top_k 超出 RERANK_CANDIDATE_N 会被截断。
        """
        q_vec = self._model.encode(query, normalize_embeddings=True).tolist()

        # 1. 向量检索（score_threshold=None 全量返回；category / kb_type 过滤锁知识域）
        hits = self._store.search_knowledge(q_vec, limit=20, score_threshold=None, category=category, kb_type=kb_type)
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
                    # kb_type 过滤：知识域不匹配的 chunk 跳过（锁知识域，避免跨域污染）
                    if kb_type and self._chunk_kb_types[idx] != kb_type:
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
        fused = self._rrf_fuse(bm25_rank, vec_rank)

        # 4. Rerank 精排（先取 top_n 候选，用 CrossEncoder 精排到 top_k）
        text_map = dict(zip(self._chunk_ids, self._chunk_texts))
        title_map = dict(zip(self._chunk_ids, self._chunk_titles))
        pid_map = dict(zip(self._chunk_ids, self._chunk_product_ids))
        top_n = min(RERANK_CANDIDATE_N, len(fused))
        candidates = fused[:top_n]

        # 单高冲突：两路 top1 各执一词，且各自只在单路强（另一路几乎没分），RRF 融合可能把它们
        # 挤出 top_n 候选。策略是「列候选软推、让用户选」（不是「优先信精确词」）——先保两路 top1 进候选。
        if label == "单高冲突":
            candidate_ids = {cid for cid, _ in candidates}
            for keep_cid in (vec_top1, bm25_top1):
                if keep_cid is not None and keep_cid not in candidate_ids:
                    for cid, score in fused:  # 两路 top1 必在 fused（RRF 并集）里，找回原始 RRF 分
                        if cid == keep_cid:
                            candidates.append((cid, score))
                            candidate_ids.add(cid)
                            break

        # 单高冲突时让 rerank 返回全量候选排序，才能把被挤出 top_k 的那一路 top1 补回来（列候选）
        rerank_top_k = len(candidates) if label == "单高冲突" else top_k
        # use_rerank=False 走的就是下面已有的「rerank 不可用 → 退回 RRF 排序」降级路径。
        # 不是为测试新开的后门：这条降级路径本来就存在，开关只是让它可以被显式触发，
        # 便于把「纯 RRF 排序」和「RRF + rerank」拉出来对比（k 值扫描要用）。
        reranked = self._rerank(query, candidates, rerank_top_k, text_map, title_map, pid_map) if use_rerank else None
        if reranked is not None:
            # Rerank 只负责排序，不做绝对分数阈值拒答（拒答已移到召回层四维判断）。
            if label == "单高冲突":
                reranked = self._merge_conflict_top1(
                    reranked, candidates, top_k, text_map, title_map, pid_map, vec_top1, bm25_top1
                )
            return label, reranked

        # 6. 降级：Rerank 不可用则退回 RRF 排序（附 text + title + product_id）
        result = []
        for cid, score in candidates[:top_k]:
            result.append((cid, score, text_map.get(cid, ""), title_map.get(cid, ""), pid_map.get(cid, "")))
        return label, result

    def _rrf_fuse(self, bm25_rank: dict, vec_rank: dict) -> list:
        """RRF 融合：取两路结果并集，按 1/(k+rank_bm25) + 1/(k+rank_vec) 打分降序。

        抽成方法一是让 search() 干净，二是 k 扫描评测要直接拿「融合后的候选排序」做防呆自检
        （对比不同 k 的候选差异），否则只能靠最终 top_k 反推、还会被 rerank 掩盖掉。
        """
        all_cids = set(bm25_rank.keys()) | set(vec_rank.keys())
        fallback_rank = len(self._chunk_ids)  # 没进检索结果的兜底排名
        fused = []
        for cid in all_cids:
            r_bm = bm25_rank.get(cid, fallback_rank)
            r_vec = vec_rank.get(cid, fallback_rank)
            score = 1.0 / (self._rrf_k + r_bm) + 1.0 / (self._rrf_k + r_vec)
            fused.append((cid, score))
        fused.sort(key=lambda x: x[1], reverse=True)
        return fused

    def has_kb_type(self, kb_type: str) -> bool:
        """检查 BM25 索引里是否存在指定 kb_type 值的 chunk（政策块缺失检测：未入库时 get_return_policy 走兜底）"""
        return kb_type in self._chunk_kb_types

    def _classify(self, vec_top1: str, vec_top1_score: float, bm25_top1: str) -> str:
        """四维置信度判断（判断层，硬编码规则，与策略映射/话术生成分层）。

        - 双高：向量≥VEC_SCORE_LOW 且 BM25 有召回 且两路 top1 相同 → 直接推
        - 单高一致：只有一路强（向量高 BM25 无，或两路 top1 一致但向量弱）→ 软推 + 确认
        - 单高冲突：两路都有 top1 但不同 → 列候选软推（两路都列，让用户选，不偏袒精确词/语义）
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

    def _rerank(self, query: str, candidates: list, top_k: int, text_map: dict, title_map: dict, pid_map: dict):
        """CrossEncoder 精排候选，返回 [(chunk_id, rerank_score, text, title, product_id)]；不可用返回 None"""
        texts = [text_map.get(cid, "") for cid, _ in candidates]
        ranked = reranker.rerank(query, texts, top_k=top_k)
        if ranked is None:
            return None
        result = []
        for score, idx in ranked:
            cid = candidates[idx][0]
            result.append((cid, score, text_map.get(cid, ""), title_map.get(cid, ""), pid_map.get(cid, "")))
        return result

    def _merge_conflict_top1(self, reranked, candidates, top_k, text_map, title_map, pid_map,
                             vec_top1, bm25_top1):
        """单高冲突：两路 top1 都列进候选（软推、不偏袒），再跟 rerank 其余结果，截断到 top_k。

        reranked 是全量候选的 rerank 降序（调用方以 top_k=len(candidates) 调用才拿得到全量）。
        两路 top1 若被 rerank 排到 top_k 之外，仍要保证在最终候选里——否则「列候选」少一路，
        用户没得选（等价于偷偷偏袒了另一路）。两路 top1 之间按 rerank 分排，不硬性把精确词那一路排第一。
        """
        keep_ids = {cid for cid in (vec_top1, bm25_top1) if cid is not None}
        keep = [r for r in reranked if r[0] in keep_ids]  # 按 rerank 分降序，不偏袒精确词/语义
        # 兜底：两路 top1 理应在 reranked 里，防御式从 candidates 补（rerank 给 0 分占位，只保证「列出来」）
        for cid in (vec_top1, bm25_top1):
            if cid is not None and cid not in {k[0] for k in keep}:
                for ccid, _ in candidates:
                    if ccid == cid:
                        keep.append((cid, 0.0, text_map.get(cid, ""), title_map.get(cid, ""), pid_map.get(cid, "")))
                        break
        rest = [r for r in reranked if r[0] not in keep_ids]
        return (keep + rest)[:top_k]


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    retriever = HybridRetriever()
    for q in ["皇家牌有哪些粮", "肠胃敏感的粮", "幼犬吃什么"]:
        print("=" * 50)
        print(f"Q: {q}")
        label, results = retriever.search(q)
        print(f"  置信度: {label}")
        for cid, score, text, title, pid in results:
            print(f"  ({score:.4f}) {title} [{pid}]")
