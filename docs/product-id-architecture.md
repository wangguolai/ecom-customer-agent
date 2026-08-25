# 商品统一 ID 架构（SSOT 颗粒度对齐）

> 背景：商品在知识库（products.md）和 MySQL 之间没有统一 ID，靠三套字符串松散对应，动态数据（价格）无法通过 ID 关联到向量检索结果。
> 目标：给商品一个稳定实体 ID，静态属性（知识库）和动态属性（MySQL 价格/库存）通过 ID 对齐，打通「向量检索到商品 → 拿到实时价格」链路。
> 方案经 plan-reviewer 审核，补了「check_stock 工具契约」和「agent.py 路由指令」两个阻塞项。

## 一、问题现状

同一个商品在三个地方是三种不同格式的字符串：

| 位置 | 商品标识 |
|------|---------|
| `products.md` 标题 | `幼犬成长粮（贝乐牌）` |
| `orders.product` | `幼犬成长粮（贝乐牌）1.5kg`（多了规格） |
| `stock.product_name` | `幼犬成长粮`（少了品牌） |

- 向量库 chunk payload 没有 product_id（只有 title + 文本）
- MySQL 没有 products 表，只有 stock（product_name 字符串当主键）
- `Product` 实体（domain/products.py）没有 id 字段

**根因**：商品实体缺少稳定 ID，导致「向量检索到商品 → 拿价格/库存」链路是断的。

## 二、ID 体系选型（材料）

| 方案 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| **整数自增** | 短、有序（B+树聚簇索引顺序插入无页分裂）、空间小、可读 | 分库分表冲突；暴露业务量；不能离线生成（依赖 DB 返自增值）；换库重灌 ID 会变 | 单机、SSOT 是数据库 |
| **UUIDv4（随机）** | 全局唯一、离线生成、分布式友好 | 长 36 字符、无序（随机插入页分裂、索引退化）、不可读、空间大 | 分布式、需离线生成、不要求有序 |
| **UUIDv7** | 全局唯一 + 时间有序（索引友好） | 比自增长、可读性差 | 分布式 + 要索引性能 |
| **雪花 ID** | 全局唯一 + 趋势递增 + 64 位 + 高性能（本地生成） | 依赖时钟（回拨重复）、复杂度高、不可读 | 分布式高并发 |
| **业务短码**（`P001`） | 可读、稳定（跨环境不变）、SSOT 文件人工可维护、可带业务语义 | 人工保证唯一（无自动机制）、扩展性差 | SSOT 是文件/人工维护 |

**关键结论：ID 选型取决于「SSOT 在哪」**（亮点，比背优缺点值钱）：
- SSOT 是数据库 → 自增（DB 生成，简单高效）
- SSOT 是文件（人工维护，如 products.md）→ 业务短码（人在文件里写死，才能「文件 → MySQL → 向量库」三处一致）
- 分布式系统 → 雪花/UUID（无中心协调，各节点独立生成）

demo 的 SSOT 是 products.md（人工维护的 markdown），所以用**业务短码 `P001`…`P041`**：人在源文件里写死，跨环境稳定，MySQL/向量库都拿这个 ID 关联，自增 ID 做不到（文件里没有 DB 生成的自增值）。

## 三、颗粒度决策：SPU vs SKU

- **SPU（商品）**：`幼犬成长粮（贝乐牌）`——一个商品一个 ID。
- **SKU（库存单位）**：`幼犬成长粮（贝乐牌）1.5kg`——一个规格一个 ID，对应一个具体价格。

demo 用 **SPU 粒度**（一个商品一个 ID `P001`），因为源文件 products.md 本身就是「一个 `##` 块一个商品」。SKU 多规格（1.5kg ¥89 / 5kg ¥219）是生产建模，demo 不展开。

## 四、静态/动态重划（价格动态化落地）

| 层 | 内容 | 存哪 |
|----|------|------|
| 静态（不变，进知识库向量化） | 商品名、品牌、类别、成分、规格、特点 | products.md（加 ID）+ Qdrant |
| 动态（会变，走工具实时查） | **价格**、库存 | MySQL products 表 |

- **价格从 products.md 拆出**，进 MySQL products 表的 `price` 字段（单主价，取多规格第一个 ¥89 当当前价）。
- **多规格价格细节**（¥219 是 5kg 的）不进 demo——SKU 级建模是生产的事。products.md 保留「规格」行（静态展示），删「价格」行。

### 工具契约：两步链路（check_stock 收 product_id）

LLM 查价格/库存的两步链路：

