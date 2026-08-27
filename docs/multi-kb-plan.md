# 多知识库适配 —— 设计文档

> 目标：把「退换/售后政策」从硬编码常量迁到 RAG 知识库，形成「商品库 + 政策库」两个知识域，
> 落地多知识库适配三件套——路由（意图→知识域）、隔离（kb_type 检索前过滤）、冲突裁决（来源优先级）。
>
> 本版已过 plan-reviewer 审核，采纳 3 阻塞 + 3 建议，复核 1 项「需用户介入」为过虑。

## 1. 现状（精确）

- 唯一知识库 `product_knowledge` collection，存 products.md 41 个商品块（`src/infra/vector_store.py:30-35`）。
- 退换政策硬编码：`RETURN_POLICY` 常量（`src/tools.py:207-213`），`get_return_policy()` 无参整段返回（`tools.py:293-295`），工具 schema 无参数（`tools.py:98-101`）。
- `REQUIREMENTS.md` 第 5 节设计「政策走 RAG」，实现只做了硬编码工具——欠账，是拆第二库最干净的切入口。
- 意图路由已有「规则优先、LLM 兜底」：`_POLICY_WORDS`（`src/intent_router.py:40`）命中 → `get_return_policy` args=`{}`（`intent_router.py:86-87`）。
- 检索链路 `HybridRetriever.search(query, top_k, category)`：category 过滤已在检索前做（向量 `vector_store.py:159-162`、BM25 `hybrid_retriever.py:90-94`）——「payload 字段 + 检索前 filter」已有先例，加 kb_type 是「多一个过滤维度」不是「新机制」。

## 2. 三个补点取舍（用户授权我定，最后给取舍说明）

### 补点 1：逻辑多库 vs 物理多库 → 选逻辑多库（共享 collection + kb_type metadata）

- 取舍：共享 `product_knowledge` collection，payload 加 `kb_type` 字段（"product"/"policy"），检索前 filter。
- 理由：① 体量小（41 商品 + 4 政策）② embedding 同构（都 BGE-small-zh 512 维 COSINE）③ category 过滤已有同款先例。
- 设计取舍（取舍说明核心）：**逻辑多库（metadata 隔离）适合「同构数据、共享 embedding、体量小」；物理多库（独立 collection）适合「不同 embedding 模型/维度/分块粒度/更新频率/权限隔离」。** 本项目政策/商品同构、共享检索基础设施，物理拆库收益为零、代价是改 vector_store 三处硬编码 collection 名 + 多套索引管理。
- 演进边界：政策库需要独立 embedding 或不同分块粒度时才拆独立 collection，当前不满足。

### 补点 2：政策 4 条拆不拆子条款 → 检索子条款 + 始终附整段参考

- 取舍：policies.md 每条政策一个 `##` 块（标题带强关键词）；`get_return_policy` 走政策 RAG 检索命中的政策块，**同时附整段 `RETURN_POLICY` 作参考**（不追求「只返回精准单块」）。
- 理由：政策 4 条语义高度相近（都退货/退款域），「双低才兜底」挡不住「单高命中错块」（如「退货几天到账」可能命中「7天无理由」块）。整段仅 ~100 字、token 成本可忽略，附整段让 LLM 有完整上下文纠偏。
- 政策 RAG 的**真实价值**不是「极致精准子条款命中」，是「政策从代码常量变成知识库数据」——数据/代码分离，改政策不用改代码、refresh 即可。按这个规范说明，不吹「子条款精准命中」。
- 实测点：入库后测「食品拆封能退吗」「退款多久到账」能否命中对应块；区分度不够就调标题关键词（有整段兜底，命中不准也不致命）。

### 补点 3：交叉查询边界 → 政策规则路由已天然收紧，交叉走 ReAct

- 取舍：`_POLICY_WORDS` 只含「明确政策词」（退货政策/七天无理由/退货流程/退货条件/退货运费），**不含**「能退吗/我要退货」这类模糊词（`intent_router.py:39` 注释明确「能退吗/我要退货走 LLM 避免和退款混淆」）。「这粮能不能退」这种「商品×政策」交叉查询本来就不被规则路由，走 ReAct 由 LLM 决策并发调 `search_products` + `get_return_policy`——冲突裁决的真实触发场景，靠 prompt 优先级表 + LLM 综合。
- 验证点：测「这粮能不能退」route_source = None（走 ReAct）。

## 3. 详细设计

### 3.1 新增 data/policies.md

```
# 退换货政策知识库

## 7天无理由退货
- 适用条件：商品未拆封、不影响二次销售
- 政策：7 天内可无理由退货

## 质量问题15天退换
- 适用条件：商品存在质量问题
- 政策：15 天内可退换

## 食品拆封不退
- 适用条件：食品类（粮/零食）
- 政策：拆封后不支持无理由退换

## 退货流程与退款时效
- 政策：退货需提供订单号，退款 1-3 个工作日到账
```

（4 块标题带强关键词，与 `RETURN_POLICY` 四条一一对应）

### 3.2 新增 src/domain/policies.py

`parse_policies()` 仿 `products.py`：读 policies.md → 按 `##` 分块 → `Policy(title, raw_chunk)` 列表。政策无 category / ID。

