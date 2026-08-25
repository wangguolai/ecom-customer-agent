# 元数据单一数据源 + 派生数据同步 —— 架构设计方案 v2

> 触发：faithfulness 下沉方案引出的「元数据改动 → 派生数据同步」散落问题。
> 定位：系统设计主线「数据分治」的事前架构化——不是靠踩坑演进，是一开始就把「源 → 派生」链路设计对。
> v2：吸收 plan-reviewer 审核（4 阻塞 + 5 重要已闭环）。
> ⚠️ 2026-08-25 演进：本文件「决策 6（价格唯一真相在 products.md，静态）」「stock 去价格」「check_stock 只返回库存」已被**商品统一 ID 重构**推翻——价格/库存现在都是动态属性，走 MySQL products 表 + `check_stock(product_id)` 工具实时查（见 `docs/product-id-architecture.md`）。本文件相关描述是当时的历史决策，现已演进为「价格动态化」。

## 一、目标

把「元数据 → 派生数据」的同步，从当前 8 处散落的提取逻辑，收敛成**一条清晰、分层、可重建的链路**。

## 二、现状问题（已核对代码 + plan-reviewer grep 全仓）

源数据两处真相，派生数据 **8 处**散落：

| # | 派生数据 | 位置 | 同步方式 |
|---|---|---|---|
| 1 | Qdrant 向量库 | `index_products.py` | 手动重跑 ❌ |
| 2 | BM25 索引 | `hybrid_retriever._build_bm25` | 从 Qdrant scroll，自动 ✅ |
| 3 | jieba 词典 `_CUSTOM_WORDS` | `hybrid_retriever.py` | 硬编码 22 词 ❌ |
| 4 | 意图识别 `CATEGORY_KEYWORDS` | `tools.py` | 硬编码 ❌ |
| 5 | 评测白名单 品牌/价格/商品名 | `eval._load_facts` | 从 md 正则提取 ✅（各写各的） |
| 6 | 评测白名单 订单号 | `eval._load_facts` | 手写 ❌ |
| 7 | MySQL DB | `backend._init_db` | 种子灌、非空不重灌 ❌ |
| 8 | **title 反向切片** | tools/rag_pipeline/eval_retrieval/hybrid_retriever | `text.split("\n")[0].strip("# ")` 4 处，绕过 parser ❌ |

另有两个「第二真相」（非派生，但散落）：**stock 表价格 vs products.md 价格**（双真相）、**eval_retrieval 标注集**（28 条人工标注商品标题，对 title 格式硬依赖）。

## 三、架构设计：三层 + 单向数据流

```
┌─ 源数据层 SSOT（唯一真相）────────────────────────┐
│  data/products.md          （商品事实，人可读）      │
│  src/backend/seed.py        （订单/物流/库存种子）    │
│  data/category_synonyms.md  （领域知识源：查询同义词+  │
│                              分词特征词，人维护）     │
└────────────────────┬────────────────────────────┘
                     │ parse（唯一入口，解析逻辑只写一次）
                     ▼
┌─ 实体层 Domain（有 schema 的 Python 对象）─────────┐
│  Product(标题/品牌/类别/价格/描述/原始chunk)        │
│  Order / Logistics / Stock（seed 结构化）          │
└────────────────────┬────────────────────────────┘
                     │ materialize（实体 → 各种投影）
                     ▼
┌─ 派生层 Derived（全部从实体生成，可重建）───────────┐
│  ├─ 向量库（Qdrant）          ← ingestion         │
│  ├─ BM25 索引                 ← 从 Qdrant scroll   │
│  ├─ jieba 词典 ← 品牌名+类别名(自动) + 特征词(映射表)│
│  ├─ 意图类别表 ← 类别名(自动) + 同义词(映射表)      │
│  ├─ 评测白名单 facts（含状态/地点）← 全自动          │
│  ├─ MySQL DB                 ← seed 灌库（DROP 重建）│
│  └─ Redis 缓存               ← seed 重建后 FLUSH      │
└───────────────────────────────────────────────────┘
```

