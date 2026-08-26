# 工具调用 / ReAct 设计方案（v2）

> 走 B：工具调用主线。手写 ReAct 循环 + 5 个工具，跑通「用户问题 → LLM 决定调工具 → 代码执行 → 结果回灌 → 最终答案」闭环，处理幻觉工具 + 死循环两个生产坑。
>
> 本版已过 plan-reviewer，修复 4 个阻塞、采纳 8 个建议。🔧 = 相对 v1 的关键改动。

## 1. 目标与范围

### 这一步做

1. 5 个工具定义（Function Calling schema）+ mock 数据 + 执行函数
2. 手写 ReAct 循环（while 循环 + 停止条件 + assistant 回填）
3. 幻觉工具校验（工具名白名单）
4. 死循环防护（最大步数 + 连续相同动作检测）
5. 参数校验（非法 JSON / 参数不匹配 → 回灌错误，不崩溃）
6. Token 截断（工具返回 500 字上限）

### 这一步不做（后置，记 TODO.md）

- **多轮对话记忆**：这一步只做单轮，历史上下文管理留到「多轮对话」主线
- **写操作权限开关**：transfer_to_human 权限待决策（见第 10 节）；退款/改单的权限开关留到「退款申请」（第 6 类请求）
- **意图路由（RAG vs 工具）**：商品咨询走 `rag_pipeline.py`，本 agent 只负责订单/物流/库存/政策/转人工
- **真实工具**：订单/物流/库存接真实数据源，待决策（见第 10 节）

## 2. 技术路线

- **循环手写**：不套 LangGraph / LangChain 的 agent executor，自己写 while 循环
- **工具交互用 Function Calling**：DeepSeek 兼容 OpenAI 协议，用 `tools` / `tool_calls` / `tool` role 三要素

理由：README 主线要求「能手写循环、说明 LLM 只输出 JSON 指令、代码才真正执行」；Function Calling 由 API 契约保证格式，避免老式「正则解析 Thought:/Action:」的格式地狱。

