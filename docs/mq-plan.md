# 模块 4 · MQ 落地方案（退款工单异步化）

> 学习定位：系统式（知识教学见 `/04-mq.md`），落地演示「解耦 / 削峰 / 异步 / 幂等消费」四概念。
> demo 用 Redis list 模拟 MQ（复用模块 3 的 Redis），不引真 Kafka/RabbitMQ。
> 方案经 plan-reviewer 审核，修了「MQ 键被 cache.flush() 误删」「降级回退路径未定义」两个阻塞项。

## 背景

当前 refund 是**同步链路**：`POST /refund` → 校验（订单号格式 / 订单存在）+ 查订单金额（金额下沉：退款金额=订单金额，全额退款，LLM 只传 order_id）→ `INSERT` 工单（待人工审批）→ 返回 ticket_id。

「工单落库」和「通知人工」都在请求线程里同步完成，没有解耦——请求要等「落库 + 通知人工」全部做完才返回。

## 目标

把「工单落库 + 通知人工」从同步链路拆出，走 MQ 异步化。核心是演示四个概念，不是追求真实性能收益（demo 里 refund 本来就快，这是**结构演示**）。

## 数据流

```
agent refund_order 工具
  → POST /refund（同步校验 + 发消息）
  → RPUSH mq:refund_queue（消息 = {message_id, order_id}，不带 amount——金额消费者重查 DB 权威值，防篡改）
  → 立即返回 {status: "已受理", message_id, ticket_id: null, refund_amount}   ← 响应含订单金额（金额下沉）
                          ↓
消费者（daemon 线程，lifespan 启动，BRPOP 阻塞拉取）
  → 幂等检查（message_id 已处理？）→ 跳过
  → 校验 + 查订单金额（复用 _validate_refund，消费者不信任消息，金额不取消息字段）
  → INSERT refunds 工单（金额=refund_amount，UNIQUE(order_id) 兜底）
  → 通知人工（mock：打印日志）
```

## 分层：同步校验 vs 异步处理

| 层 | 内容 | 为什么 |
|----|------|--------|
| 同步（/refund 内） | 订单号格式、订单存在、查订单金额（金额下沉：退款金额=订单金额，全额退款） | 参数非法要**即时反馈**，不能让用户「受理中」后才发现问题；校验是轻操作（查一条订单） |
| 异步（消费者内） | 落库工单 + 通知人工 | 下游操作（通知人工系统、发短信）可能慢，异步化不阻塞退款响应 |

**校验函数抽纯函数**：`_validate_refund(order_id) -> (ok, err_msg, status_code, refund_amount)` 返回结果不抛异常（金额下沉：LLM 只传 order_id，refund_amount 是订单金额权威值）。`/refund` 把它翻译成 `HTTPException`，消费者直接判断 `ok`——避免 `HTTPException`（HTTP 层概念）泄漏进消费者线程。

**消费者不信任消息**：MQ 消息可能来自多生产者 / 被篡改 / 订单状态已变，消费者处理前重新校验（复用 `_validate_refund`，不重复代码）。

## 幂等消费（两层，各拦一类重复）

1. **消息级**：`message_id` 去重。`SET mq:refund:done:{message_id} 1 NX EX 604800`（7 天）原子抢占标记——防**生产者侧重复入队**（/refund 在 RPUSH 超时后重发，同 message_id 入队两次）。
   - ⚠️ 表述修正：Redis list 用 BRPOP 是「至多一次」语义（消息原子弹出、崩溃即丢、无 ack 无重投），不存在「至少一次投递」。message_id 幂等防的是**生产者重复发布**，不是「消费重投」。
2. **业务级**：DB 唯一约束 `UNIQUE(order_id)` 兜底（模块5 升级 + 金额下沉后更明确）。防「同一订单」不同 message_id 的重复（用户重复提交两次退款）——消费者 `INSERT` 撞唯一约束 → 查已有工单 → 跳过。