**单向数据流**：源 → 实体 → 派生，派生永不反向写源。
**消费方解耦**：不再自己解析源（含 title 切片），改为 import 派生层结果。

## 四、文件改动清单

### 新建（5）
| 文件 | 职责 |
|---|---|
| `src/backend/seed.py` | 抽出 `_SEED_ORDERS/_SEED_LOGISTICS/_SEED_STOCK`（STOCK 去价格只留 qty），与 FastAPI/DB 副作用解耦 |
| `src/domain/products.py` | `Product` 实体（含 `title` 字段）+ `parse_products()`（唯一 parser） |
| `src/derived/categories.py` | 意图类别表 + jieba 词典（类别名自动 + 同义词/特征词读映射表） |
| `src/derived/facts.py` | 评测白名单（brands/prices/product_names/order_ids/order_statuses/logistics_statuses/logistics_locations 全自动） |
| `src/refresh.py` | 统一 refresh：parse → 建向量库 + 灌 MySQL（DROP 重建，幂等） |

### 修改（8）
| 文件 | 改动 |
|---|---|
| `src/backend/main.py` | 删 `_SEED_*` 改 import seed；`_init_db` 用 seed；stock 表去 price 列、`/stock` 接口去价格 |
| `src/index_products.py` | 删本地 `chunk_markdown` + 正则，改用 `parse_products()` |
| `src/infra/hybrid_retriever.py` | `_CUSTOM_WORDS` 改从派生层 import；title 切片改取 Product.title |
| `src/tools.py` | `CATEGORY_KEYWORDS` 类别名 import 派生层，同义词迁出；title 切片改取 Product.title；`check_stock` 去价格只返回库存（schema description 同步去「价格」） |
| `src/rag_pipeline.py` | title 切片改取派生层；`detect_category` 改 import 派生层 |
| `tests/eval_answer_quality.py` | `_load_facts` 改 import facts；**`_kb_context()` 的状态/地点硬编码也改从 facts 生成** |
| `tests/eval_retrieval.py` | title 切片改取派生层；标注集加一致性校验（expected 标题不在派生层 → 报警） |
| `src/infra/vector_store.py` | demo 阶段**不改 payload 结构**（品牌暂不入 payload），仅声明「新增字段时 payload_schema + scroll_all 需同步」 |

### 新增数据文件（1）
| 文件 | 职责 |
|---|---|
| `data/category_synonyms.md` | 领域知识映射表：意图同义词（query→类别）+ jieba 分词特征词，人维护 |

## 五、关键设计决策

### 决策 1：内存派生 vs 持久化派生（分两类，不同步机制）
- **内存派生**（jieba 词典 / 类别表 / 评测白名单）：运行时从实体生成，模块 import 建一次。源改了 → 重启即同步，零 refresh。
- **持久化派生**（向量库 / MySQL DB / Redis 缓存）：`python -m src.refresh` 重建（含 Redis FLUSHDB）。源改了 → 跑 refresh。

### 决策 2：products.md 保留 md 格式（不升级 JSON）
- 换人可读、运营可改；代价是 parser 靠正则脆弱。demo 规模值得。迁结构化源时 parser 是唯一入口，改一处。

### 决策 3：jieba 词典 + 意图同义词，两张表「自动 vs 手动」边界显式化
- **自动提取**：品牌名（标题 `（XX牌）`）、类别名（`类别：XX` 字段）——源里显式有。
- **人维护（映射表 `category_synonyms.md`）**：① 意图同义词（「幼猫/成猫/美毛」query→类别映射）② jieba 特征词（「钙磷/化毛膏/漏食球」防切碎）。两者用途不同（一个判类别、一个保分词），但共用同一份映射表。
- jieba 词典 = 品牌名（自动）+ 类别名（自动）+ 特征词（映射表），**三源合并**，不是「纯自动」。

