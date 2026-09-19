# 伪菜单走规则路由 + 压缩落回会话层（2026-09-20）

> 起因：用户实测「点伪菜单反应都很慢」，问是不是冷启动。
> **不是冷启动**——同一会话各轮 3.9s~14.0s 没有衰减曲线，且后端已服务 6 轮。
> 真因是两条结构性叠加，本批各修一条。

---

## 0. 现状（已核实，非推测）

### 6 个菜单项里 4 个掉进 LLM 决策

| 菜单 | 发送文本 | `route_by_rule` | 走法 |
|---|---|---|---|
| 📦 查订单 | `查订单` | `None` | 🔴 LLM |
| 🚚 查物流 | `查物流` | `None` | 🔴 LLM |
| 🏷️ 查库存 | `查库存` | `None` | 🔴 LLM |
| 🐾 商品咨询 | `我想咨询一下商品` | `None` | 🔴 LLM |
| 📋 退货政策 | `退货政策` | `get_return_policy` | ✅ 规则 |
| 🙋 转人工 | `我要转人工` | `transfer_to_human` | ✅ 规则 |

根因：`intent_router` 的订单分支要求「**订单号 + 关键词**」**双命中**才路由（`:103-110`），
菜单项只有关键词、没有参数 → 拦不住。

### 菜单点击的完整链路（实测 trace 佐证）

```
前端 onPick(item) → item.text 当**普通消息**发（不带菜单标记）
  ↓ session_store.load → AgentSession
  ↓ route_by_rule("查订单") → None
  ↓ _react_loop_stream:
      step1  LLM 决策 → 不调任何工具，直接反问
      step2  摘要调用（history 2772 token > MAX_HISTORY_TOKENS 2000）
  → 2 次 LLM 往返 = 6.479s，tool_calls=[]
```

---

## 1. 任务一：菜单走规则路由 + 从会话上下文补参数

### 1.1 目标行为（用户原话）

> 「伪菜单无非两条路：一条是**已经有订单号或必备信息了直接去找**；另一类是**固定返回，
> 没有订单号、去上下文找，也没有再问，有的话直接搜索返回**才对。」

```
菜单点击
  ├─ ① 本轮消息里有必需参数 → 直接执行工具
  └─ ② 没有 → 去会话历史找最近的参数
        ├─ 找到 → 直接执行工具（沿用上下文里那个）
        └─ 没找到 → **固定反问话术**（零 LLM）
```

### 1.2 安全约束（**本设计的核心风险点**）

菜单意图是**客户端传来的**，与 `session_id` 同级——**都是不可信输入**。

- ❌ **绝不让客户端传工具名**（`search_orders`）——那等于开放「任意工具调用」入口，
  配合 §1.3 的「从上下文补参数」可以直接构造「拿别人的订单号去查」。
- ✅ 客户端只传**菜单 id**（`orders`），后端用**白名单表**映射到工具。
  白名单外一律当普通消息处理（掉 LLM 兜底），不报错、不降级成默认意图。

```python
# rules.py
MENU_INTENTS = {
    "orders":    ("search_orders",     "order"),
    "logistics": ("search_logistics",  "order"),
    "stock":     ("check_stock",       "product"),
    "policy":    ("get_return_policy", None),
    "products":  (None, None),          # 商品咨询走 RAG，无必需参数
    "human":     ("transfer_to_human", None),
}
```

### 1.3 参数解析：从上下文补

新增「最近参数」提取：**从后往前扫会话历史**，找最近出现的订单号 / 商品 ID。

```python
def _find_recent_order_id(history) -> str | None:
    """从会话历史里找最近提到的订单号（先看 user 消息，再看 assistant）。"""
```

⚠️ 只扫**会话历史**（本 session 的），不跨 session——作用域由 `session_id` 决定，
与「画像作用域从认证上下文推导」同源。

### 1.4 零 LLM 的固定反问

缺参数时**不走 LLM**，直接 yield 固定文案（如「请把订单号发我～」）。

**为什么这里可以用模板**：菜单是**确定性入口**，缺参提示也是确定性的。
「表达交模型」针对的是**业务话术生成**（推荐商品、解释政策），不是「请提供订单号」。
省下来的正好是那 6.5 秒里的大头。

### 1.5 改动清单