两层各拦一类重复：消息级拦「同一条消息入队两次」，业务级拦「两个不同消息本质是同一笔退款」。TTL 过期导致消息级去重失效时，业务级兜住，不产生重复工单。

## 降级：MQ 挂了不拖垮退款

- **检测方式**：`RPUSH` 抛 `RedisError` 才回退（实际操作失败才降级），**不做前置 ping**（多一次往返 + 竞态窗口）。
- **回退路径**：复用旧「`INSERT` 工单 + 撞键查已有」整段逻辑，等价旧行为。撞 `UNIQUE(order_id)` → `IntegrityError` → SELECT 已有 → 返回 duplicate，不产生重复工单。
- **统一响应形状（关键）**：两条路径都返回 `{status: "已受理", message_id, ticket_id}`——异步路径 `ticket_id=None`，回退路径带 `ticket_id`。`tools.py` 按 `ticket_id` 是否为空分两套文案，避免解析崩。
- MQ 是「解耦 + 削峰」的手段，不是退款正确性的依赖；挂了降级同步，退款仍然可用。

## 消费者线程（实现约束）

- daemon 线程 + `stop_event` 标志；`BRPOP` 用有限超时（5s），让循环能干净退出（uvicorn --reload 重启）。
- **BRPOP 期间绝不持 DB 连接**：只在处理消息时 `get_conn()`，处理完立即 `close_conn()` 归还——否则消费者长持连接，`maxconnections=5` 被占满，请求线程卡死。
- **catch 所有 RedisError/TimeoutError**：Redis 挂时消费者循环不能死，`except RedisError` → 记日志 → `sleep(1)` 重试。`socket_timeout=1` 可能让 BRPOP 周期性抛 TimeoutError，必须当正常迭代吞掉。

## 改动文件

- **新增 `src/backend/mq.py`**（`# -*- coding: utf-8 -*-` 头 + stdout reconfigure）：`publish_refund()` + `consume_loop()` + 幂等标记
- **改 `src/backend/main.py`**：抽 `_validate_refund` 纯函数；`/refund` 改成校验 → 发消息 → 返回统一形状；`lifespan` 启动消费者线程
- **改 `src/tools.py`**：`refund_order` 文案改异步语义，按 `ticket_id` 是否为空分两套；删除 `duplicate` 分支（去重判断移到消费者）
- **同步 docs**：`docs/backend-pitfalls.md`、`docs/architecture.md`、`docs/mysql-migration.md` 里描述同步 refund 语义的过时事实

## 要点映射

| 概念 | 落地 |
|------|------|
| 解耦 | 退款请求（生产者）和工单处理（消费者）经 MQ 解耦 |
| 削峰 | 高峰退款进队列（Redis list），消费者按自己速度消费 |
| 异步 | 落库 + 通知人工不阻塞退款响应 |
| 幂等消费 | message_id 去重 + DB 唯一约束双层 |
| 降级 | Redis 挂 → 回退同步 |
| MQ 代价 | 异步引入「受理 ≠ 完成」的一致性窗口 + 延迟 + 复杂度；消费者校验失败只能「记日志 + 丢弃」，无拒绝回执 |
| Redis 做 MQ 的局限 | 不持久化（重启丢消息）、无可靠 ack、无重试、至多一次语义——这正是「真 MQ 要 Kafka/RabbitMQ」的对照 |

## 边界（不做）

- 不引真 Kafka/RabbitMQ（Redis list 模拟，说明局限即可）
- 不做「工单查询接口」（异步后用户凭 order_id 查 refunds 表；认知到位，测试直查 MySQL）
- `transfer_to_human` 保持 mock，不异步化（聚焦 refund 一个代表，同构不引新要点）
- 不处理「消息积压报警」「死信队列」（认知到位：消费失败重试 N 次进死信）

## 验证

- 起后端 → POST /refund → 返回「已受理」→ 消费者消费 → refunds 表生成工单（直查 MySQL 确认）
- 幂等：同 order_id 重复 POST（不同 message_id）→ 只 1 张工单
- 降级：停 Redis → POST /refund → 回退同步 → 工单直接生成