### 决策 4：BM25 保持从 Qdrant scroll（不重复建 chunk）
- BM25 与向量库必须用同一份 chunk，从 Qdrant scroll 天然对齐，不算散落。

### 决策 5：refresh 对 MySQL 用 DROP TABLE 重建（幂等语义）
- seed 是唯一源，`refresh` 直接 `DROP TABLE IF EXISTS` 三张表再重建灌种子，解决「非空不重灌」。
- 副作用：退款工单表 `refunds` 是运行时数据（非 seed），DROP 时不碰它。

### 决策 6：stock 双真相本次解决（价格收敛到 products.md）
- `_SEED_STOCK` 的「商品名+价格」与 products.md 重复。**qty（库存量）是独立源**（products.md 无），价格是静态属性应归 products.md。
- 本次解决：`_SEED_STOCK` 去价格只留 `(商品名, qty)`；`check_stock` 去价格只返回库存；价格唯一真相在 products.md（Product 实体）。
- 分工边界：价格（静态属性）走 `search_products` 查知识库，库存（动态属性）走 `check_stock` 查工具。这同时消掉 SYSTEM_PROMPT「价格走 search_products」与 check_stock description「查库存和价格」的现有不一致。
- 设计取舍（数据分治的判断力，不是机械规则）：demo 价格不变当静态放知识库；生产价格会变时，再把价格移到后端走工具实时查。

### 决策 7：eval_retrieval 标注集是「第二源（评测资产）」，不可自动派生
- query→正确答案是人工标注的评测 ground truth，无法从源派生。但消除它对 title 格式的**手工对齐依赖**：评测运行时校验 expected 标题是否在派生层 title 集合，不在则报警（防「改名后 Recall 恒 0」静默失效）。
- 它和 `category_synonyms.md` 一样，是「非商品真相的第二源」，显式声明，不入派生层。

## 六、实施顺序（依赖拓扑）

1. 抽 `seed.py`（无依赖，先做）
2. `domain/products.py`（parser + Product 实体，含 title 字段）
3. `derived/categories.py`（依赖 domain）；`derived/facts.py`（依赖 domain **+ seed**）
4. 改消费方 import（hybrid_retriever / tools / rag_pipeline / eval_answer_quality / eval_retrieval / index_products / backend）
5. `refresh.py`（依赖 domain + 现有 infra）
6. 回归验证：检索评测 + 回答质量评测 + 后端启动三端点

## 七、验收标准（可逐条打勾）

- [ ] 全仓 grep 无 `strip("# ")` 这类从 chunk 文本反向解析 title 的代码
- [ ] 全仓 grep `products.md` 的读取只在 `domain/products.py` 一处
- [ ] `_SEED_*` 只在 `seed.py` 定义，其余 import
- [ ] jieba 词典 22 词三源齐全（品牌 3 + 类别 3 + 特征词 16 从映射表），无丢失
- [ ] `_kb_context()` 的状态/地点从 facts 生成，无手写枚举
- [ ] 评测白名单 order_ids 从 seed 生成，无手写
- [ ] stock 表无 price 列，check_stock 返回不含价格

## 八、文档同步（改造后描述失真，需一并更新）

- `docs/migration-assets.md`：「CATEGORY_KEYWORDS 硬编码 6 类」条目
- `.md`：数据分治 / 架构演进相关条目（「硬编码→配置化」升级为「SSOT 三层架构」）
- `docs/backend-pitfalls.md`：价格存两处的坑 → 标注已识别、待收敛
- 编码规范自查：5 个新 .py 文件加 UTF-8 头 + `sys.stdout.reconfigure`

## 九、待实施时核对（不阻塞设计）

- products.md 的「类别：」字段实际取值，是否与 CATEGORY_KEYWORDS 6 个 key 一一对应
- jieba 词典 22 词逐词归类（品牌/类别/特征词），确认特征词恰好 16 个
- `tests/bench_rerank.py`、`src/demo_injection.py`、`tests/test_agent_defense.py` 里硬编码商品名，列为可选清理项（不阻塞）