| 文件 | 改什么 |
|---|---|
| `src/config/rules.py` | 新增 `MENU_INTENTS` 白名单 + 缺参反问文案 |
| `src/intent_router.py` | 新增 `route_by_menu(intent, user_msg, history)` |
| `src/agent.py` | `stream_chat` 接受菜单意图；新增「有参执行 / 无参反问」分支 |
| `src/backend/main.py` | 从 payload 读 `menu_intent`，白名单校验后传给 session |
| `frontend/src/menuItems.ts` | 每一项加 `intent` id |
| `frontend/src/components/Menu.tsx` / `App.tsx` / `useChatStream.ts` | 把 `intent` 带进请求体 |

---

## 2. 任务二：压缩结果落回会话层

### 2.1 现状：压缩从来没生效过（第二轮）

```
每轮：session_store.load() 读完整历史 → AgentSession 在**内存里**压缩
      → 请求结束，压缩结果随 session 对象丢弃
      → session_store.save(session_id, **history**, user_msg, answer)  ← 存的是原始 history
→ 历史单调增长 2,4,6…20 条 → 一旦超 2000 token，**每轮都重新压一遍**
```

实测：会话历史 20 条 → `_messages_tokens` = 2772 > `MAX_HISTORY_TOKENS` 2000 →
**每轮多付一次 LLM 往返**。摘要的钱花了两遍、收益一遍没拿到。

### 2.2 为什么当时这么写（**不是写错，是取舍没做完**）

`session_store` 刻意存 `history + 本轮问答` 而不是 `session.messages`，因为
`session.messages` 里混着**不能跨轮保存**的东西：

| 混进来的 | 为什么不能存 |
|---|---|
| 画像注入（`MEMORY_INJECT_PREFIX`） | 每轮会重新注入（`_strip_memory_injection` 清旧的），存了会累积 |
| 规则路由的引导消息（`【路由引导：`） | **内部指令**，喂进下一轮等于自己给自己注入 |
| `【规则路由已执行工具…】` | 同上，且工具结果是时点数据 |
| `tool` 角色 / 带 `tool_calls` 的 assistant | ReAct 中间态，跨轮无价值 |

但**摘要消息（`SUMMARY_PREFIX`）必须保留**——它正是压缩的产物，丢了压缩就等于没做。

### 2.3 修法

`AgentSession` 新增导出方法：

```python
def session_history(self) -> list[dict]:
    """导出可持久化的干净历史：user/assistant 文本对 + **摘要消息**，
    排除画像注入 / 路由引导 / 工具回填 / tool_calls。"""
```

`session_store.save` 改成收**组装好的**干净历史（调用方负责含本轮问答），
`main.py` 传 `session.session_history()`。

**判据（怎么知道修对了）**：连续聊 10 轮后，Redis 里的历史条数**不再单调增长**，
且第二轮起 `摘要调用次数` 应显著下降（历史已被压到预算内，不再每轮触发）。

---

## 3. 本批**不做**

- **`MAX_HISTORY_TOKENS` 调大**（用户明确跳过）——先看修完 §2 后触发频率降到多少，用数据说话。
- **`REASONING_EFFORT` 调低**——挂起决策，评测未跑，独立一条（见 TODO.md）。
- **手打的裸意图词也走规则路由**——菜单点击是**确定性**入口，可以零误判；
  手打「查订单」则是概率性输入，放宽会重演今天刚修的「裸词误伤」（`比较`）。
  两条路径风险量级不同，**不合并**。

---

## 4. 验证方式

| 层 | 验证 | 花钱 |
|---|---|---|
| 菜单路由 | 单测：6 个菜单 id 各断言（白名单内→正确工具；白名单外→None） | 否 |
| 上下文补参 | 单测：历史里有订单号/没有，两种断言 | 否 |
| 安全 | 单测：客户端传 `search_orders`（工具名而非菜单 id）→ 必须被拒 | 否 |
| 会话压缩 | 单测：10 轮后 `session_history()` 长度不单调增长、含 `SUMMARY_PREFIX` | 否 |
| 端到端 | 点「查订单」实测耗时（目标：有参 ≈ 1 次 LLM；无参 < 1s） | 是（少） |
| 回归 | `test_stream` / `test_reasoning_and_leak` / `test_memory` / `test_feedback` 全绿 | 否 |
