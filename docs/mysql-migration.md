# 模块 2 · MySQL 落地方案 v2（SQLite → MySQL）

> 学习定位：**系统式**（后端基础给完整内容 + 为什么 + 设计要点，见 `/02-mysql.md`）。
> v2：吸收 plan-reviewer 审核（2 阻塞 + 5 重要 + 6 建议已闭环）。

## 一、目标

backend 数据层 `sqlite3` → MySQL，落地「订单号唯一索引 + 连接池 + 事务（退款原子性 + 幂等）」三个要点。

## 二、现状（元数据同步重构后）

- `src/backend/seed.py`：种子数据（`_SEED_ORDERS`/`_SEED_LOGISTICS`/`_SEED_STOCK`，STOCK 已去价格）
- `src/backend/main.py`：FastAPI + sqlite3 直连；`_init_db()` 在**模块 import 时顶层执行**（底部调用）；`/refund` 每个操作各 `sqlite3.connect` 一次、无事务、幂等靠「查重复 + 插入」
- `src/refresh.py`：统一重建入口（`_init_db` + 向量库）

**落地要解决的**：① 无连接池 ② refund 无事务 ③ refund 幂等竞态（先查后插） ④ 主键 TEXT 在 MySQL 下非法 ⑤ import 时执行 DB 初始化。

## 三、技术选型：pymysql + DBUtils

| 选项 | 结论 | 理由 |
|---|---|---|
| **pymysql + DBUtils** | ✅ | 裸 SQL 看清每条 SQL（）；DBUtils 连接池；同步阻塞在 FastAPI `def` 端点（线程池）够用 |
| SQLAlchemy | ❌ | ORM 遮住 SQL 细节，demo 不值 |
| aiomysql | ❌ | backend 保持同步 `def`（agent 已异步化，backend 是独立服务），不需要异步驱动 |

**同步 vs 异步**：agent 侧已 asyncio，backend 保持同步 `def` + 线程池，模块 2 不引入 backend 异步化。

## 四、改动清单

| 文件 | 改动 |
|---|---|
| `requirements.txt` | 加 `pymysql` + `DBUtils` |
| `.env` | 新增 `MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB` |
| `src/backend/db.py`(新) | 连接池封装：`python-dotenv` 读 .env 配置 → PooledDB；`get_conn()` 取连接、`close_conn()` 归还 |
| `src/backend/main.py` | ① `sqlite3.connect` → `db.get_conn()` ② DDL 主键列 TEXT→VARCHAR(n) ③ DML 占位符 `?`→`%s` ④ 读接口 `autocommit=True` ⑤ `/refund` 显式事务 + 唯一约束 ⑥ `_init_db` 移入 lifespan |
| `src/backend/seed.py` | 不变（纯数据，与存储引擎解耦） |
| `src/refresh.py` | 避免 import 触发 + 显式调用双 DROP（改调 lifespan 里的建表函数） |

## 五、关键设计决策

### 决策 1：连接池（DBUtils PooledDB）
- **为什么**：每请求「TCP 握手 + MySQL 认证」新建连接贵，池化复用。
- **参数**：`maxconnections=5`（demo 够）、`mincached=1`、`maxcached=5`、**`ping=1`**（每次取连接前 ping，检测被 MySQL `wait_timeout` 杀掉的死连接）。
- **连接泄漏**（要点）：连接用完必须 `close`（归还池），忘还 → 池耗尽后续请求卡死。用 `try/finally` 保证归还。
- **生命周期**：池在进程启动时建、进程退出靠 OS 回收（demo 不强制 atexit）；多 worker 各自持池、`--reload` 会重建，属预期行为。

### 决策 2：事务边界
- **读接口（orders/logistics/stock）**：无事务需求，PooledDB 层设 `autocommit=True`，读完直接 `close` 归还——避免「不 commit 就还池泄漏未提交读事务 → REPEATABLE READ 下读陈旧快照」。
- **写接口（/refund）**：单步写（只插一条工单），`autocommit` + 单条 INSERT 天然原子，**不需要显式事务**。实现时从「显式事务」简化到此——refund 没有多步写，包事务是过度设计。

### 决策 3：幂等——唯一约束替代「先查后插」
- 当前「先 SELECT 查无重复、再 INSERT」有竞态：两并发都查「无重复」→ 双 INSERT → 双工单。
- **正解**：`refunds` 表加 `UNIQUE(order_id, amount)`，INSERT 撞唯一约束 → 按下面顺序处理。
- **撞唯一键的处理（autocommit 下，比显式事务更简）**：`INSERT` 抛 `IntegrityError` → 直接 `SELECT` 已存在工单 → 返回 `duplicate=True` + 真实 `ticket_id`。autocommit 下每条 SQL 独立事务，撞键后 SELECT 能看到对方已提交的行，**不需要 rollback**（那是显式事务 REPEATABLE READ 下的要求，这里不适用）。
- **refunds 迁移策略**：refunds 是运行时数据，迁移时 **DROP 重建**（清空），CREATE TABLE 直接带唯一约束。**不能依赖 `CREATE TABLE IF NOT EXISTS` 加约束**（表已存在会跳过，约束永远加不上）。

