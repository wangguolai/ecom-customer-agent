# 技术选型全量对比：为什么选这个，不用那个

> 对应 README  第 6 项「说明『为什么用这个框架不用那个』」。
> 覆盖本项目用到的**每一个**技术选型，补全「那个」（替代方案）是什么、为什么不选。
> 来源：2026-08-20 WebSearch 调研 + 架构设计记录。框架选型详见 [[framework-selection]]。

---

## 1. LLM：DeepSeek（deepseek-chat）

**那个**：GPT、Claude、Qwen（通义千问）、Kimi、GLM、Gemini

| 替代 | 为什么不选 |
|------|-----------|
| GPT / Claude | 国内访问受限 + 贵 + 数据出境；中文任务不比国产强多少 |
| Kimi | 中文 5 星比 DeepSeek 高，但贵——2026 涨价后 Kimi K3 输出 ¥100/M，DeepSeek ¥27/M |
| GLM | 工具调用最稳定，但本项目用 DeepSeek 已够；客服最优其实是「GLM-4-Flash 前置分类 + DeepSeek 承接」的**组合**，demo 简化只用 DeepSeek |
| Qwen | 阿里云生态好，但本项目不绑阿里云，DeepSeek 便宜够用 |

**为什么 DeepSeek**：便宜 + 中文好（4 星）+ OpenAI 兼容（原生 Function Calling）。

**诚实点**：中文任务质量 Kimi/GLM 其实评 5 星，高于 DeepSeek 的 4 星。选 DeepSeek 是「性价比优先」，不是「效果最优」。

---

## 2. Embedding：BGE-small-zh-v1.5（512 维）

**那个**：OpenAI text-embedding、Cohere、M3E、bge-large、Qwen3-Embedding

| 替代 | 为什么不选 |
|------|-----------|
| OpenAI text-embedding | 中文弱（召回率 79%）+ 收费 + 数据出境 |
| Cohere | 中文「一般还收钱」，是 OpenAI small 的 6 倍价 |
| M3E | 轻量但中文召回率只有 71%，对比型查询明显掉队 |
| bge-large | 本项目数据量小，small 够用；large 多占 30% 存储、提升边际递减 |
| Qwen3-Embedding-8B | 中文最强（MTEB 榜首）但需 A10 级 GPU，本项目用不上 |

**为什么 BGE-small-zh**：中文 RAG 默认选项（开源标杆，下载 1500 万+）+ 本地免费（零 API 成本）+ 数据量小 512 维够用。

---

## 3. Rerank：BGE-Reranker-v2-m3

**那个**：Cohere Rerank、bge-reranker-base

| 替代 | 为什么不选 |
|------|-----------|
| Cohere Rerank | 收费 + 中文一般 |
| bge-reranker-base | ~1GB 略小，但精度不如 v2-m3 |

**为什么 v2-m3**：中文 RAG 精排标准答案 + 与 embedding 的 BGE 系列同源 + 本地免费。

---

## 4. 向量库：Qdrant（本地文件模式）

**那个**：Milvus、Chroma、Pinecone、Weaviate、FAISS

| 替代 | 为什么不选 |
|------|-----------|
| Milvus | 运维最重（要 etcd + S3 + Kafka 一堆组件），十亿级才需要，本项目 40 条数据杀鸡用牛刀 |
| Pinecone | 闭源 + SaaS 贵（10M 向量生产可达 $500+/月）+ 供应商锁定 |
| FAISS | 不是数据库，是算法库——持久化、过滤、API 全要自己搭 |
| Chroma | 本项目规模其实 Chroma 也行，但 Qdrant 原生 RRF + 可过滤索引更强 |
| Weaviate | 功能全但偏重，本项目用不上 |

**为什么 Qdrant**：无需 Docker（本地文件模式，避中国大陆网络限制）+ API 简洁 + 数据量小本地够用 + 原生 RRF 融合。

---

## 5. Agent 编排：手写 ReAct（详见 [[framework-selection]]）

**那个**：LangChain、LangGraph、CrewAI、AutoGen。

一句话：手写是「刻意的技术选型，不是技术选型结论」，为了踩坑 + 说明「LLM 只输出指令、代码才执行」。生产产品大概率该用 LangGraph。

---

## 6. 工具交互：Function Calling

**那个**：正则解析 ReAct（Thought:/Action:）、XML 提示

