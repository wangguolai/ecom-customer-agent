# 混合检索设计（第一步：BM25 + 向量 + RRF）v2

> RAG 深化第一步。把检索从「单向量检索」升级到「混合检索」，解决「纯向量对精确词（品牌名/型号/专有名词）不敏感」的坑。**本设计不含 Rerank**。
>
> v2：已过 plan-reviewer，修 5 个阻塞 + 采纳 8 个建议。

## 1. 要解决的坑

纯向量检索按语义走，对「精确词」（品牌名「贝乐牌」、特征词「肠胃敏感」）在**正确类别内**也可能漏掉——因为向量按语义相似度，不按关键词精确匹配。

> ⚠️ 注意：不是「问猫粮漏掉犬粮」那种跨类别漏（那是对的，锁类别优先）。是「问贝乐牌，向量可能漏掉贝乐牌自己的商品」这种**同类别内**的精确词漏召回。

## 2. 技术方案

**BM25（稀疏/关键词）+ 向量（稠密/语义）→ RRF 融合（k=60）**

| 环节 | 选型 | 说明 |
|------|------|------|
| BM25 检索 | jieba 分词 + rank-bm25 | 中文 BM25 标准组合，精确词匹配 |
| 向量检索 | 已有 Qdrant + BGE 512 维 | 复用，语义匹配，**去掉 score_threshold** |
| 融合 | RRF，k=60 | `score(d) = Σ 1/(60 + rank_i(d))`，只看排名不看分数 |

**RRF 公式**：`score(d) = 1/(60 + rank_bm25) + 1/(60 + rank_vec)`，rank 统一 **0-based**（第 1 名 rank=0，由 score 排序 argsort 生成）。

## 3. 文件改动

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/infra/vector_store.py` | 改 | 新增 `scroll_all` 方法，返回所有 chunk 的 `(chunk_id, text)` |
| `src/infra/hybrid_retriever.py` | 新增 | BM25 索引构建 + 混合检索 + RRF 融合 |
| `src/tools.py` | 改 | `search_products` 从单向量检索换成混合检索 |
| `requirements.txt` | 新增 | 项目当前没有依赖文件，新建，写入 `jieba`、`rank-bm25` |

## 4. 关键实现细节

### 4.1 BM25 索引构建（和数据源对齐）

- 懒加载时从 Qdrant `scroll_all` 出所有 chunk 的 `(chunk_id, text)`
- **显式维护 `doc_index → chunk_id` 映射**（rank-bm25 的 corpus 用列表下标标识文档，必须映射到 chunk_id，不能拿 scroll 列表下标当 chunk_id，否则和向量结果错位）
- jieba 配置：`jieba.add_word("贝乐牌")` 等，把品牌名、专有名词加进词典（否则「贝乐牌」被切成「贝/乐/牌」，匹配不上）
- **建索引和查询用完全相同的 jieba 配置**（分词模式 + 自定义词典），保证 token 一致

### 4.2 混合检索流程

```
query
  → 分词（jieba，空 token 则跳过 BM25 直接走向量）
  → BM25：对所有 chunk 打分 → argsort 得 0-based rank_bm25
  → 向量：embedding → Qdrant search limit=20, score_threshold=None（全量返回 15 个）→ rank_vec
  → 对「两个结果并集」的每个 chunk 算 RRF = 1/(60+rank_bm25) + 1/(60+rank_vec)
  → 按 RRF 排序，返回 Top-K（默认 3）
```

### 4.3 边界处理

- **rank 对称**：去掉 `score_threshold` 后，15 个 chunk 向量全量返回，BM25 也全量（15 个），两者 rank 都是 0~14，对称。兜底 rank = 15（chunk 总数），不是 20。
- **BM25 命中 0 个**（query 无任何 token 命中，所有 chunk 0 分）：BM25 检索实际失败，**退化为纯向量检索**，不做并列末位强行融合。
- **空 query / 分词后 token 为空**：直接走向量检索。
- **向量空**：返回空，`search_products` 返回「未查到」。
- **BM25 索引的内容**：对 chunk 的 text 分词（text 是带 `## 标题`/`- 标签：` 的 markdown，标签词「类别」「适用对象」会进索引但 IDF 低影响小；类别混淆靠后续 category 过滤解决，见第 6 节待决策）。

## 5. 验收标准

1. 「贝乐牌有哪些粮」→ 精确召回贝乐牌的 **2 款**狗粮（幼犬成长粮 + 成犬均衡粮），之前纯向量可能漏
2. 「肠胃敏感的粮」→ 召回含「肠胃敏感」关键词的「全阶段鸡肉粮」（不带类别约束的关键词召回，之前纯向量漏掉）
3. 语义查询不退化：「幼犬吃什么」仍能正确召回幼犬成长粮（混合检索不差于单向量）

## 6. 这一步不做的（第二步）

- Rerank（BGE-Reranker 精排）
- 动态权重（BM25/向量权重按查询类型调优）
- 查询改写（同义词扩展、指代消解）
- **category 预过滤（锁类别）** —— 待用户决策（见下）
