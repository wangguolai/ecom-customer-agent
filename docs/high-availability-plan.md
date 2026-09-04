# 模块 5 · 高可用 落地方案（plan-reviewer 审核后修订版）

> 定位：🔴 核心，系统设计环节的关键。限流 / 熔断 / 降级 / 幂等四件套。
> 落地三块：① 后端接口限流 ② agent 侧熔断 ③ 退款幂等系统化（状态机）
> 本版吸收 plan-reviewer 审核结论（🔴1 阻塞 / 🟡1-7 建议 / 🟢1-8 补充），核心变更见「五、审核后修订记录」。

## 一、背景与现状

| 现状 | 问题 | 对应方案 |
|------|------|-------------|
| `tools.py` 有 `REQUEST_TIMEOUT=2.0`，catch ConnectError/Timeout → 返回友好错误 | 每次请求都打后端，后端连续挂 100 次也照样等 2s——**只有超时，没有熔断** | 熔断三态 |
| 后端接口（main.py）无限流 | 任何客户端可无限刷接口 | 限流三算法 |
| `refunds.status` 只有「待人工审批」一态，插进去就死 | **工单状态机缺失**（backend-pitfalls 坑 3 已踩实） | 幂等系统化 |
| 幂等已有两层：message_id 去重 + `UNIQUE(order_id, amount)` | 拦「同订单同金额」，拦不住「同订单退 50 又退 80」（金额不同） | 状态机幂等 |

## 二、块 1：后端接口限流（滑动窗口）

### 设计

- **算法选滑动窗口**（Redis ZSET），不用令牌桶。理由（plan-reviewer 修正后）：
  1. **令牌桶无法对任意墙钟窗口给出严格上限**——突发 + 期间补的令牌，一个窗口最多放进「桶容量 + 速率×窗口」两倍量；滑动窗口可以对任意时间窗严格封顶。客服查询场景要的是「封顶」不是「突发」，滑动窗口更贴合
  2. 令牌桶的「允许突发」价值在秒杀开抢瞬间洪峰，客服查询不适用
  - **规范**：令牌桶 vs 漏桶 vs 滑动窗口三算法区别要会讲（令牌桶允许突发 / 漏桶强制匀速 / 滑动窗口精确封顶），落地选滑动窗口的理由是「客服要封顶不要突发」
- **读写分离限流**：读接口（订单/物流/库存/商品）共享一个松桶，写接口（退款 + 审批/执行）单独一个严桶——与 `tools.py` 已有 `WRITE_TOOLS` 读写分离呼应
- **桶粒度（plan-reviewer 🟡4）**：demo 用固定 client 标识（单客户端），`bucket_key = f"rl:{bucket}:{client}"`；规范「生产用 per-IP + 全局双层桶，全局挡总量、per-IP 挡单客户端刷」。**注意：全局桶挡不住单客户端刷，防刷靠 per-IP 粒度，不是算法**
- **Redis 前缀 `rl:`**：与 `ecom:`（业务缓存，flush 清）、`mq:`（MQ 队列）隔离，`cache.flush()` 只清 `ecom:*`，不误删限流计数
- **原子性（plan-reviewer 修正 🟡5/🔴 表述）**：`ZREMRANGEBYSCORE`（删窗口外）+ `ZCARD`（数）用 pipeline 只减少 RTT + 保证这两步原子；但「数 → Python 判断 → 决定是否 ZADD」的**决策在 Python 侧，check-then-add 竞态是固有的**，pipeline 消不掉。demo 接受（并发 k 最多超 k-1），**严格原子要 Lua 脚本**。ZADD 的 member 必须每次唯一（`f"{now}-{uuid4()}"`），不能用 `str(time.time())`（同毫秒覆盖 → 少计数 → 限流失效）
- **fail-open（plan-reviewer 🟡3）**：Redis 不可用时 `rate_limit` 捕获 RedisError **放行**（记录日志），不抛异常——限流是「保护」不是「正确性」，挂了宁可放行不能全挂（对齐 `cache.py` 旁路降级哲学）
- **key 过期（plan-reviewer 🟡5）**：每次 ZADD 后 `EXPIRE key = window + 缓冲`，防 `rl:` 键无界增长（它逃过了 `flush()` 的 `ecom:*` 清理）

### 落点

