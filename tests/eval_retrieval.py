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

import time

import jieba
from src.infra.embedding import get_embedding_model
from src.infra.vector_store import QdrantStore
from src.infra.hybrid_retriever import HybridRetriever
from src.tools import detect_category

import regression
from cases import RETRIEVAL_CASES as EVAL_SET

# 检索评测集已集中到 cases.py（RETRIEVAL_CASES），此处 import 别名 EVAL_SET 保持脚本内逻辑不变。


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
    failures = []  # 混合检索 top1 错的 case（数据飞轮：自动回流进池）

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

        # 失败判定：混合检索召回不足（top1 错 或 多答案召回不全）→ 记失败（自动回流进池）
        # 规范 = top_k=3 的召回上限 min(len(expected), 3)，和毕业标准同规范，不自相矛盾
        hybrid_top1 = hybrid_titles[0] if hybrid_titles else ""
        recalled = len(set(expected) & set(hybrid_titles))
        recall_cap = min(len(expected), 3)
        if recalled < recall_cap:
            if not hybrid_titles:
                fail_type = "空返回"
            elif hybrid_top1 not in expected:
                fail_type = "top1错"
            else:
                fail_type = "多答案不全"
            failures.append({
                "query": query,
                "expected": list(expected),
                "fail_type": fail_type,
                "first_actual": hybrid_top1,
                "added_at": time.strftime("%Y-%m-%d"),
            })

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

    # 数据飞轮：失败回流 + 池回归 + fix 型毕业
    _run_regression(retriever, failures)


def _run_regression(retriever, failures):
    """数据飞轮：① 失败 case 自动回流进池 ② 池 open case 复测（回归）③ fix 型毕业 / lock 型防误修"""
    # 先取池 open case（本次回流前），再回流——避免本次刚失败的 case 又复测一遍
    pool_cases = regression.open_cases("retrieval")
    added = regression.add_failures("retrieval", failures)

    print()
    print("=" * 70)
    print(f"数据飞轮：本次回流 +{added} 条 → 回归池 open {len(pool_cases)} 条（不含本次）")
    print("-" * 70)
    if not pool_cases:
        print("📊 回归池：空（无待回归 case）")
        print("=" * 70)
        return

    passed = set()
    for c in pool_cases:
        query = c["query"]
        exp = c.get("expected") or []
        expected = set(exp) if isinstance(exp, (list, tuple)) else {exp}
        category = detect_category(query)
        _, hybrid_results = retriever.search(query, top_k=3, category=category)
        hybrid_titles = [title for _, _, _, title, _ in hybrid_results]
        top1 = hybrid_titles[0] if hybrid_titles else ""
        recalled = len(expected & set(hybrid_titles))
        recall_cap = min(len(expected), 3)

        if c.get("expect_type") == "lock":
            # 行为锁定：锁「该软推的对象」——top1 还是 first_actual 才算保持
            first_actual = c.get("first_actual", "")
            if top1 == first_actual:
                print(f"  ✅ [lock 锁定] {query}：软推保持（top1={top1}）")
            elif not hybrid_titles:
                print(f"  🔴 [lock 退化] {query}：应软推却拒答（召回空）！行为被误修，检查召回策略")
            else:
                print(f"  ⚠️ [lock 漂移] {query}：top1 {first_actual} → {top1}，软推对象变了，检查召回策略")
        else:  # fix
            if recalled >= recall_cap:
                passed.add(query)
                print(f"  ✅ [fix 修复] {query}：召回 {recalled}/{len(expected)} 达标")
            else:
                print(f"  ❌ [fix 仍坏] {query}：top1={top1 or '（空）'}，召回 {recalled}/{len(expected)}，期望 {sorted(expected)}")

    graduated = regression.graduate("retrieval", passed)
    if graduated:
        print(f"🎓 毕业 {len(graduated)} 条：{graduated}")
    open_left = len(regression.open_cases("retrieval"))
    print(f"📊 回归汇总：open {len(pool_cases)} / 通过 {len(passed)} / 毕业 {len(graduated)} / 剩余 open {open_left}")
    print("=" * 70)


if __name__ == "__main__":
    run_eval()
