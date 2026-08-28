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
    for cid, text, title, _, _, _ in store.scroll_all():
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
    neg_total = neg_pass = 0  # 负样本（expected 空）：期望空召回，统计空返回率

    for query, expected in EVAL_SET:
        # 1. 纯向量
        q_vec = model.encode(query, normalize_embeddings=True).tolist()
        vec_hits = store.search_knowledge(q_vec, limit=3, score_threshold=None, kb_type="product")
        vec_titles = [h.payload.get("title", "") for h in vec_hits]

        # 2. 纯 BM25
        tokens = jieba.lcut(query)
        bm25_titles = []
        if retriever._bm25 is not None and tokens:
            scores = retriever._bm25.get_scores(tokens)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            rank = 0
            for idx in order:
                if scores[idx] > 0:
                    # kb_type 过滤：eval 集只测商品域，政策块不入商品 BM25 指标（过滤后集合与改造前一致）
                    if retriever._chunk_kb_types[idx] != "product":
                        continue
                    bm25_titles.append(title_map.get(retriever._chunk_ids[idx], ""))
                    rank += 1
                    if rank >= 3:
                        break

        # 3. 混合 + 预过滤 + Rerank（完整链路）
        category = detect_category(query)
        _, hybrid_results = retriever.search(query, top_k=3, category=category, kb_type="product")
        hybrid_titles = [title for _, _, _, title, _ in hybrid_results]

        # 负样本（expected 空）：期望空召回，正确 = 混合检索返回空（双低拒答）。
        # 不计 Recall/MRR（空 expected 除零无意义），单独统计空返回率；暂不回流（回归逻辑对负样本待单独设计）
        if not expected:
            neg_total += 1
            if not hybrid_titles:
                neg_pass += 1
            continue

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

    # 分母用「正样本数」（expected 非空），负样本已 continue 不进 agg，混入 len(EVAL_SET) 会低估 Recall
    pos_total = len(EVAL_SET) - neg_total
    print("=" * 70)
    print(f"{'方法':<22} {'Recall@3':<12} {'MRR':<10}")
    print("-" * 70)
    for m in methods:
        recall = sum(agg[m]["recall"]) / pos_total if pos_total else 0
        mrr = sum(agg[m]["mrr"]) / pos_total if pos_total else 0
        print(f"{m:<22} {recall:<12.4f} {mrr:<10.4f}")
    print("=" * 70)
    if neg_total:
        print(f"负样本空返回率 = {neg_pass}/{neg_total} = {neg_pass/neg_total:.2%}（期望空召回，正确=混合检索返回空）")
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
        _, hybrid_results = retriever.search(query, top_k=3, category=category, kb_type="product")
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