```
用户问「XX 多少钱/有货吗」
  → search_products(query)  RAG 语义检索 → 返回商品描述 + product_id
  → check_stock(product_id) MySQL 精确查 → 返回 price + qty
```

- **`check_stock` 收 `product_id`，不收商品名**：名字会歧义（「猫粮」多款）/对不上（「幼犬成长粮」vs「幼犬成长粮（贝乐牌）」），ID 唯一精确。这是「RAG 返回结构化 ID、动态数据用 ID 精确查、不用脆弱字符串匹配」的落地。
- `search_products` 返回结果**明确带 `product_id` 字段**，tool schema 描述引导 LLM「查价格库存用 product_id（从 search_products 结果获取）」。
- `/stock/{product_name}` 接口 → `/products/{product_id}`。

## 五、改动清单（横切，SSOT 单向链路）

| 文件 | 改动 |
|------|------|
| `data/products.md` | 每个商品块加 `- ID：P001`；删「价格」行（价格动态化） |
| `src/domain/products.py` | `Product` 加 `id`，去掉 `prices` |
| `src/backend/seed.py` | 加 `_SEED_PRODUCTS`（product_id + name + price + qty，**41 个全有**，qty 无缺省语义） |
| `src/backend/main.py` | `_init_db` 建 `products` 表（product_id 主键 + name + price + qty）；`/stock/{product_name}` → `/products/{product_id}` |
| `src/derived/facts.py` | 价格白名单从 `_SEED_PRODUCTS` 拿（不再从 products.md parse） |
| `src/index_products.py` | chunk payload 加 `product_id` |
| `src/infra/vector_store.py` | `scroll_all` 的 `with_payload` 显式加 `product_id`（否则检索读不到） |
| `src/infra/hybrid_retriever.py` | `_build_bm25` 存 product_id；`search` 返回 4→5 元组（加 product_id） |
| `src/tools.py` | `check_stock` 改收 product_id、查价格+库存；`search_products` 返回 product_id；tool schema 描述更新 |
| **`src/agent.py`** | **SYSTEM_PROMPT 改：价格/库存走 check_stock（用 search_products 的 product_id），不再「价格走 search_products」** |
| **`search` 4→5 元组解包三处** | `tests/eval_retrieval.py`、`src/tools.py`、`src/infra/hybrid_retriever.py` 自测 |
| **接口探针连带** | `tests/test_redis_cache.py`（/stock 断言）、`tests/eval_answer_quality.py` 的 `_backend_up` 探针 |
| 评测 | 回答质量评测回归（baseline 含第二价格引用，需重跑）；检索评测回归（raw_chunk 变 → embedding 变） |
| docs | `architecture.md` / `data-sync-architecture.md` / `-mine.md` / `backend-pitfalls.md` / `category_synonyms.md` / 数据分治要点 |

**ID 唯一性校验**（新增）：`refresh.py` 校验「ID 集合内唯一 + products.md 标题集合 ↔ `_SEED_PRODUCTS` product_id 双向一致」，防人工加漏/加重 ID 导致 products 表主键冲突、md↔seed 漂移。

## 六、迁移步骤

1. 改源 `products.md`（加 ID + 删价格）→ 改 parser `domain/products.py`
2. 改 seed `_SEED_PRODUCTS` + `_init_db`（建 products 表）+ ID 唯一性校验
3. 改派生 `derived/facts.py`（白名单价格来源）
4. 改向量化（payload 带 product_id）+ 检索（返回 product_id，4→5 元组）
5. 改工具（check_stock 收 ID 查价）+ agent.py SYSTEM_PROMPT + 接口探针
6. 检索评测回归 + 回答质量评测回归（baseline 重定稿）
7. 文档 + 要点同步

## 七、边界（不做 + 已知取舍）

- 不做 SKU 级建模（多规格价格一一对应、打折字段、活动日期等业务逻辑）
- 不做价格历史/版本（demo 只存当前价）
- 不做商品增删改的运营后台（ID 在 products.md 人工维护，refresh 同步）
- 订单的 order_id 仍是业务单号 VARCHAR 主键（合理，天然唯一，不是「乱定」）
- orders 表不做 product_id 关联（订单 product 字段是「下单快照字符串」，非商品实体引用；「订单→商品实时价」场景 demo 没有，做了是过度设计）
- **已知取舍（设计取舍）**：问「5kg 装多少钱」会给主价 ¥89 顶替——SPU 粒度下「规格级价格」不存在，这是「答错」不是「拒答」。讲法：「demo 用 SPU 主价，SKU 级价格是生产建模（加 SKU 表 + 规格维度），当前主动承认只支持主价」。