- 新文件 `src/backend/ratelimit.py`：`rate_limit(bucket, client, window, max_req) -> bool`
- `main.py`：`Depends` 依赖注入，读接口挂 `rl:read`（如 100 次/10s），写接口挂 `rl:write`（如 10 次/10s，退款 + 审批 + 执行都挂）
- 超限返回 `HTTPException(429, "请求过于频繁，请稍后重试")`

## 三、块 2：agent 侧熔断（三态状态机）

### 设计

- **三态**：`closed`（正常）→ `open`（连续失败达阈值，快速失败）→ `half_open`（冷却后放一个试探）→ 成功回 `closed` / 失败回 `open`
- **失败判定（plan-reviewer 修正后）**：
  - 算失败：`TransportError` 全覆盖（Connect / Timeout / **RemoteProtocol 后端中途挂**）/ HTTP **5xx** / **「200 但 JSON 解析失败」**（对应 backend-pitfalls 坑 7「异常处理不完整」）。「200 缺关键字段」由各工具 `d['xxx']` 的 KeyError 触发、被 agent 捕成「工具参数不匹配」——demo 边界，生产做 schema 校验
  - **不算失败且 `record_success()`**：HTTP 4xx（400 参数非法 / 404 不存在 / 409 状态机冲突 / 429 限流）——4xx 是「后端起了、正常拒绝请求」，语义上后端健康，应**重置连续失败计数**（不是 no-op 不计数，否则「失败失败 404 失败」会因 404 打断连续计数导致熔断不触发）
  - 429 尤其要排除：agent 被限流是自己的错，不是后端挂了
- **熔断 vs 降级**：熔断=「不调坏的下游」（保护自己，快速失败），降级=「给用户兜底结果」（返回友好提示）。熔断打开 → 工具返回「服务暂不可用」降级话术
- **原子性（plan-reviewer 修正 🟡/🟢）**：`allow()/record_*()` 状态转换**无锁**，安全仅因为**当前所有 HTTP 工具都是纯 async、走 `await` 在事件循环里执行、不进 to_thread**（`search_products` 才走 to_thread）。`allow()` 同步无 await → 状态机转换原子。**「复用 `_get_hybrid_retriever` 的双重检查锁」是误导**——DCL 只保护懒加载初始化，不保护熔断状态转换；如果将来把 HTTP 工具丢进 to_thread，无锁假设立刻崩。多进程部署（web 托管）下每个进程一个熔断器，半开单飞退化为多探针——demo 单进程没问题，方案注明此边界
- **计时用 `time.monotonic()`**（单调时钟，不受系统时间调整影响）
- **进程级单例**：demo 单后端一个全局熔断器；生产按下游服务粒度分（订单/物流/库存各一个）
- **连续失败 vs 滑动窗口错误率（plan-reviewer 🟡6）**：单接口持续 500（退款逻辑 bug）而其他接口正常时，成功调用会重置连续计数 → 熔断永不触发。这是「整体挂」场景的简化模型，规范准备「生产用时间窗口错误率（Hystrix 式）而非连续失败计数」——这是要点

### 落点

- 新文件 `src/circuit_breaker.py`：`CircuitBreaker(fail_threshold=5, cooldown=30)`，方法 `allow()/record_success()/record_failure()`
- `tools.py`：抽 `_http_request(method, url, json=None)` 帮助函数——**返回 `(resp_or_none, error)` 对象 + 完成熔断记账，话术留在各工具**（退款工具有丰富的 400 detail 透传 / ticket_id/message_id 两套话术，helper 不能返回字符串吃掉这些）
- 熔断打开时返回「服务暂不可用（当前熔断中，请稍后重试或转人工）」，与超时话术区分；429 返回「请求过于频繁，请稍后再试」
- `search_products` 本地 RAG 不走后端，不套熔断

## 四、块 3：退款幂等系统化（状态机）

### 核心变更：`UNIQUE(order_id, amount)` → `UNIQUE(order_id)`（DB 兜底，不用「先查后插」）

plan-reviewer 🔴1 抓出：方案原写「`_validate_refund` 查该订单是否有非 rejected 工单，有则拒绝」是 **check-then-act，无 DB 约束兜底**——两个不同金额的并发退款（退 50 + 退 80）都先查库都通过，都插入 → 同订单两条进行中工单，「一个订单一个工单」不变量被打破。这正是模块 2 自己记的坑。

