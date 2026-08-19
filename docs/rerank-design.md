# Rerank 精排设计

> 状态：v1（2026-08-19）
> 定位：RAG Pipeline 主线，混合检索（召回）之后的「精排」层，README  硬项「混合检索 + Rerank」的后半段。

## 一、为什么需要 Rerank

混合检索（向量 + BM25 + RRF）是「召回」——追求快而全，排序不准。
Rerank 是「精排」——用更强的 CrossEncoder 模型对少量候选重新打分，追求准。

两阶段：
- 召回（Bi-Encoder / BM25）：query 和 doc 各算各的，能预计算、快，但无 token 级交互 → 召回快、排序不准
- 精排（CrossEncoder）：query 和 doc 拼成一条一起编码，深度交互 → 排序准，但每对都要跑一遍、慢

「先粗后精」是速度 × 精度的黄金标准。

## 二、链路

```
query → 向量 Top-20 + BM25 Top-20 → RRF 融合 → Top-20 候选 → CrossEncoder 精排 → Top-3 交给 LLM
```

现有 `HybridRetriever` 已经做到「RRF 融合 → Top-3」，缺「融合后先取 Top-20 候选 → 精排」这一步。

## 三、模型选型

| 模型 | 大小 | 精度 | 结论 |
|------|------|------|------|
| BGE-Reranker-v2-m3 | 2.27GB | 最准，中文精排主流 | ✅ 选用 |
| bge-reranker-base | ~1GB | 略低 | 备选 |

选 v2-m3：中文 RAG 精排标准答案，与 embedding 的 BGE 系列同源。

## 四、文件改动

- 新建 `src/infra/reranker.py`：CrossEncoder 懒加载单例（照 embedding.py 模式），`rerank(query, candidates, top_k) -> [(score, 原始索引)]`
- 改 `src/infra/hybrid_retriever.py`：RRF 融合后取 top_n=20 候选 → rerank → top_k；降级退 RRF

## 五、关键坑（设计要点）

1. **logits → sigmoid**：CrossEncoder 本地输出 logits（不是 0-1 概率），要 `1/(1+e^-x)` 归一化
2. **降级**：模型没装/加载失败 → 退 RRF 排序，不拖垮检索链
3. **候选数自适应**：`top_n = min(20, 库总量)`，否则候选比库多，Rerank 没意义
4. **score 量纲变化**：Rerank 后 score 从 RRF 的 1/(60+rank) 变成 sigmoid 的 0-1 概率，下游若有分数阈值逻辑需注意