**policy payload 必须含**（对照商品 `index_products.py:37-40`）：
- `source_file="policies.md"`（`delete_by_source` 按此精确删旧块，缺了删不掉残留）
- `chunk_id=f"policies:{i}"`（和商品 `f"products:{i}"` 同款，幂等覆盖）

### 3.3 src/infra/vector_store.py

- `COLLECTIONS["product_knowledge"]["payload_schema"]` 加 `"kb_type"`。
- `scroll_all` 的 `with_payload` 加 `kb_type`，返回 6 元组 `(chunk_id, text, title, category, product_id, kb_type)`。
- `search_knowledge` 加 `kb_type` 参数，`query_filter` 同时带 `category` + `kb_type`（都检索前 filter）。

### 3.4 src/infra/hybrid_retriever.py

- `_build_bm25` 解包 6 元组，存 `_chunk_kb_types`。
- `search` 加 `kb_type` 参数（**默认 `None` = 全库不过滤**，所有商品调用点显式传 `"product"`，防「默认藏 bug」）：
  - 向量检索传 `kb_type` 给 `search_knowledge`。
  - BM25 循环内加 `kb_type` 过滤，位置和 category 一样在 `continue` 之前（保持紧凑 rank 语义，见 `hybrid_retriever.py:88-94`）。

### 3.5 src/index_products.py

`build_knowledge_base()` 索引两个源：
- 商品：`parse_products()` → payload 加 `kb_type="product"`，category 不变。
- 政策：`parse_policies()` → payload 加 `kb_type="policy"`，`category=""`。
- `delete_by_source` 分别按 `products.md` / `policies.md` 删（幂等重建）。

### 3.6 src/tools.py

- `TOOL_SCHEMAS` 里 `get_return_policy` 加 `query` 参数（required）。
- `get_return_policy(query)` 改走政策 RAG（`asyncio.to_thread`，和 `search_products` 同款）：
  1. `HybridRetriever.search(query, kb_type="policy")` 检索政策块。
  2. 返回「命中的政策块（带 `[来源: 退货政策]` 标注）+ 整段 `RETURN_POLICY` 参考」，LLM 综合。
  3. 检索 `双低`/空 或 **政策块缺失（BM25 索引里无 policy，说明没入库）→ 打印警告 + 整段兜底**（不静默退化）。
- `_search_products_sync` 调 `search(..., kb_type="product")`。
- `RETURN_POLICY` 常量保留（整段兜底）。

### 3.7 src/intent_router.py

政策路由 args 从 `{}` 改成 `{"query": user_msg}`（`intent_router.py:87`）。

### 3.8 src/agent.py（软裁决）

`SYSTEM_PROMPT` 加一条优先级说明：「退货/售后条款以 `get_return_policy`（政策知识库）为准，价格/库存/订单状态/物流以实时工具（后端）为准，商品静态信息以 `search_products`（商品知识库）为准；跨来源冲突时如实说明、不编造。」

### 3.9 src/rag_pipeline.py（plan-reviewer 阻塞 2 补上）

`retriever.search(...)` 显式传 `kb_type="product"`，防政策块污染商品 RAG 回答。

### 3.10 src/refresh.py

进度输出 `[3/3]` 描述改成「重建知识库（商品 + 政策）」。

**部署约束**（写入 refresh 文档）：先 `python -m src.refresh` 再启动服务进程——单例 BM25 索引在进程启动时从 Qdrant scroll 全量建，政策必须在启动前已入库。

## 4. 隔离 + 冲突裁决

- **隔离**：`search_products` 只返回 `kb_type="product"`，`get_return_policy` 只返回 `kb_type="policy"`（工具层隔离）；`kb_type` 只在检索前 filter，不进 embedding 文本。
- **优先级表**（设计取舍）：价格/库存/订单状态/物流 → MySQL 工具（实时）；退货/售后条款 → policy KB；商品静态属性 → product KB。
- **跨域冲突**：ReAct 里 LLM 按 SYSTEM_PROMPT 优先级综合。
- **政策域「单高冲突」**：政策块是**互补规则不是互斥选项**，`get_return_policy` 合并呈现两路候选文本给 LLM（不是学商品域「列候选让用户选」）。

## 5. 改动文件清单

新增：`data/policies.md`、`src/domain/policies.py`
修改：`src/infra/vector_store.py`、`src/infra/hybrid_retriever.py`、`src/index_products.py`、`src/tools.py`、`src/intent_router.py`、`src/agent.py`、`src/rag_pipeline.py`、`src/refresh.py`、`tests/eval_retrieval.py`（解包 6 元组 + 三处传 kb_type="product"）、`tests/cases.py`（新增纯政策 case）

## 6. 测试方案

1. 零付费：`py_compile` 全改文件 + `import` 断言。
2. 入库后检索区分度：测「食品拆封能退吗」「退款多久到账」「7天无理由」各命中对应块（有整段兜底，命中不准不致命，重点看能否命中）。
3. 交叉查询：测「这粮能不能退」route_source = None（走 ReAct）。
4. 商品检索回归：`eval_retrieval.py` 全量（Recall/MRR 不应变——商品块 + kb_type="product" 过滤后集合不变，且已显式传 kb_type）。
5. 政策块缺失检测：进程内模拟「政策未入库」场景，确认 get_return_policy 警告 + 兜底，不静默退化。
6. 付费（用户说跑才跑）：`eval_answer_quality.py` 回归（含新增纯政策 case「食品拆封能退吗」）。
