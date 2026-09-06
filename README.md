# 电商客服 Agent

一个从 0 到 1 实现的电商客服智能体，覆盖「意图识别 → 工具调用 → RAG → 多轮对话 → 转人工」完整链路。核心卖点是**生产级工程能力**，而不是「调 API 跑通对话」——手写 ReAct 循环、混合检索 + Rerank、后端横切组件（幂等 / 缓存 / 限流 / 熔断 / 鉴权）、Prompt Injection 多层防御。

## 核心能力

### 1. 手写 ReAct 循环（不套框架）

刻意不用 LangChain / LangGraph，手写 `Thought → Action → Observation` 循环，理解「LLM 只输出 JSON 指令、代码才真正执行」的本质。循环内处理五类生产坑：

| 生产坑 | 防御 |
|--------|------|
| 死循环 | 连续相同动作检测 + 最大步数上限 |
| 幻觉工具调用 | 工具名白名单校验，未知直接报错 |
| 上下文污染 | 滑动窗口截断 + 摘要压缩 |
| Token 爆炸 | 工具返回截断 + 分页 |
| Prompt Injection | 数据/指令分离 + 写权限开关 + 出网白名单 + 沙箱降权（四层防御） |

### 2. RAG 混合检索 + Rerank

- 检索链：类别预过滤 → BM25 + 向量双路召回 → RRF 融合 → BGE-Reranker 精排
- 召回策略「召回优先、拒答兜底」：四维置信度（双高 / 单高一致 / 单高冲突 / 双低）映射不同策略
- 数据分治：静态属性（材质/规格/品牌）进向量库，动态属性（价格/库存/订单/物流）走工具实时查
- 增量更新：稳定 chunk_id 做锚点，新增/修改/删除/不变四分支 diff，更新后集合对账防漂移

### 3. 后端横切组件（FastAPI + MySQL + Redis）

- **MySQL**：连接池 + 唯一约束幂等（并发双请求只产生 1 工单）
- **Redis**：Cache Aside 缓存 + 空值哨兵防穿透 + 互斥锁防击穿 + TTL 抖动防雪崩 + 降级
- **MQ**：Redis List 模拟，退款工单异步削峰 + 幂等消费 + 降级回退同步
- **高可用**：滑动窗口限流（read/write/auth 三桶）+ 三态熔断 + 退款状态机（条件 UPDATE 乐观锁）
- **安全**：手写 HS256 JWT 鉴权 + 出网白名单 + 容器降权（非 root + cap_drop ALL）

### 4. 评测与可观测

- 检索层（Recall@3 + MRR）+ 回答层（accuracy + faithfulness 结构化下沉）两层评测
- 坏 case 数据飞轮：评测失败自动回流成回归集
- 自研 Trace（单次埋点）+ MetricsStore（聚合指标）+ 故障注入 + Fuzzing

## 架构

```
用户问题（自然语言）
        ↓
AgentSession（多轮会话 + token 预算 + 摘要压缩）
        ↓
意图路由（规则优先命中高频命令，长尾/模糊 LLM 兜底）
        ↓
ReAct 循环（LLM 决策 → 工具 → 回灌，max 8 步）
        ↓
工具层（8 个 Function Calling 工具）
        ↓                        ↓
RAG 检索（混合）          FastAPI 后端（真实数据源）
BM25+向量+RRF+Rerank      /orders /logistics /products /refund
                           MySQL + Redis 横切组件
```

数据流采用 SSOT 三层架构：`源数据（products.md / seed.py）→ 实体层（Domain）→ 派生层（向量库 / BM25 / 词典 / 白名单 / MySQL / Redis）`，单向流动，派生可重建。

## 技术栈

| 层 | 选型 |
|----|------|
| LLM | DeepSeek（deepseek-chat） |
| Agent 编排 | 手写 ReAct（不用 LangChain / LangGraph） |
| Embedding | BGE-small-zh-v1.5（512 维，本地） |
| Rerank | BGE-Reranker-v2-m3（CrossEncoder，CUDA 加速） |
| 向量库 | Qdrant（本地文件模式） |
| 后端 | FastAPI + MySQL 8.0 + Redis |
| 语言 | Python 3.13 |

## 快速开始

### 环境要求

- Python 3.13
- MySQL 8.0、Redis 6+
- DeepSeek API Key

### 方式一：Docker（推荐）

后端 + MySQL + Redis 用 Docker Compose 起，agent/RAG 链路（需要 torch）留在宿主机：

```bash
# 1. 起 MySQL + Redis + 后端（三容器）
docker compose up -d

# 2. 安装 agent/RAG 依赖（宿主机）
pip install -r requirements.txt

# 3. 配置 .env（参考下方「环境变量」）
cp .env.example .env   # 填入 DEEPSEEK_API_KEY 等

# 4. 初始化数据（重建 MySQL seed + 向量库）
python -m src.refresh

# 5. 跑内置 demo（商品咨询 + 订单查询两个示例）
python -m src.agent
```

### 方式二：全本机

```bash
pip install -r requirements.txt

# 本机起好 MySQL / Redis 后，配 .env
python -m src.refresh                       # 初始化数据
uvicorn src.backend.main:app --port 8000    # 起后端（另开终端）
python -m src.agent                         # 跑 agent demo
```

### 环境变量（`.env`）

| 变量 | 说明 | 默认 |
|------|------|------|
| `DEEPSEEK_API_KEY` | 必填，DeepSeek API Key | — |
| `DEEPSEEK_BASE_URL` | API 地址 | 官方默认 |
| `DEEPSEEK_MODEL` | 模型名 | `deepseek-chat` |
| `MYSQL_HOST` / `MYSQL_PORT` | MySQL 地址 | `127.0.0.1` / `3306` |
| `MYSQL_USER` / `MYSQL_PASSWORD` | MySQL 账号 | `root` / 空 |
| `REDIS_HOST` / `REDIS_PORT` | Redis 地址 | `127.0.0.1` / `6379` |
| `AUTH_SECRET` | JWT 签名密钥（生产必须改） | `dev-only-secret-change-me` |
| `BACKEND_URL` | agent 连后端的地址 | `http://localhost:8000` |

## 量化结果

| 指标 | 数值 |
|------|------|
| 混合检索 Recall@3 | 0.57 → **0.97**（推翻 Rerank 绝对阈值误杀口语 query） |
| Rerank 延迟 | 170ms → **16ms**（torch CPU → CUDA） |
| 后端压测 | 限流器先于连接池成为并发瓶颈（每请求 4 次 Redis 往返） |
| Fuzzing | 后端 163 断言 + 工具 210 断言，抓到「孤立代理三连炸」真实边界 bug |

## 目录结构

```
src/
├── agent.py            # ReAct 循环 + 意图路由 + 多轮会话
├── rag_pipeline.py     # RAG 生成链路
├── intent_router.py    # 规则优先、LLM 兜底的路由层
├── tools.py            # 8 个 Function Calling 工具
├── refresh.py          # 统一数据重建入口（SSOT → 派生）
├── index_products.py   # 向量库全量/增量索引
├── infra/              # 基础设施（embedding/rerank/向量库/LLM/可观测）
├── domain/             # 实体层（products/policies schema）
├── derived/            # 派生层（白名单/类别/词典）
└── backend/            # FastAPI 后端（MySQL/Redis/MQ/限流/熔断/鉴权）
```