**解法**：把唯一约束从 `UNIQUE(order_id, amount)` 改成 `UNIQUE(order_id)`——「一个订单一个工单」由数据库硬兜底，幂等从「先查后插」升级成「撞键兜底」，和项目哲学一致：

- 同订单任何金额二次退款 → 撞 `UNIQUE(order_id)` → 工单层去重（不管金额）→ 同时解决「退 50 又退 80」的洞。异步路径下 `/refund` 仍返回「已受理」，撞键去重发生在消费者落库——「受理 ≠ 完成」的又一个实例，可讲
- 并发不同金额 → 撞键兜底，并发双请求只产生 1 工单（DB 硬保证）
- 「已退款不能再退」→ refunded 工单占着 order_id，二次退款撞键
- 消费者处理顺序两次不同金额退款 → 第二个撞键走「duplicate 查已有」路径，**不静默丢弃**（`_create_refund_ticket` 已有撞键 → 查已有 → `duplicate=True` 逻辑，复用）

**代价（demo 可接受的简化，诚实讲）**：
1. **不支持部分退款**——一个订单一个工单记录（生产按商品/金额维度拆分 + 订单状态机联动）
2. **rejected 后同金额不能重提**——rejected 工单占着 order_id（生产要「rejected 后可重提」，需生成列部分唯一索引或 active_refund 表）

### 设计

- **4 状态**：`待人工审批(pending)` → `已批准(approved)` → `已退款(refunded)`（终态）；`待人工审批` → `已拒绝(rejected)`（终态）
- **状态流转由代码层驱动，不由 LLM**：新增审批接口（人工/代码驱动），LLM 只能生成「待审批」工单
- **流转落库用条件 UPDATE（plan-reviewer 🟡1）**：`UPDATE refunds SET status=新 WHERE ticket_id=? AND status=旧`，affected rows=0 → 409。**不能**「读 status → Python 判断 → 普通 UPDATE」（两个并发审批 approve+reject 都读 pending 都放行 → last-writer-wins，409 保证失效）。这正好是幂等 4 实现之一的「乐观锁版本号」要点
- **状态机常量放 `mq.py`（退款域），db.py 不动**（plan-reviewer 🟢8：db.py 是连接池基础设施，退款状态机是业务域，放 mq.py 避免常量两边重复）

### 落点

- `mq.py`：状态常量 + `_transition()` 流转函数 + `_create_refund_ticket` 撞键逻辑适配 `UNIQUE(order_id)`（撞键查 `WHERE order_id=?` 不再查 amount）
- `main.py`：`POST /refund/{ticket_id}/review`（action=approve/reject）+ `POST /refund/{ticket_id}/execute`（approved→refunded，mock）
- `db.py` 的 `_init_db`：refunds 表 `UNIQUE(order_id, amount)` → `UNIQUE(order_id)`，且 schema 变更需 DROP 重建 refunds（demo 历史工单可清，和 seed 表一致；不再「IF NOT EXISTS 保留工单」）
- **审批/执行接口无鉴权是新增攻击面（plan-reviewer 🟢3）**：资金敏感操作裸奔成无鉴权 HTTP，比「只能生成待审批工单」风险高一级。规范「demo 可接受，生产必须鉴权 + IP 白名单」
- **execute 后订单状态不联动（plan-reviewer 🟢6）**：refunded 后 `orders.status` 不变（mock 退款执行），规范「生产退款到账要联动订单状态」

## 五、审核后修订记录（plan-reviewer 抓出的关键变更）

