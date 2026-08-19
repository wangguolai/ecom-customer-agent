# -*- coding: utf-8 -*-
"""Rerank 精排封装 —— CrossEncoder 懒加载单例

模型：BAAI/bge-reranker-v2-m3（~2.27GB，中文 RAG 精排主流）
定位：混合检索（召回）之后，对少量候选精排，挑最该进上下文的 Top-K。

关键点：
  - CrossEncoder 本地输出 logits（不是 0-1 概率），要 sigmoid 归一化
  - 加载失败 get_reranker() 返回 None，调用方降级退回 RRF 排序
  - 懒加载单例 + 失败只尝试一次（避免每次检索都重试加载 2.27GB）
"""

import sys
import os
import math

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
_reranker = None
_load_attempted = False


def get_reranker():
    """懒加载单例——加载失败返回 None（调用方降级退 RRF），失败只尝试一次"""
    global _reranker, _load_attempted
    if _load_attempted:
        return _reranker
    _load_attempted = True
    try:
        # 离线模式：模型已缓存就直接用本地（和 embedding.py 保持一致，避免被墙超时）
        os.environ["HF_HUB_OFFLINE"] = "1"
        from sentence_transformers import CrossEncoder
        print(f"  ⏳ 加载 rerank 模型: {_RERANKER_MODEL_NAME} ...")
        _reranker = CrossEncoder(_RERANKER_MODEL_NAME, max_length=512)
        print("  ✅ rerank 模型就绪")
    except Exception as e:
        print(f"  ⚠️ rerank 模型加载失败，检索降级为纯 RRF 排序：{e}")
        print(f"  💡 若为首次运行，请先联网下载 {_RERANKER_MODEL_NAME}（HF_ENDPOINT=https://hf-mirror.com），再重启进程重试")
        _reranker = None
    return _reranker


def _sigmoid(x: float) -> float:
    """logit → 0-1 概率"""
    return 1.0 / (1.0 + math.exp(-x))


def rerank(query: str, candidates: list[str], top_k: int = 3):
    """对候选精排，返回 [(rerank_score, 原始索引)] 降序；模型不可用或候选为空返回 None"""
    if not candidates:
        return None
    model = get_reranker()
    if model is None:
        return None
    pairs = [(query, c) for c in candidates]
    logits = model.predict(pairs)
    scores = [_sigmoid(float(l)) for l in logits]
    # 按分数降序，保留原始索引供调用方映射回 chunk_id
    indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    return [(score, idx) for idx, score in indexed[:top_k]]