### 决策 4：索引设计
- `orders.order_id`：`VARCHAR(32) PRIMARY KEY` = 聚簇索引 + 唯一（满足「订单号唯一索引」）。
- `logistics.order_id`：建二级索引（`/logistics/{order_id}` 频繁 `WHERE`）。
- `refunds` 的 `UNIQUE(order_id, amount)`：既保幂等，又是复合索引。

### 决策 5：MySQL 环境 = 本地 MySQL（已定）
- 本地装 MySQL 社区版（Windows 服务），国内镜像源，约 30 分钟~1 小时。
- Docker 留到模块 7（WSL2 + 镜像加速两个坑，且 Docker 是模块 7 目标本身）。

### 决策 6：SQLite → MySQL 语法差异（完整核对表）

| 差异 | SQLite | MySQL | 本项目处理 |
|---|---|---|---|
| 占位符 | `?` | `%s` | 改 |
| **TEXT 主键** | 允许 | **报错**（TEXT/BLOB 不能做主键/索引） | **主键列改 VARCHAR(n)** |
| 自增主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `INT AUTO_INCREMENT` | 本项目用业务主键（order_id/ticket_id 非自增），不涉及 |
| 事务/autocommit | 默认自动 | 默认 `autocommit=False` | 读接口设 autocommit=True，写接口显式事务 |
| 日期函数 | `strftime` | `CURRENT_TIMESTAMP`/`DATE_FORMAT` | 本项目时间列是零填充 TEXT 字符串，不涉及，不改 |
| 布尔 | 无真布尔（1/0） | `TINYINT(1)` | 本项目状态是 TEXT，不涉及，不改 |

**主键列改法**：`order_id VARCHAR(32)`、`product_name VARCHAR(64)`、`ticket_id VARCHAR(32)`。

### 决策 7：_init_db 移入 lifespan（解决 import 副作用）
- 现状 `_init_db()` 在模块 import 时顶层执行 → 切 MySQL 后连不上就 import 抛异常、uvicorn 起不来。
- **改**：`_init_db` 移入 FastAPI `lifespan` 启动事件（标准做法），import 时不再执行。
- refresh.py 同步改：不再靠 import 副作用触发，显式调 lifespan 里的建表函数（避免双 DROP）。

### 决策 8：DB 配置来源（.env + python-dotenv）
- `.env` 新增 `MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB`，db.py 用 `python-dotenv` 读（复用 `llm.py` 已有的 `load_dotenv` 模式），不硬编码。

### 决策 9：amount 类型局限（标注，不改）
- 现状：`orders.amount` 是 TEXT「¥89」（含货币符号）、`refunds.amount` 是 REAL。`UNIQUE(order_id, amount)` 建在浮点上。
- demo 整数金额（89.0/32.0）可精确表示，暂不影响；但这是「浮点唯一键」的隐患，生产应改 `DECIMAL(10,2)`。
- **本次不改**（改 DECIMAL 牵动 seed + 评测金额断言，超出模块 2「SQLite→MySQL」范围），标注为已知局限 + 设计要点。

## 六、实施顺序

1. 装本地 MySQL + 建库（前置）
2. `db.py` 连接池封装（.env 配置 + PooledDB + ping）
3. `main.py` DDL 改造（主键 VARCHAR）+ DML 占位符 + 读接口 autocommit
4. `_init_db` 移入 lifespan + refresh 同步改
5. `/refund` 显式事务 + 唯一约束 + rollback 顺序
6. 种子迁入 + `refresh` 跑通（**触发 embedding 批量 API，按 no-auto-generate 需用户明确说「跑」再执行**）
7. 评测回归（后端三端点 + 检索评测）

## 七、验收标准

- [ ] 后端三端点（orders/logistics/stock）MySQL 下返回正常
- [ ] refund 并发双请求只产生一个工单（唯一约束兜底）
- [ ] 连接池无泄漏（跑多轮请求连接数不涨）+ 空闲超时后 ping 能重建连接
- [ ] `EXPLAIN` 确认 logistics.order_id 走索引
- [ ] 评测回归：检索/回答质量不退化

## 八、环境前置（已定：本地 MySQL）

- 开工前装好本地 MySQL 社区版 + 建库，实施第 1 步。