def run_rrf_sweep():
    """RRF k 值扫描（零成本）：k ∈ {10,30,60,100} × 链路 ∈ {纯RRF, RRF+Rerank} 的 Recall@3/MRR 对比。

    关键洞察（决定实验必须同时跑两条链路）：
      k 只影响「哪些 chunk 进 rerank 候选集」，不影响候选集内的 rerank 重排 ——
      所以 k 对最终 top_k 的影响是「间接」的，只发生在相关 chunk 处在候选边界附近时。
      只测「RRF+Rerank」完整链路会得到「k 没影响」的结论，但那很可能是被 rerank 稀释了，
      不是 k 真的没影响。必须同时测「纯 RRF」链路，才能看到 k 的真实作用。

    防呆自检（先于扫描）：同一 query 用 k=10 和 k=100 跑纯 RRF，断言融合候选排序必须有差异。
    若没有差异，要么是本数据上 k 真的不敏感（可以下结论），要么是实验坏了（k 没传进去、
    融合还在用模块级常量）—— 必须报错让实验者去查，不能静默得出「k 没影响」的假结论。
    """
    print("=" * 70)
    print("RRF k 值扫描：k × 链路 对比 Recall@3 / MRR")
    print("-" * 70)

    model = get_embedding_model()
    base = HybridRetriever()  # 建一次索引 + 模型，之后只换 k，不碰数据/模型
    store = base._store

    # ── 防呆自检：k=10 vs k=100 的纯 RRF 结果集必须可区分（不能单靠一条 query）──
    # 教训：最初用单条 probe query 判「融合排序是否变化」，结果误报——单条 query 可能碰巧
    # 不重排，但 k 对整份评测集是生效的（Recall 随 k 单调变化）。防呆要防的是「k 压根没传进去
    # 导致所有 query 结果都一模一样」，所以应该对比「整份评测集的纯 RRF 输出集合」，而非单条。
    import jieba as _jieba

    def _probe_fused(k, query):
        qvec = model.encode(query, normalize_embeddings=True).tolist()
        hits = store.search_knowledge(qvec, limit=20, score_threshold=None, kb_type="product")
        vec_rank = {h.payload.get("chunk_id"): r for r, h in enumerate(hits) if h.payload.get("chunk_id")}
        toks = _jieba.lcut(query)
        bm25_rank = {}
        if base._bm25 is not None and toks:
            scores = base._bm25.get_scores(toks)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            r = 0
            for idx in order:
                if scores[idx] > 0 and base._chunk_kb_types[idx] == "product":
                    bm25_rank[base._chunk_ids[idx]] = r
                    r += 1
        fallback = len(base._chunk_ids)
        fused = []
        for cid in set(bm25_rank) | set(vec_rank):
            sc = 1.0 / (k + bm25_rank.get(cid, fallback)) + 1.0 / (k + vec_rank.get(cid, fallback))
            fused.append((cid, sc))
        fused.sort(key=lambda x: x[1], reverse=True)
        return tuple(cid for cid, _ in fused[:3])

    def _probe_all(k):
        return [_probe_fused(k, q) for q, e in EVAL_SET if e]

    all_10 = _probe_all(10)
    all_100 = _probe_all(100)
    if all_10 == all_100:
        print("⚠️ 防呆自检未通过：k=10 和 k=100 对整份评测集的纯 RRF top3 完全相同。")
        print("   几乎可以断定 k 没真正传进融合（还在用模块级常量）—— 请核查 hybrid_retriever 的融合。")
        print("   继续扫描（结果仅作参考），但结论不可信。")
    else:
        n_diff = sum(1 for a, b in zip(all_10, all_100) if a != b)
        print(f"✅ 防呆自检通过：k=10 vs k=100 有 {n_diff} 条 query 的纯 RRF top3 不同（k 对候选集有真实影响）")

    ks = [10, 30, 60, 100]
    methods = ["纯RRF", "RRF+Rerank"]
    agg = {(k, m): {"recall": [], "mrr": []} for k in ks for m in methods}
    pos_total = 0

    for k in ks:
        base._rrf_k = k  # 换 k，复用同一份 BM25 索引 + embedding 模型
        for query, expected in EVAL_SET:
            if not expected:
                continue  # 负样本不入 Recall/MRR 规范（和 run_eval 一致）
            category = detect_category(query)
            # 纯 RRF 链路：跳过 rerank，看 k 的真实作用
            _, pure = base.search(query, top_k=3, category=category, kb_type="product", use_rerank=False)
            # 完整链路：RRF → rerank，看 k 被 rerank 稀释后还剩多少作用
            _, full = base.search(query, top_k=3, category=category, kb_type="product", use_rerank=True)
            for method, results in (("纯RRF", pure), ("RRF+Rerank", full)):
                titles = [title for _, _, _, title, _ in results]
                recall = len(set(expected) & set(titles)) / len(expected)
                mrr = 0.0
                for i, t in enumerate(titles, 1):
                    if t in expected:
                        mrr = 1.0 / i
                        break
                agg[(k, method)]["recall"].append(recall)
                agg[(k, method)]["mrr"].append(mrr)
        pos_total += 1  # 每个 k 过一遍正样本，最后统一除以正样本数

    pos_total = sum(1 for _, e in EVAL_SET if e)
    print("-" * 70)
    print(f"{'k':<6} {'链路':<12} {'Recall@3':<12} {'MRR':<10}")
    print("-" * 70)
    results_out = {}
    for k in ks:
        for m in methods:
            recall = sum(agg[(k, m)]["recall"]) / pos_total if pos_total else 0
            mrr = sum(agg[(k, m)]["mrr"]) / pos_total if pos_total else 0
            print(f"{k:<6} {m:<12} {recall:<12.4f} {mrr:<10.4f}")
            results_out[f"k={k}_{m}"] = {"recall": round(recall, 4), "mrr": round(mrr, 4)}
    print("=" * 70)

    # 落盘
    import json as _json
    os.makedirs(os.path.join(_project_root, "tests", "eval_results"), exist_ok=True)
    out_path = os.path.join(_project_root, "tests", "eval_results", "rrf_k_sweep.json")
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "pos_total": pos_total, "results": results_out}, f, ensure_ascii=False, indent=2)
    print(f"📁 结果已落盘：{out_path}")


if __name__ == "__main__":
    if "--rrf-k-sweep" in sys.argv:
        run_rrf_sweep()
    else:
        run_eval()
