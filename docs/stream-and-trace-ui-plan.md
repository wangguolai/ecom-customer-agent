# 规则路由流式化 + 执行步骤可视化

> 状态：阶段 1 设计稿（待 plan-reviewer 审核）
> 来源：2026-09-18 用户实测「最后一次查的很慢」+ 用户新需求「左侧栏同步展示 trace 链路，表示目前到哪了、第几步」

---

## 〇、问题与目标

### 问题 1：规则路由路径**不是流式的**（用户实测的「慢」）

`src/agent.py` 的 `stream_chat`：
```python
if routed:
    result = await _run_routed(...)   # 完整生成整段
    yield result                       # ← 一次性吐出
    return
async for delta in _react_loop_stream(...):   # 只有 ReAct 路径逐 token
```

**实测证据**（后端 trace + 路由实测）：
| 路径 | trace | 用户看到 |
|---|---|---|
| 规则路由（查订单） | `耗时=3.988s LLM=1次 工具=1次` | **沉默 4 秒 → 整段出现** |
| ReAct 兜底 | `耗时=4.54s LLM=1次 工具=0次` | 约 1 秒后**逐字冒出** |

两条路径**总耗时接近**，差的只是「有没有中间反馈」。用户体感的「慢」是**没有流式**，不是真的更慢。
（代码注释自己写着「规则路由话术流式列为后续」——这是已登记的待做项。）

### 需求 2：执行步骤可视化（用户新提）

> 「trace 链路在左侧的栏目中也同步展示出来，表示我目前到哪了，第几步」

现状：`Trace` 只在**服务端控制台**打印（`print(trace)`），用户看不到执行过程。
目标：把「第几步、在做什么」**实时推到前端左侧栏**。

**面试价值**：这是把项目已有的 Trace/可观测体系**用户可见化**——从「我会看日志」升级到「我把 agent 的执行过程做成了产品的一部分」，是 agent 产品化的常见形态（类似 Claude/ChatGPT 的「思考中」展示）。

---

## 一、设计

### 1.1 核心：统一流式事件协议

现在 `stream_chat` 只产出**文本 delta**，调用方（`main.py`）直接包成 SSE。要带步骤事件，必须让产出**带类型**。

**改成产出二元组**：
```python
async def stream_chat(self, user_msg):
    yield ("text", "回答文本片段")
    yield ("step", {"seq": 1, "text": "识别为「查订单」"})
```

**为什么用二元组而不是改 SSE 格式**：`stream_chat` 是纯逻辑层，不该知道 SSE 长什么样；
`main.py` 负责把它翻译成 SSE。这保持「agent 层不依赖传输格式」的分层。

`main.py` 侧：
```python
async for kind, payload in session.stream_chat(user_msg):
    if kind == "text":
        answer += payload
        yield f"data: {json.dumps({'delta': payload}, ensure_ascii=False)}\n\n"
    else:  # step
        yield f"data: {json.dumps({'step': payload}, ensure_ascii=False)}\n\n"
```

### 1.2 步骤事件的定义

```python
{"seq": 1, "text": "识别为「查订单」"}
{"seq": 2, "text": "调用 search_orders"}
{"seq": 3, "text": "生成回答"}
```

- **seq**：序号，前端据此渲染「第几步」
- **text**：给人看的一句话
- **不带状态字段**：前端把「最后一条」渲染成进行中（收到下一条时上一条自然变完成）——
  比维护 running/done 状态简单，且不会出现「状态和实际不一致」的 bug

**插桩点**（都从已有的 `Trace` 数据派生，不新造统计）：

| 路径 | 事件 |
|---|---|
| 规则路由命中 | `识别为「{意图}」` → `调用 {tool_name}` → `生成回答` |
| ReAct 循环 | `LLM 决策（第 N 步）` → `调用 {tool_name}` → …（循环）→ `生成回答` |
| 降级/异常 | `{异常说明}` |

### 1.3 规则路由流式化

`_run_routed` 的最后一步是 `chat_with_usage`（非流式）。改成 `stream_events`（`llm.py` 已有，`_react_loop_stream` 在用）。

**顺带**：`_run_browse` / `_run_summarize` 走的是 `_pure_generate`（同样非流式）——三条规则分支共用一套流式实现，一起改。

---

## 二、改动文件清单

| 文件 | 改动 |
|---|---|
| `src/agent.py` | `stream_chat` 改产出二元组；新增 `_pure_generate_stream` / `_run_routed_stream`；`_react_loop_stream` 的 `yield` 改二元组并插 step 事件；`_run_browse` / `_run_summarize` 接到流式版 |
| `src/backend/main.py` | `event_gen` 按事件类型分发（`delta` / `step`） |
| `frontend/src/hooks/useChatStream.ts` | 解析新 SSE 事件类型，维护 `steps` 状态 |
| `frontend/src/types.ts` | 加 `StepEvent` 类型 |
| `frontend/src/App.tsx` | 左侧栏加「执行步骤」区 |
| `frontend/src/styles.css` | 步骤列表样式 |
| `tests/test_stream.py` | 扩：断言事件类型与顺序 |
| `docs/architecture.md` / `references/INTERVIEW_POINTS.md` / `TODO.md` | 同步 |

**明确不改**：`chat()`（非流式路径，CLI/测试用）——它没有 SSE 消费者，加步骤事件无意义。

---

## 三、验证标准

| # | 用例 | 期望 |
|---|---|---|
| 1 | 规则路由（查订单） | **逐 token 出现**（不再沉默 4 秒） |
| 2 | ReAct 路径 | 仍逐 token（不能改坏） |
| 3 | 步骤事件顺序 | 规则：识别 → 调用工具 → 生成；ReAct：决策 → 工具 → … |
| 4 | `seq` 连续 | 前端「第 N 步」不跳号 |
| 5 | 工具异常 | 有对应步骤事件，不静默 |
| 6 | 客户端断开 | 无泄漏（`finally` 已有清理） |
| 7 | **首 token 延迟** | 规则路径从「≈ 总耗时」降到「≈ 1 次 LLM 首 token」 |
| 8 | 回归 | `test_stream` / `test_agent_defense` / 路由 / 后台全绿 |
| 9 | `chat()` 不受影响 | 非流式路径行为逐字节一致 |

---

## 四、风险

| # | 项 | 说明 |
|---|---|---|
| 1 | **`_react_loop_stream` 的 yield 全要改** | 约 10 处，漏一处会导致「文本被当事件」或反之——用 `test_stream` 断言全覆盖 |
| 2 | 步骤事件增加 SSE 帧数 | 每步一帧，量很小（个位数） |
| 3 | 前端左侧栏空间 | 已有「测试订单」区，步骤区要放得下（可折叠或分区） |
| 4 | 步骤文案可能泄露内部机制 | 「调用 search_orders」暴露了工具名——**这是产品决策**：给用户看的进度展示天然要暴露「在查订单」。但**不能**暴露状态码/内部 id（与 prompt 第 7 条一致）。文案用**中文动作**（「查询订单」）而非工具名（`search_orders`） |