| # | 原方案 | 审核结论 | 修订 |
|---|--------|---------|------|
| 🔴1 | 状态机幂等「先查后插」 | 并发竞态，倒退到模块 2 自己记的坑 | `UNIQUE(order_id, amount)` → `UNIQUE(order_id)`，DB 兜底 |
| 🔴2 | 异步消费者校验失败静默丢弃 | 顺序两次不同金额都受理，第二个被丢弃 | 改撞键后走 duplicate 路径，不丢弃 |
| 🟡1 | `_transition` 纯函数 + 普通 UPDATE | 并发审批 last-writer-wins | 条件 UPDATE（WHERE status=旧）+ affected rows |
| 🟡2 | 状态机「非 rejected 才拦」 | 与 UNIQUE 冲突，rejected 后同金额重提被挡 | 并入 UNIQUE(order_id) 语义，明确「rejected 不能重提」为 demo 代价 |
| 🟡3 | 限流 Redis 挂未定义 | 可能 500 比不限流更糟 | fail-open（放行 + 记日志） |
| 🟡4 | 桶粒度未定义 | 全局桶挡不住单客户端刷 | demo 固定 client，讲 per-IP + 全局双层 |
| 🟡5 | 「pipeline 保证原子」表述自相矛盾 | check-then-add 竞态固有，pipeline 消不掉 | 改表述 + 补 key EXPIRE + ZADD member 唯一性 |
| 🟡6 | 连续失败计数 | 部分降级下成功调用重置计数 | 标注简化模型，讲错误率法 |
| 🟡7 | `_http_request` 契约未定义 | 退款工具话术会被吃掉 | helper 返回 resp 对象，话术留各工具 |
| 🟢1 | 429 未处理 | agent 会 429 重试浪费轮次 | 429 返回友好话术，不算熔断失败 |
| 🟢2 | 熔断 vs cache 降级关系 | 后端 DB 挂时读接口可缓存兜底 | 强加分点，记要点 |
| 🟢7 | 测试未提时钟注入 | cooldown/window 等真实时间 | 测试注入时钟 / 参数可配置 |
| 🟢8 | 状态常量 db.py + mq.py 重复 | 常量两边都有 | 全放 mq.py |

## 六、文件清单

| 文件 | 动作 |
|------|------|
| `src/backend/ratelimit.py` | 新增：滑动窗口限流 |
| `src/circuit_breaker.py` | 新增：三态熔断器 |
| `src/tools.py` | 改：抽 `_http_request` 帮助函数 + 套熔断 |
| `src/backend/main.py` | 改：接口挂限流依赖 + 审批/执行接口 |
| `src/backend/mq.py` | 改：状态常量 + `_transition` + 撞键适配 `UNIQUE(order_id)` |
| `tests/test_ratelimit.py` | 新增：限流测试（常驻回归，对齐 test_mq.py） |
| `tests/test_circuit_breaker.py` | 新增：熔断测试（常驻回归） |
| `tests/test_refund_state_machine.py` | 新增：状态机测试（常驻回归） |

## 七、测试验证

1. 限流：连发 N 次超阈值 → 429；窗口滑动后恢复放行（注入时钟，不真等）
2. 熔断：模拟后端连续失败 5 次 → 第 6 次快速失败（不真调）；冷却后半开试探成功 → 恢复（注入时钟）
3. 状态机：pending→approved→refunded 正常；非法流转（refunded→approved）返回 409（条件 UPDATE affected rows=0）；同订单不同金额二次退款撞 `UNIQUE(order_id)` 拒绝

## 八、要点

- 限流三算法 tradeoff（令牌桶允许突发 / 漏桶强制匀速 / 滑动窗口精确封顶），为什么客服场景选滑动窗口（令牌桶无法严格封顶）
- 熔断三态 + 熔断 vs 降级区别 + 「4xx 不算失败、5xx 算、200 垃圾响应也算」的边界
- 幂等 4 种实现（唯一索引 / 乐观锁 / 状态机 / token），本项目落地「唯一约束 UNIQUE(order_id) + 条件 UPDATE 乐观锁」两层
- 服务雪崩四件套（超时 / 熔断 / 降级 / 限流）+ 服务隔离
- 连续失败计数 vs 时间窗口错误率（生产 Hystrix 式）
- 熔断 vs 后端 cache 降级：后端 DB 挂时读接口可降级用缓存旧数据（熔断保护自己 vs 降级继续服务）
- 限流原子性：Redis check-then-add 竞态，严格要 Lua；限流 fail-open

## 九、方向性偏离（已确认 2026-08-26）

1. **令牌桶 → 滑动窗口**：✅ 已确认。理由——令牌桶无法严格封顶（突发 = 桶容量 + 速率×窗口两倍量），客服要封顶不要突发。
2. **砍掉重试**：✅ 已确认。只做超时（已有）+ 熔断，不做独立重试层。理由——写接口不能乱重试，读接口 demo 抖动意义不大，熔断半开探测本质是受控重试。