| 替代 | 为什么不选 |
|------|-----------|
| 正则解析 | 格式地狱——LLM 输出稍微不规范，正则就解析失败；要自己维护 prompt 格式和解析器 |
| XML 提示 | 同样要自己解析，且 LLM 不保证输出合法 XML |

**为什么 Function Calling**：API 契约保证格式可靠（模型原生输出结构化 JSON tool_calls），不用自己解析「Thought:/Action:」。

---

## 7. 后端：FastAPI

**那个**：Flask、Django、Sanic

| 替代 | 为什么不选 |
|------|-----------|
| Flask | 同步 + 无自动接口文档 + 无类型校验，要自己拼 OpenAPI |
| Django | 太重（ORM/Admin 全家桶），本项目只要 3 个接口 |
| Sanic | 异步但生态不如 FastAPI |

**为什么 FastAPI**：自动生成 `/docs`（Swagger UI）+ Pydantic 类型校验 + 异步支持 + 现代 Python 后端标配。

---

## 8. 数据库：MySQL（原 SQLite，2026-08-24 迁移）

**那个**：PostgreSQL、SQLite、Redis

| 替代 | 为什么不选 |
|------|-----------|
| SQLite | 单写者 + 无连接池，写密集会 `database is locked`；且宽松语法（TEXT 主键）掩盖 MySQL 严格性 |
| PostgreSQL | 更强大（JSON/全文检索），但 MySQL 最普及（/教程/公司都用），贴合目标 |
| Redis | 内存库，不适合做持久化订单库（除非做缓存） |

**为什么 MySQL**：后端基础盘要学「索引 / 事务 / 锁 / 连接池」这些要点，MySQL 是主流；SQLite 只是零配置起步，迁到 MySQL 是「真实工具」的演进（连接池 + 唯一约束幂等 + 二级索引）。

---

## 缓存：Redis（旁路，2026-08-24 落地）

**那个**：memcached、本地 dict、无缓存

| 替代 | 为什么不选 |
|------|-----------|
| memcached | 只支持 KV、无数据结构、无持久化，Redis 更通用 |
| 本地 dict | 单进程内有效，多实例/重启丢数据 |
| 无缓存 | 每次查 MySQL，读压力大 |

**为什么 Redis**：内存快（10w+ QPS）+ 5 种数据结构 + TTL 过期自愈。缓存是旁路、可重建副本（真源 MySQL），缓存挂降级查库不拖垮真源读。

---

## 9. 分词：jieba

**那个**：LAC（百度）、HanLP、THULAC、pkuseg

| 替代 | 为什么不选 |
|------|-----------|
| HanLP / LAC | 更重（深度学习模型），本项目 BM25 分词轻量场景用不上 |
| THULAC / pkuseg | 安装重、维护不如 jieba 活跃 |

**为什么 jieba**：最主流、上手快、词典丰富、支持 `add_word` 自定义词典（本项目补品类词就用它）。

---

## 10. 检索融合：RRF（Reciprocal Rank Fusion，k=60）

**那个**：加权融合、线性组合

| 替代 | 为什么不选 |
|------|-----------|
| 加权融合（w1·BM25 + w2·向量） | BM25 分数和向量 cosine 分数**量纲不同**，直接加权要先归一化，还要调 w1/w2 两个权重 |
| 线性组合 | 同上，且对分数敏感 |

**为什么 RRF**：只看排名不看分数，天然免疫两路分数量纲不一致的问题；k=60 是经验常数，无需调参。

---

## 选型总览

| 选型点 | 我们选的 | 核心理由 |
|--------|---------|---------|
| LLM | DeepSeek | 便宜 + 中文好 + OpenAI 兼容 |
| Embedding | BGE-small-zh | 中文标杆 + 本地免费 |
| Rerank | BGE-Reranker-v2-m3 | 中文精排标准 + 同源 |
| 向量库 | Qdrant | 免 Docker + 简洁 + 原生 RRF |
| 编排 | 手写 ReAct | 学习选择，踩坑讲底层 |
| 工具交互 | Function Calling | 契约可靠，避正则地狱 |
| 后端 | FastAPI | 自动文档 + 类型校验 |
| 数据库 | MySQL | 主流，索引/事务/连接池要点落地 |
| 缓存 | Redis | 旁路缓存，减轻 MySQL 读压力，缓存挂降级查库 |
| 分词 | jieba | 主流 + 自定义词典 |
| 融合 | RRF | 免疫分数量纲不一致 |
