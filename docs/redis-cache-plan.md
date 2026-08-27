# 模块 3 · Redis 落地方案 v2（缓存层 Cache Aside）

> 学习定位：系统式（知识见 `/03-redis.md`）。落地踩坑式。
> v2：吸收 plan-reviewer 审核（2 阻塞 + 5 重要 + 5 建议已闭环）。

## 一、目标

backend 加 Redis 缓存层：订单/物流/库存**三个只读接口**走 Cache Aside 读路径，减轻 MySQL 读压力。缓存是**旁路、可重建副本**，不是第二真相。

## 二、现状

- backend 已 MySQL 化，`get_order`/`get_logistics`/`get_product` 直接查 MySQL，无缓存层。
- 三个接口都是**只读**（seed 数据，HTTP 层无更新 orders/stock/logistics 的写接口）。
- **但 `refresh.py` 会 DROP 重建真源**（seed 是唯一源）——这是「写」的真实来源，缓存副本要联动失效（见决策 5）。

## 三、技术选型：redis-py（同步 + 进程级单例连接池）

| 选项 | 结论 | 理由 |
|---|---|---|
| **redis-py**（同步） | ✅ | backend 同步 `def` 端点（线程池），和 pymysql 同逻辑；redis-py 内置连接池 |
| aioredis / redis.asyncio | ❌ | backend 保持同步 |

**连接池**：进程级单例 `Redis(host=..., port=..., decode_responses=True)`，模块级 `_redis` 单例（类似 `db.py` 的 `_pool`），不每请求重建。`decode_responses=True`（否则返回 bytes，json.loads 踩编码坑）。

**RESP3 兼容坑（实现时踩）**：redis-py 8.x 默认 RESP3（连接时发 `HELLO 3`），但 tporadowski/redis 是 5.x 不支持 RESP3 → `unknown command HELLO`。解法：构造加 `protocol=2` 强制 RESP2。requirements 锁 `redis>=8,<9`（保 8.x 行为一致）。这是「新版客户端 vs 旧版服务端」的兼容坑，未来模块 4/5/6 复用同一 Redis 的读者别重踩。

**Qdrant 类比**：Redis 是独立网络进程、连接池线程安全，**无 Qdrant 本地模式的 `AlreadyLocked` 文件锁坑**，不需要 `_get_hybrid_retriever` 那种双重检查锁——这是可讲的「Redis vs Qdrant 本地模式」区别。

## 四、改动清单

| 文件 | 改动 |
|---|---|
| `requirements.txt` | 加 `redis` |
| `.env` | 加 `REDIS_HOST=127.0.0.1`、`REDIS_PORT=6379`（tporadowski 默认无密码） |
| `src/backend/cache.py`(新) | UTF-8 头 + stdout reconfigure + load_dotenv 读 .env；进程级单例连接池；`get(key)`/`set(key, value, ttl)`（统一 JSON）+ 空值哨兵 + 降级 |
| `src/backend/main.py` | `get_order`/`get_logistics`/`get_product` 加 Cache Aside 读路径 + 缓存空值 |
| `src/refresh.py` | 加 `FLUSHDB`——重建真源时清空缓存副本（SSOT 单向链路闭环） |

## 五、关键设计决策

### 决策 1：数据结构 + key 命名 + 序列化规范（统一 JSON）
- **统一 JSON 序列化**：订单存 `{...订单字段}`、物流存 `[...轨迹]`、库存存 `{"qty": 120}`（**不存裸值**，消除「叫 json 却存裸数字」的歧义）。
- **key 命名带命名空间**：`ecom:order:{id}` / `ecom:logistics:{id}` / `ecom:product:{product_id}`（商品统一 ID 后 stock→products、name→product_id）——未来模块 4（MQ 用 list）/5（限流）/6（分布式锁）复用同一 Redis，前缀防冲突。

### 决策 2：Cache Aside 读路径
- **读**：先 `GET` 缓存 → 命中返回；miss → 查 MySQL → `SET`（带 TTL）→ 返回。
- **写后删缓存**：HTTP 层无更新 orders/stock/logistics 的写接口，认知不落地（说明「先更 DB 再删缓存 + TTL 兜底」）。

### 决策 3：空值哨兵（防穿透，关键——区分「缺货」和「不存在」）
- `get(key)` 返回三元语义：**None = miss（未命中）**、**`__EMPTY__` = 命中空标记（不存在）**、**JSON 对象 = 命中真实数据**。
- **豆腐猫砂 `qty=0` 是合法真实数据（「缺货」），必须缓存 `{"qty": 0}`，绝不能当空标记**——否则 qty=0 会被误判成「不存在」反复穿透查库。
- `get_order` 查不到订单（404）→ 缓存 `__EMPTY__`（短 TTL 30s），第二次同样请求挡在缓存层。
- TTL：订单/物流 `EX 60`、库存 `EX 30`、空标记 `EX 30`。