## 3. 文件改动

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/infra/llm.py` | 改 | `chat()` 增加 `tools=None`，返回完整 message 对象（含 `content` + `tool_calls`）；docstring 和返回类型注解同步改 |
| `src/rag_pipeline.py` | 改 | 调用处 `chat(...)` → `chat(...).content`，`answer()` 返回类型保持 `str` |
| `src/tools.py` | 新增 | 5 个工具 schema + mock 数据 + 执行函数 + `TOOL_MAP` 白名单 |
| `src/agent.py` | 新增 | ReAct 循环 + 防御 |

新增 `.py` 遵守 `encoding-rules.md`：UTF-8 文件头、`sys.stdout.reconfigure`、文件 I/O 显式 `encoding="utf-8"`。

## 4. 工具定义（5 个，来自 REQUIREMENTS.md）

| 工具 | 参数 | 返回 | 读写 |
|------|------|------|------|
| `search_orders` | `order_id` 订单号 | 订单状态 | 只读 |
| `search_logistics` | `order_id` 订单号 | 物流轨迹 | 只读 |
| `check_stock` | `product_id` 商品ID | 🔧 **库存 + 价格** | 只读 |
| `get_return_policy` | 无参数 | 退换货政策 | 只读 |
| `transfer_to_human` | `problem` 问题描述 | 工单号 | 写（权限待决策） |

🔧 `check_stock` 一并返回库存+价格——这样「价格时效性」这一半数据分治要点（价格该走工具不该进向量库）才能踩到。

## 5. ReAct 循环逻辑（伪代码）

```python
def run_agent(user_msg):
    messages = [system_prompt, {"role": "user", "content": user_msg}]
    last_action = None      # 死循环检测用
    repeat_count = 0

    for step in range(MAX_STEPS=8):
        resp = llm.chat(messages, tools=TOOL_SCHEMAS)   # 返回完整 assistant message
        messages.append(resp)   # 🔧 关键：先回填 assistant（含 tool_calls），否则下轮 API 400

        if resp.tool_calls is None:
            return resp.content                    # 没有工具调用 = 最终答案

        for tc in resp.tool_calls:
            name = tc.function.name

            # 🔧 参数解析：非法 JSON 回灌错误，不崩溃
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": f"错误：参数不是合法 JSON：{tc.function.arguments}"})
                continue

            # ① 幻觉工具校验：工具名不在白名单 → 不执行，回灌错误
            if name not in TOOL_MAP:
                result = f"错误：工具 {name} 不存在，可用工具：{list(TOOL_MAP)}"
            else:
                # 🔧 执行：参数不匹配（缺参/多参/类型错）回灌错误
                try:
                    result = TOOL_MAP[name](**args)
                except (TypeError, KeyError) as e:
                    result = f"工具 {name} 参数不匹配：{e}"
                result = truncate(result, max=500)   # ④ Token 截断

            # ② 死循环检测：🔧 用规范化参数做 key（原来用原始 JSON 字符串）
            key = (name, json.dumps(args, sort_keys=True))
            if key == last_action:
                repeat_count += 1
            else:
                last_action, repeat_count = key, 1
            if repeat_count >= 3:
                return "连续 3 次调用同一工具同一参数，判定死循环，已停止"

            # 结果回灌（tool role，前面已有对应 assistant 消息）
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "达到最大步数仍未得到答案，已停止"
```

## 6. 防御机制（对应 README 五类坑）

| 坑 | 防御 | 实现 | 边界说明 |
|----|------|------|---------|
| 死循环 | 最大步数 + 连续相同动作 | `MAX_STEPS=8`；连续 3 次同工具同参数判死循环 | 振荡型（A→B→A→B）靠 `MAX_STEPS` 兜底；单次响应内多个相同 tool_call 也触发检测（视为异常） |
| 幻觉工具调用 | 工具名白名单 | 未知工具名不执行，回灌「工具不存在」 | 白名单只保证「不执行」，不保证「能纠正」；反复幻觉靠步数兜底 |

Token 爆炸做截断（500 字）；Prompt Injection / 上下文污染留到对应主线。

## 7. 数据源（mock）

订单 / 物流 / 库存没有真实数据，用内存 mock：

- `MOCK_ORDERS`：2-3 个订单号 → 状态（下单时间、商品、金额）
- `MOCK_LOGISTICS`：订单号 → 物流轨迹列表
- `MOCK_STOCK`：商品名 → `{"库存": N, "价格": "¥xx"}`（🔧 模块 1 过渡，已接真实后端：products 表 + `check_stock(product_id)`）；未命中用 `dict.get()` 返回「未查到该商品」，不 KeyError
- `RETURN_POLICY`：一段退换货政策文本
- `transfer_to_human`：返回生成的工单号

`MOCK_STOCK` 必须覆盖 `data/products.md` 里的「幼犬成长粮」（验收用例会问它）。

## 8. system prompt 要点

- 你是宠物电商客服，能查订单 / 物流 / 库存 / 退货政策
- 有工具就用工具查，不要凭空编造订单号 / 库存 / 价格
- 查不到（订单号不存在 / 商品没货）如实说，需要人工介入时调 `transfer_to_human`
- 🔧 **遇到商品咨询（材质/规格/适用对象/价格，非本模块能力）→ 明确说明不属于本模块，引导转人工**，不要胡编

## 9. 验收标准（这一步完成 = 什么算「跑通」）

1. 「我的订单 20240818001 到哪了」→ 调 `search_logistics`，返回物流轨迹
2. 「幼犬成长粮还有货吗」→ 调 `check_stock`，返回库存+价格
3. 「我要投诉」→ 调 `transfer_to_human`，返回工单号
4. 故意让 LLM 调不存在的工具 / 连续调同一工具 → 被幻觉校验 / 死循环防护拦下，不崩
5. 🔧 负向用例：「幼犬粮的材质是什么」（商品咨询）→ agent 不胡编，引导转人工

## 10. 待决策（2 项）

1. **transfer_to_human 权限开关**：REQUIREMENTS 写「写操作需权限开关，默认只读」，`transfer_to_human` 被标「写」。但 demo 阶段它只生成内存工单号、不改真实数据。要不要加权限开关？
2. **mock vs 真实工具**：README 验收是「≥3 个真实工具」，本设计 5 个全 mock。真实数据源（第三方 API 或自建后端）何时做、怎么做？
