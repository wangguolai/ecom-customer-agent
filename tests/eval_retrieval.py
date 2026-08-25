# -*- coding: utf-8 -*-
"""检索评测：量化 纯向量 / 纯BM25 / 混合检索（含预过滤+Rerank）的 Recall@3 + MRR

用法：python tests/eval_retrieval.py
指标：
  Recall@3 —— 正确答案出现在前 3 个结果里的比例（多正确答案时 = 召回数/答案总数）
  MRR      —— 第一个正确答案排名的倒数（1/rank），没召回记 0
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import jieba
from src.infra.embedding import get_embedding_model
from src.infra.vector_store import QdrantStore
from src.infra.hybrid_retriever import HybridRetriever
from src.tools import detect_category

# 标注集：query -> 正确答案标题列表（基于 data/products.md 人工标注）
# ⚠️ expected 必须用「完整标题（含品牌括号）」，和 index_products.py 入库的 title 字段
#    （re.match(r"## (.+)", chunk) 提取）严格一致，否则集合交集为空、Recall 恒 0。
EVAL_SET = [
    # ── G1 字面基线：query≈标题，三路都该对，只留 2 条做 sanity check ──
    ("松木猫砂", ["松木猫砂"]),
    ("逗猫棒", ["逗猫棒（羽毛）"]),

    # ── G2 精确词/品牌：embedding 对品牌名不敏感，测 BM25 应赢、向量可能漏 ──
    ("贝乐牌的狗粮有哪些", ["幼犬成长粮（贝乐牌）", "成犬均衡粮（贝乐牌）", "肠胃敏感狗粮（贝乐牌）", "老年犬配方粮（贝乐牌）"]),
    ("喵趣牌的猫粮有哪些", ["幼猫奶糕粮（喵趣牌）", "成猫美毛粮（喵趣牌）", "泌尿呵护猫粮（喵趣牌）", "布偶猫专用粮（喵趣牌）"]),
    ("优宠牌的粮有哪些", ["全阶段鸡肉粮（优宠牌）", "幼犬奶糕粮（优宠牌）", "大型犬高钙粮（优宠牌）", "全阶段猫粮（优宠牌）", "肠胃敏感猫粮（优宠牌）", "英短专用粮（优宠牌）"]),

    # ── G3 同义改写：字面不重叠、靠语义，测 BM25 应输、向量应赢 ──
    ("有没有粉尘少一些的猫砂推荐", ["豆腐猫砂"]),
    ("猫砂哪种结块快一点", ["膨润土猫砂"]),
    ("有没有能直接冲马桶的猫砂", ["豆腐猫砂", "松木猫砂"]),
    ("我家狗嘴巴里有味道，吃什么能清清", ["洁齿磨牙棒"]),
    ("想给猫咪补点水，有没有流质一点的零食", ["猫条（金枪鱼味）"]),
    ("狗狗爱咬东西，有没有耐啃的玩具", ["耐咬橡胶球"]),
    ("猫爪子总抓沙发，怎么防", ["猫抓板"]),
    ("遛狗的时候它老往前冲拉不住", ["宠物牵引绳"]),

    # ── G4 场景描述：绕开答案关键词，测最难召回的边界 ──
    ("我家狗狗老是乱咬东西，牙齿很脏，有没有什么零食可以清洁一下", ["洁齿磨牙棒"]),
    ("最近天热了我家猫咪老是掉毛，给自己洗完澡容易吐，有什么办法", ["猫咪化毛膏"]),
    ("我有时候懒得天天给家里的小猫小狗换水，有什么推荐吗", ["自动饮水机"]),
    ("我家狗喜欢玩水，天天弄得自己毛发乱糟糟的", ["宠物梳毛刷"]),
    ("我们家宠物出门的时候老是乱跑，有没有什么办法", ["宠物牵引绳"]),
    ("我们家小猫喜欢蹦蹦跳跳的，有没有什么吃的或者玩的能消耗一下他的体力", ["益智漏食球"]),
    ("我们家宝宝肠胃不好，有什么粮推荐吗", ["全阶段鸡肉粮（优宠牌）", "肠胃敏感狗粮（贝乐牌）", "肠胃敏感猫粮（优宠牌）"]),
    ("我家小猫刚断奶，不知道喂什么好", ["幼猫奶糕粮（喵趣牌）"]),
    ("我家狗狗年纪大了，腿脚不太利索了", ["老年犬配方粮（贝乐牌）"]),
    ("我家猫总是尿频，还老舔下面", ["泌尿呵护猫粮（喵趣牌）"]),
    ("带猫坐飞机要装什么箱子里", ["宠物航空箱"]),
    ("我家狗睡觉老刨地，想给它整个窝", ["狗窝（大号）"]),
    ("我家猫毛色越来越差，摸起来糙糙的", ["成猫美毛粮（喵趣牌）"]),
    ("我家英短越吃越胖，怕它得病", ["英短专用粮（优宠牌）"]),
    ("我家布偶毛长，老是打结成坨", ["布偶猫专用粮（喵趣牌）"]),
    ("训狗的时候想拿点东西奖励它", ["狗狗训练饼干"]),
    ("我家猫睡地上怕它冷", ["猫窝（保暖）"]),
]


def _build_title_map(store):
    """chunk_id -> title 映射（title 从 Qdrant payload 拿，不切片）"""
    title_map = {}
    for cid, text, title, _, _ in store.scroll_all():
        title_map[cid] = title
    return title_map


def _validate_expected(title_set: set) -> None:
    """一致性校验：标注集 expected 标题必须在入库 title 集合里，否则报警（防「改名后 Recall 恒 0」静默失效）"""
    missing = []
    for _, expected in EVAL_SET:
        for e in expected:
            if e not in title_set:
                missing.append(e)
    if missing:
        print(f"⚠️ 标注集 expected 标题不在知识库 title 集合（商品改名/删除？）：{sorted(set(missing))}")
        print("   → Recall 会被低估，请同步更新 EVAL_SET。")


def run_eval():
    model = get_embedding_model()
    retriever = HybridRetriever()  # 内部已创建 QdrantStore（本地模式单实例，文件锁）
    store = retriever._store       # 复用，避免同一目录起两个 client 触发锁冲突
    title_map = _build_title_map(store)
    _validate_expected(set(title_map.values()))

    methods = ["纯向量", "纯BM25", "混合+预过滤+Rerank"]
    agg = {m: {"recall": [], "mrr": []} for m in methods}

    for query, expected in EVAL_SET:
        # 1. 纯向量
        q_vec = model.encode(query, normalize_embeddings=True).tolist()
        vec_hits = store.search_knowledge(q_vec, limit=3, score_threshold=None)
        vec_titles = [h.payload.get("title", "") for h in vec_hits]

        # 2. 纯 BM25
        tokens = jieba.lcut(query)
        bm25_titles = []
        if retriever._bm25 is not None and tokens:
            scores = retriever._bm25.get_scores(tokens)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            for idx in order[:3]:
                if scores[idx] > 0:
                    bm25_titles.append(title_map.get(retriever._chunk_ids[idx], ""))

        # 3. 混合 + 预过滤 + Rerank（完整链路）
        category = detect_category(query)
        _, hybrid_results = retriever.search(query, top_k=3, category=category)
        hybrid_titles = [title for _, _, _, title, _ in hybrid_results]

        for method, titles in zip(methods, [vec_titles, bm25_titles, hybrid_titles]):
            recall = len(set(expected) & set(titles)) / len(expected)
            mrr = 0.0
            for i, t in enumerate(titles, 1):
                if t in expected:
                    mrr = 1.0 / i
                    break
            agg[method]["recall"].append(recall)
            agg[method]["mrr"].append(mrr)

    print("=" * 70)
    print(f"{'方法':<22} {'Recall@3':<12} {'MRR':<10}")
    print("-" * 70)
    for m in methods:
        recall = sum(agg[m]["recall"]) / len(EVAL_SET)
        mrr = sum(agg[m]["mrr"]) / len(EVAL_SET)
        print(f"{m:<22} {recall:<12.4f} {mrr:<10.4f}")
    print("=" * 70)


if __name__ == "__main__":
    run_eval()