### 决策 4：Redis 降级（缓存是旁路，不成为单点）
- **`get` 对 Redis 异常（`RedisError`，含 ConnectionError + TimeoutError）吞掉返回 None（视为 miss）**→ 请求回落到 MySQL，正确性不受影响。
- **超时必须设**：构造加 `socket_connect_timeout=0.5, socket_timeout=1`——redis-py 默认 `socket_timeout=None`（无限阻塞），Redis 半开连接/网络分区会抛 TimeoutError 而不是 ConnectionError，只捕后者会让超时冒出去挂死线程池。缓存是旁路，超时降级查库，不能拖垮真源。
- **⚠️ 必须关默认重试（2026-08-24 实测坑）**：redis-py 8.1 的 `Redis()` 默认 `retry=Retry(ExponentialWithJitterBackoff(), 10)`——连接失败会退避重试 10 次。实测 Redis 停止时每次操作 ~9s（0.5s connect 超时 × 10 + 指数退避），比无限阻塞还隐蔽。构造加 `retry=None` 关闭，连接失败 0.5s 即抛 RedisError 降级查库；Redis 恢复后连接池自会建新连接，不需要重试。
- **`set` 失败只记日志、不抛**（含空标记写入）。
- 启动时 Redis 连接失败不阻塞服务（lifespan 里 Redis ping 非致命探活）。
- **验收**：停掉 Redis 后，三端点仍返回正确数据（降级查库）。

### 决策 5：refresh 联动（SSOT 单向链路闭环）
- `refresh.py` 加 `FLUSHDB`（或按 `ecom:*` 前缀删）——refresh 是「重写真源」的真实写场景，缓存副本必须同步失效，否则最长 60s 返回旧数据。
- 这补上了 `data-sync-architecture.md` 的派生层清单缺口：**Redis 和向量库、MySQL 一样，是可重建派生副本，纳入 refresh 的单向重建链路**。

### 决策 6：与「数据分治」架构的关系（规范）
- **Redis = MySQL 的可重建副本，真源仍是 MySQL**，丢了可查库重建，永不背真相。和向量库同一性质，与「数据分治」（源库=唯一真相、派生=可重建副本）要点直接挂钩。
- 这就是 TODO「缓存一致性深度：缓存副本 vs 真源不一致」要踩的坑的落地。

### 决策 7：认知为主，不落地
- **分布式锁**：退款幂等已在模块 2 用 DB `UNIQUE(order_id, amount)` 唯一约束实现、模块 5 升级 `UNIQUE(order_id)`（单机单进程内是正解），分布式锁只在「多实例部署」下才有意义，demo 无多实例 → 认知不落地。
- **击穿**：agent 侧 `asyncio.gather` 并发查同一 key（用户同时问订单+物流）是「击穿微缩场景」——demo 量小 MySQL 扛得住，不落地互斥锁，但场景真实存在（可观测点）。
- **雪崩**：过期随机打散，认知。

## 六、实施顺序

1. `pip install redis`
2. `cache.py` 连接池单例 + get/set + 空值哨兵 + 降级
3. `main.py` 三接口加 Cache Aside 读路径 + 缓存空值
4. `refresh.py` 加 FLUSHDB
5. 回归验证（起 Redis + 三端点 + 缓存命中 + 停 Redis 降级）

## 七、验收标准

- [ ] 三端点 Redis 下返回正常
- [ ] 首次 `get_order` miss 查 MySQL + 写缓存，第二次命中直接返回（不查 MySQL）
- [ ] 缓存有 TTL（`TTL ecom:order:{id}` 返回剩余秒数）
- [ ] **豆腐猫砂 `get_product` 缓存 `{"qty": 0}`（真实缺货），不是空标记**（商品统一 ID 后 stock→products）
- [ ] 404 订单缓存 `__EMPTY__`，第二次同样请求不查 MySQL
- [ ] **停掉 Redis 后三端点仍返回正确数据（降级查库）**
- [ ] refresh 后缓存被 FLUSH（不返回旧数据）
- [ ] 缓存一致性/击穿/雪崩/分布式锁能口述「为什么」+「各自解法」

## 八、环境前置（已定：tporadowski/redis 5.x）

- Redis 已装（用户确认 `redis-server.exe` 可跑，默认 6379 无密码）。
