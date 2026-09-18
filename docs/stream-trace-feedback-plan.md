# 流式化 + 执行步骤可视化 + trace 落盘 + 反馈评分

> 状态：**已实施完毕（2026-09-18）**，R1-R20 全部吸收 + code-reviewer 复核后的 11 处修复。
> 剩余只有浏览器端手工验证（见 `TODO.md` 对应条目）。
> 来源：用户实测「查订单很慢」+ 用户新需求「左侧展示 trace 链路」+「30s 无回复提示打分」
> 用户已拍板（2026-09-18）：**四个一起做**；评分形态 = **好评/差评 + 原因标签**（不用几颗星）
>
> **实施与本文档的偏差（以代码为准，此处列出避免文档漂移）**：
> 1. **步骤事件只有 `{"text": …}`，没有 `seq`**（见 §二）。生成器是单条顺序产出，
>    前端按数组下标即可确定顺序，`seq` 加了也没人用——**用不到的字段就是负债**。
>    `types.ts` 的 `Step` 类型与之一致。
> 2. **`FEEDBACK_IDLE_SECONDS` 从 `config/settings.py` 移除了**（见 §四）。计时完全发生在
>    浏览器，后端既不参与也不感知；留一个 Python 常量在那儿会造出「改了没反应」的排查黑洞。
>    单一真源 = `frontend/src/App.tsx`。
> 3. **`feedback_id` 直接等于 `trace_id`**（R20 采纳），无 `:user_rating` 后缀。
> 4. **列表排序用 `updated_at` 而非 `created_at`**：把好评改成差评是最该被复盘的信号，
>    按首评时间排它永远浮不上来。
> 5. `POST /api/feedback` 的 `trace_id` **只校格式不校存在**——「无存在性校验」是已知缺口
>    （记 TODO），R12 的「trace 缺失是正常态」决定了不能简单加存在性校验。

---

## ⚠️ 阶段 2 修订记录（**实施时以本节为准**）

### 为什么不能拆成两批做（审核要求写明）

A/B（流式+步骤）与 C/D（落盘+评分）**共享 `trace_id` 这一条唯一的耦合线**。
拆开做要**改两遍** `main.py` 的 `event_gen`：第一次引入二元组协议，第二次再往里塞 trace_id 与落盘。
在「抽考优先 / 客服 agent 退二线」的当前节奏下，改两遍的窗口期最容易拖成半成品。

**为什么现在做**：`trace 落盘` 是 `feedback` 的**前置依赖**（没有它，评分是孤立数字——调研原话「这正是飞轮死掉的地方」），
而 trace 落盘本身又是**可观测性的补齐**（现在 Trace 是纯内存对象，请求结束即丢）。
TODO.md 里「不再加新功能」的约定已被用户明确覆盖，此处记录在案。

---

### R1. 🔴 `include_usage` 会打崩**所有**流式回答（含答案轮正路）

`_react_loop_stream` 现在的写法是「非 text 就当成 tool_calls」：
```python
async for ev in stream_events(...):
    if ev[0] == "text": ...
    else:                      # ← 默认 tool_calls
        tool_calls_list = ev[1]
```
新增 `("usage", obj)` 后：决策轮 → `tool_calls_list` 被 usage 对象覆盖 → `tc["name"]` **TypeError**；
**答案轮（主路）** → 唯一非 text 事件就是 usage → `if not tool_calls_list:` 对 pydantic 对象**判为假** → 走错分支 → **同样崩**。
**开 `include_usage` 之后每一次流式回答都会炸。**

**修法（四步，缺一不可）**：
1. `src/infra/llm.py`：`stream_events` 显式 yield `("usage", usage_obj)`，**定死时机**（放在 `tool_calls` 之后、流结束前），
   docstring 写明「出边界有三种 kind：text / tool_calls / usage」。
2. `_react_loop_stream` 的 `else` 拆成显式分支：`elif ev[0]=="tool_calls"` / `elif ev[0]=="usage"` / `else: 显式告警`。
3. `trace.add_llm(step+1, elapsed, 0,0,0,0)`（`agent.py:394`）**必须用 usage 事件回填** token/cache——
   否则落盘的 `total_tokens`/`cache_hit` 永远是 0，而这两个字段正是抓「Token 爆炸」的抓手。
4. `tests/test_stream.py` 的 mock **不产 usage 事件** → 必须补一条「usage 事件不逃逸」的用例
   （对标 tool_calls 那条），否则这个崩溃测不到。

### R2. 🔴 `retrieved_ids` 取数路径不成立，且**最该复现的两类失败零覆盖**

四个障碍：① `_record_images` 只收**有图**商品且**丢掉 chunk_id**；
② `main.py` 的 finally **已经 pop 过一次**（读取并清空）→ 落盘写在其后拿到空；
③ `_collected_images` 是**模块级 list**，并发会串（图片串了是 UI 瑕疵，**chunk_id 串了是往「用来复现失败」的表里写别人的数据**）；
④ **「双低」拒答分支和 `get_return_policy` 从不调用它** → 最典型的失败恰好零覆盖。

**修法**：
- 扩展收集产物为 `{"product_id","title","image","intro","chunk_id"}`，**一次 pop 同时返回 images 与 retrieved_ids**（避免二次读取拿空）；
- **「双低」分支也要收集**（结果非空、只是被策略拒了，正是要看的）；
- `_get_return_policy_sync` 命中分支补收集；
- 并发串扰：**在表注释与方案里明写「retrieved_ids 在并发下可能串，demo 单用户」**，不修（per-request contextvars 改造不划算），但**不能默默带过**。

### R3. 🔴 前端 `traceId` 不能是单值 state，否则评分挂错 trace

`types.ts` 的 `Message` **没有** `traceId` 字段。多轮后点**旧消息**的 👍 会提交**最新一轮**的 trace_id ——
**这不是显示问题，是数据污染**，直接毁掉「trace↔feedback」这条线（本方案全部价值所在）。

**修法**：`Message` 加 `traceId?: string`；SSE 首帧到达时 `patchAsst(m => ({...m, traceId}))`；
评分控件从 `message.traceId` 取。**缺失时禁用评分控件**（不发无 trace_id 或伪造的请求）。
补验证：连续两轮后评第一条，回溯到的 `trace.query` 必须是第一轮的。

### R4. 🔴 后台 `/admin/feedback` 页在改动清单里不存在

只列了聊天页的 `components/Feedback.tsx`。实际还需要：
- `frontend/src/admin/pages/FeedbackPage.tsx`（新建）
- `frontend/src/admin/AdminApp.tsx`（加 Route，否则落到 `<Route path="*">页面不存在`）
- `frontend/src/admin/AdminLayout.tsx`（`NAV` 加一项，否则侧栏点不进去）

### R5. 🔴 `POST /api/feedback` 零校验零限流（违反项目写接口口径）

项目**每一个**外部输入写接口都有：`_clean_field` 截断 + 白名单校验 + 独立限流桶 + 理由注释。
**修法**：
- `rating` ∈ {1,-1}；`reason` ∈ 新枚举白名单；`comment` 截断 512；`trace_id` 格式校验 + `_clean_field`；
- **新开 `feedback` 独立桶**（`RL_FEEDBACK_*` 环境变量兜底），理由：「匿名可写，与 read 桶混用会让灌评分把聊天打成 429」；
- `GET /api/admin/feedback` 挂 `require_role("admin") + _limit_admin`；
- `tests/test_admin_api.py` 的 `ADMIN_ENDPOINTS` 加新接口（无 token → 401 扫描）+ `test_contract` 加契约断言。

### R6. 🟡 落盘 finally 的四个口子
1. **必须独立 try/except** —— finally 里的异常会**吞掉它后面的 finally 语句**，而 `session_store.save` / `spawn_extract` 排在后面，落盘一抛就都不执行。
2. **`session.get_last_trace()` 可能为 `None`** —— trace 在 `stream_chat` 首次 `__anext__` 才创建；`FAKE_STREAM=1` 走早退分支压根不调它 → **必须 guard**（`test_stream_disconnect.py` 正好会跑这条路径）。
3. **客户端断开**（`except CancelledError`）能落盘但 answer 是半截的、usage 可能从未收到 → `end_reason` 要补一个**「断开」态**（默认 `"未知"` 会和「跑完但没设值」的样本混在一起，SQL 分不开）。
4. **同步 MySQL 写在 finally 里阻塞事件循环** —— `db.py` 是同步 pymysql 且**未设 `connect_timeout`**，MySQL 挂时会按默认超时干等。**至少设 `connect_timeout`**，并在方案里写明代价。

### R7. 🟡 `VARCHAR(512)` 无截断 → **最该看的病态样本恰恰写不进去**
`query`（`/chat/stream` 没做长度上限）、`tool_calls`（8 步多工具最易超）、`retrieved_ids`（多轮累加会超）
—— 而落盘是「失败只打日志」，于是**恰好在最需要排查的病态 case 上静默丢样本**。
**修法**：`_clean_field(user_msg, 512)` 复用现有工具；两个 JSON 列落库前显式截断（保留前 N 条）**并标记「已截断」**。
（`answer TEXT` 不是问题：`MAX_OUTPUT_TOKENS=2048` 封顶。）

### R8. 🟡 隐私口径与项目既有 PIPL 决定**自相矛盾**
`rules.py` 的 `MEMORY_SENSITIVE_PATTERNS` + `_is_sensitive_memory` 是**刻意拒绝**把手机号/身份证/**订单号**/邮箱/地址
写进 `user_memories` 的；而 `traces.query/answer` 会**无条件**存下同一批内容——同一份代码库里一处拒绝、一处全量落盘。
**修法**：**显式承认这个不一致**并给取舍（trace 是内部排障数据、与面向用户的画像口径不同），
**不是**一句「demo 可接受」带过。

### R9. 🟡 口径标注（照抄 `/api/admin/metrics` 的先例）
CLI/评测不落盘 → `/admin/feedback` 看到的**全是 Web 链路**。新接口返回体要带 `sample_source: web`，
否则同一后台里两套口径不一致、看的人必然误读。

### R10. 🟡 验证 #11「低分不进池」是**同义反复**，且文件名写错
`tests/regression.json` **不存在**（实际是 `regression_retrieval.json` / `regression_routing.json`）；
且 `POST /api/feedback` **不可能**碰到池（`regression.py` 的入口只被两个 eval 脚本调用）→ 断言恒真。
**修法**：#11 从测试表**移到 §四作为代码约束**，并落一条可 review 的守卫（code review 项：
「`api/feedback` 不得 import `tests.regression`」）。

### R11. 🟡 `ON DUPLICATE KEY UPDATE` 与项目既有幂等写法不一致
全仓 `ON DUPLICATE` / `REPLACE INTO` **零使用**；幂等一律 `except pymysql.err.IntegrityError`
（`main.py` 画像、`mq.py` 退款工单）。**改用项目一致写法**。
另：表**没有 `updated_at`** → 用户改主意时「什么时候第一次评的、什么时候改的」丢失，而时间趋势是这类表最常用的维度 → **加 `created_at` + `updated_at`**。

### R12. 🟡 `feedback.trace_id` 弱引用 → 悬空行 + 后台渲染未定义
落盘失败或 FAKE_STREAM 路径下 feedback 仍能写入 → 后台回溯不到 trace。
**照抄项目已有口径**：**trace 缺失是正常态不是错误**，前端渲染成「该轮记录未保存」而非报错。

### R13. 🟡 步骤语义：答案已开始吐 token，侧栏还在转「思考中」
**修法**：**该轮首个 text delta 到达时**把最后一步切到「生成回答中」（或置完成）。
这个信号前端已有（`obj.delta` 到达即触发），**零额外成本**。

### R14. 🟡 `_run_routed_stream` 的告警白名单必须含 `"usage"`
否则规则路由**每一次**请求都刷一条告警 → 告警很快变成没人看的噪声。
白名单 = `{"text", "usage"}`；只有白名单之外才告警。

### R15. 🟡 工具→中文动作映射放 `rules.py` 不是 `prompts.py`
`prompts.py` 的模块契约自己写着「所有**送给 LLM 的**文案」——而这张表是**给用户看的**，不送 LLM，
且「统一显示正在处理」「映射 miss 回落」都是**判据式规则** → 归 `rules.py`（与 `MEMORY_CATEGORIES`/`WRITE_TOOLS` 同类）。

### R16. 🟡 `PROMPT_VERSION` 改**自动 hash**，并补 `kb_version`
- `PROMPT_VERSION = hashlib.sha256(SYSTEM_PROMPT...).hexdigest()[:8]`，import 时算，**零维护、不可能忘**。
- 更实质的问题：「复现失败」单靠 prompt 版本不够 —— 同样决定答案的还有知识库源数据与 `tools.py` 里**硬编码的用户可见话术**
  → 至少加 `kb_version`（源文件 hash）。

### R17. 🟡 排期没落到可执行
写死：**先单独提交 demo-polish，再开工本方案**。重叠锚点：`App.tsx` 侧栏、`styles.css`、`main.py` 的 `event_gen` finally。
合并提交会让两批改动在 diff 里分不开，**出问题无法单独回滚**。

### R18. 🟡 `_route_tool_call()` 抽得不够（防漂移只完成一半）
真正的漂移风险在**参数解析 + 死循环检测 + `called_write` 占位 + `_exec` 协程体**
（`_react_loop` 与 `_react_loop_stream` 几乎逐行对应）。
**阻碍**：非流式侧 `tc` 是 **openai 对象**、流式侧是 **dict** → 要抽必须先归一化（统一成 `{"id","name","arguments"}`）。

### R19. 🟡 30s 提示的边界全部未定义
什么算「活动」（**在输入框打字算不算**）/ **streaming 中不得触发** / 最后一条非 assistant 时不得出现 /
已评分的不再出现 / **差评卡片在提交原因前不应关闭** / `visibilitychange` 后台标签页定时器被节流 /
**新轮开始即移除旧的未提交卡片**（否则多张卡片同时挂着，用户不知点哪个）。

### R20. 🟡 `feedback_id` 的 `user_rating` 后缀无意义
`f"{trace_id}:user_rating"` 等价于「以 trace_id 为主键」——要么说明为什么预留后缀（将来接 judge_rating？），
要么直接用 `trace_id`。

---

---

## 〇、四个模块与它们的耦合

```
请求开始 ──→ 生成 trace_id ─┬─→ 模块 A/B：流式化 + 步骤事件（SSE 内）
                            ├─→ 模块 C：trace 落盘（请求结束时写库）
                            └─→ 模块 D：评分（trace_id 随 SSE 首帧给前端 → 评分带它回来）
```

**为什么必须一起做**：`trace_id` 是贯穿四者的一条线。**没有落盘，评分就是孤立数字**——
调研原话：「只能聚合（『FAQ 意图低分占 30%』），**无法复现失败**，不知道当时的 prompt 版本、
检索命中了哪些 chunk、工具调用序列是什么。**这正是飞轮死掉的地方**」。

---

## 一、模块 A：规则路由流式化

### 现状（实测确认）
`src/agent.py:613-620`：规则路由命中时 `result = await _run_routed(...)` → `yield result`，**一次性吐出**。

**实测数据**（本机热缓存，2026-09-18）：

| 工具 | 耗时 |
|---|---|
| search_orders / search_logistics | **0.003s** |
| check_stock | 0.004s |
| search_products（embedding+混合检索+rerank） | 0.033s |
| get_return_policy | 0.054s |

**结论**：**工具是毫秒级的，4 秒里 99% 是 LLM 生成** → 流式化是主要收益。
（plan-reviewer 曾担心「RAG 路由工具耗时可能数秒」，实测推翻；但它要求「先量分母再定标准」是对的。）

### 设计
`stream_chat` 的产出从「纯文本 delta」改为**带类型的二元组**：

```python
yield ("text", "回答片段")
yield ("step", {"seq": 1, "text": "识别为「查订单」"})
```

`main.py` 负责翻译成 SSE —— agent 层不知道 SSE 长什么样，分层不变。

---

## 二、模块 B：执行步骤可视化

### 事件定义
```python
{"text": "识别为「查订单」"}     # ⚠️ 实施时去掉了 seq（见开头「偏差 1」）
```

**不带状态字段**：前端把「最后一条」渲染成进行中。但**必须有终态**——
收到 `[DONE]` 或 `streaming` 变 false 时，前端把全部步骤置为完成（否则最后一步永久转圈）。

### ⚠️ 插桩的三个硬约束（plan-reviewer 指出，都是结构性的）

**① 工具步骤不能写在 `_exec` 里**：工具走 `asyncio.gather` 并发，`_exec` 是**协程**——
协程里 `yield` 无法交给外层生成器（结构上不成立）。
→ 必须在 `await asyncio.gather(...)` **之前**，按 `parsed` 的顺序逐条 yield（顺序确定，不受 I/O 完成序影响）。

**② ReAct 的「生成回答」在流式下不可知**：只有收到第一个 delta 才知道这一轮是答案轮还是决策轮。
→ 采用**方案①**：统一叫「思考中（第 N 步）」，不区分决策/回答（最省事且语义真实）。
N 用 `for step in range(MAX_STEPS)` 的**循环变量**，不用 `len(trace.steps)+1`（`trace` 默认 `None` 会崩）。

**③ `("step",…)` 与 `stream_events` 的 `("tool_calls",…)` 是同层协议**：
实现者写 `yield ev` 时，text 部分**碰巧正确工作**，而 `tool_calls` 会逃到 `main.py` 的 `else` 被当步骤下发。
→ `main.py` **显式三分支**（`if text / elif step / else: 告警并丢弃`），绝不用 `else` 兜；
→ `_run_routed_stream` 只处理 `ev[0] == "text"`，收到其他类型**显式告警不静默丢弃**；
→ `stream_chat` 的 docstring 写成契约：出边界的 kind 只有 `"text"` 和 `"step"`。

### 工具 → 中文动作映射
放 `src/config/prompts.py`（文案归 config，不硬编码进 `agent.py`）。

| 工具 | 展示文案 | 依据 |
|---|---|---|
| search_products | 查询商品信息 | 业务动作，用户该知道 |
| search_orders / search_logistics / check_stock | 查询订单 / 物流 / 库存 | 同上 |
| get_return_policy | 查询退货政策 | 同上 |
| refund_order | 提交退款申请 | ⚠️ **不带**「工单/审批/权限」字样（暴露写操作权限设计） |
| ⚠️ `check_online` / `transfer_to_human` | **不展示具体动作**，统一显示「正在处理」 | 展示「查询客服在线状态」= 把「转人工前先探活 + 不在线不承诺」这套机制摆到用户面前，正是 `SYSTEM_PROMPT` 第 7 条说的防御机制细节 |
| 映射 miss（幻觉工具名） | 固定字符串「处理请求」 | **绝不回落裸工具名**（可能含非法字符，进 `json.dumps` 会炸） |

---

## 三、模块 C：trace 落盘

### 表结构
```sql
CREATE TABLE IF NOT EXISTS traces (
    trace_id      VARCHAR(36) PRIMARY KEY,
    session_id    VARCHAR(64),
    user_id       VARCHAR(64),
    query         VARCHAR(512),
    answer        TEXT,
    route_source  VARCHAR(16),      -- 规则 / LLM
    end_reason    VARCHAR(16),      -- 正常/死循环/超步数/异常
    total_sec     DOUBLE,
    total_tokens  INT,
    cache_hit     INT,
    cache_miss    INT,
    llm_steps     INT,
    tool_calls    VARCHAR(512),     -- JSON
    retrieved_ids VARCHAR(512),     -- 检索命中的 chunk_id（调研强调：不带就复现不了失败）
    created_at    VARCHAR(32),
    KEY idx_created (created_at),
    KEY idx_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**沿用 `user_memories` 的规矩**：`CREATE TABLE IF NOT EXISTS`，**不参与 `_init_db` 的 DROP 重建**
（trace 是运行时数据，不是 seed 派生）。

### trace_id 的产生与传递
```
/chat/stream 请求开始 → trace_id = uuid4()
    ├─ 传给 AgentSession（记进 Trace）
    ├─ SSE 首帧 {"trace_id": "..."}  ← 前端存下来，评分时带回
    └─ 请求结束（finally）→ 落库
```

### 落盘内容里**必须带**的三样（调研强调，缺了就复现不了失败）
1. **prompt 版本号** —— 现在 prompt 在 `config/prompts.py`，改动无版本标记。
   → 加一个 `PROMPT_VERSION` 常量（手工 bump），落进 trace。
2. **检索命中的 chunk_id** —— 从 `_record_images` / 检索结果收集（`retrieved_ids`）。
3. **工具调用序列** —— `trace.tool_calls` 已是 `[(name, elapsed, step, is_empty)]`。

### 降级
落盘失败**不阻塞**（同 `session_store` 哲学：可观测是增强不是依赖），只打日志。

⚠️ **隐私标注**：`query` / `answer` 是用户对话内容。demo 可接受，但要在表注释里写明
「生产环境需脱敏或设保留期」——PIPL 视角下这是用户个人信息。

---

## 四、模块 D：反馈评分

### 形态（用户拍板 + 调研修正）
**好评/差评 + 原因标签**，不做几颗星：
- 调研：星级偏差大（5 星堆在 4–5）
- **客服场景特有**：差评可能跟答案质量**完全无关**（物流慢、催单、情绪发泄）→ **必须有原因标签**，否则噪声直接污染回流

### 触发时机（用户要求）
- **常驻**：每条 assistant 消息下方一个轻量 👍/👎 图标（**不做弹窗**——调研明确「不 modal、不强制」）
- **超时提示**：**30 秒无输入**时，在最后一条消息下方显示评分卡片（内联，不是 modal），提交后显示「谢谢使用」
- 超时值放**前端** `frontend/src/App.tsx`（`FEEDBACK_IDLE_SECONDS = 30`）
  ——原计划放 `config/settings.py`，实施时改掉：计时完全在浏览器，后端那份是死配置（见开头「偏差 2」）

### 原因标签枚举（放 `config/rules.py`）
`答非所问` / `信息错误` / `没解决问题` / `语气不好` / `其他`（差评时展示；好评可直接提交）

### 表结构
```sql
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id VARCHAR(64) PRIMARY KEY,   -- f"{trace_id}:user_rating"（幂等键）
    trace_id    VARCHAR(36) NOT NULL,
    session_id  VARCHAR(64),
    rating      TINYINT NOT NULL,          -- 1 = 好评, -1 = 差评
    reason      VARCHAR(32),
    comment     VARCHAR(512),
    created_at  VARCHAR(32),
    KEY idx_trace (trace_id),
    KEY idx_rating (rating, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**幂等**：`feedback_id = f"{trace_id}:user_rating"` + `INSERT ... ON DUPLICATE KEY UPDATE`
—— 用户改主意是**更新不是新增**（调研强调的标准做法）。

### 接口
| 接口 | 用途 | 鉴权 |
|---|---|---|
| `POST /api/feedback` | 提交评分（幂等） | 无需（普通用户） |
| `GET /api/admin/feedback` | 后台查看评分列表 | **admin** |

### 🔴 低分**不进**坏 case 池 —— 进「待复核队列」
`tests/regression.py` 的池**毕业判据是 `top1 ∈ expected`（客观）**，而用户评分的判据是**主观**。
直接进池 → 池会因「没有 expected」在 1 次跑后被**假阳性毕业**。

**所以两套记录分开**：

| | 来源 | 判据 | 用途 |
|---|---|---|---|
| **内部记录**（已有） | 评测跑出的坏 case | 客观 `top1 ∈ expected` | 自动回归、自动毕业 |
| **外部记录**（新增） | 用户评分 | 主观 | **只用来决定「去看哪些 trace」** |

**从外部到内部的通道必须经过人工**：低分 trace → 人工读样本（调研：约 20 分钟）→ 聚类失败模式
→ 手写 case 进 `tests/cases.py`（**用「修正后的答案」当 expected，不是被嫌弃的原答案**）。

**禁止把原始差评自动灌进评测集**——那叫回归集污染（调研原话）。翻车案例：
OpenAI 把点赞当 reward signal，上线了谄媚模型；arXiv 论文证明用模拟用户反馈做 RL 会让模型
**精准锁定那 2% 易被操纵的用户**。

### 后台新增页
`/admin/feedback`：评分列表（trace_id / 评分 / 原因 / 时间）+ 点进去看该 trace 的完整记录。

---

## 五、改动文件清单

| 文件 | 模块 | 改动 |
|---|---|---|
| `src/agent.py` | A/B/C | `stream_chat` 产出二元组；新增 `_pure_generate_stream` / `_run_routed_stream`；`_react_loop_stream` 的 **5 处 yield** 改二元组并插步骤事件；**抽共享函数** `_route_tool_call()`（工具执行+truncate+is_empty+route_source）供两条路复用，防漂移 |
| `src/backend/main.py` | A/C/D | `event_gen` 显式三分支分发 + 首帧发 trace_id + finally 落盘；新增 `POST /api/feedback`、`GET /api/admin/feedback`；两张表建表 |
| `src/config/prompts.py` | B/C | 工具→中文动作映射；`PROMPT_VERSION` |
| `src/config/rules.py` | D | 差评原因标签枚举 |
| `src/config/settings.py` | D | ~~`FEEDBACK_IDLE_SECONDS`~~ → **实施时取消**（改前端，见开头「偏差 2」）；`FEEDBACK_COMMENT_MAX` 落在这里 |
| `src/infra/llm.py` | C | `stream_events` 加 `stream_options={"include_usage":True}` 并**产出 usage 事件**（实测：usage 绑在最后一个 chunk、choices 长度 1、含 cache 字段） |
| `src/infra/observability.py` | C | Trace 加 `trace_id` / `retrieved_ids`；`summary()` 透出 tool elapsed |
| `frontend/src/hooks/useChatStream.ts` | A/B/D | 解析 `step` / `trace_id` 事件；维护 `steps` / `traceId` 状态；`send()` 与 `reset()` 时清空 |
| `frontend/src/App.tsx` | B/D | 左侧栏加步骤区；消息下方加评分控件；30s 无输入提示 |
| `frontend/src/components/Feedback.tsx` | D | **新建** |
| `frontend/src/types.ts` / `styles.css` | B/D | 类型 + 样式 |
| `tests/test_stream.py` | A | 断言事件类型与顺序 |
| `tests/test_feedback.py` | C/D | **新建**：幂等、边界、落盘 |
| `docs/` / `references/INTERVIEW_POINTS.md` / `TODO.md` | — | 同步 |

---

## 六、验证标准

| # | 用例 | 期望 |
|---|---|---|
| 1 | 规则路由（查订单） | **逐 token 出现**（不再沉默 4 秒） |
| 2 | ReAct 路径 | 仍逐 token（不能改坏） |
| 3 | 步骤事件顺序 | 规则：识别 → 调用工具 → 思考中；ReAct：思考中 → 工具 → …（seq 连续） |
| 4 | **README 声称的 `tool_calls` 不逃逸** | 构造 `stream_events` 产出 `("tool_calls",…)`，断言它**不**变成 step |
| 5 | **空回复兜底** | 模型返回空 → 用户侧**有可见文本** + 本轮入历史 + 步骤有终态（B5 的回归） |
| 6 | **首 token 延迟** | 脚本量「首个 step 帧 / 首个 delta 帧 / 总耗时」三个数，改造前后对比 |
| 7 | trace 落盘 | 每轮请求写一条，含 trace_id / retrieved_ids / tool_calls / prompt 版本 |
| 8 | trace_id 传递 | SSE 首帧含 trace_id，与落库记录一致 |
| 9 | 评分幂等 | 同一 trace 提交两次 → 更新不是新增 |
| 10 | 评分关联 | `GET /api/admin/feedback` 能按 trace_id 回溯到完整 trace |
| 11 | **低分不进池** | 提交差评后，`tests/regression.json` **无变化** |
| 12 | 30s 提示 | 无输入 30 秒后出现评分卡片；提交后显示「谢谢使用」 |
| 13 | 密码学无关：`chat()` 不受影响 | 非流式路径行为逐字节一致 |
| 14 | 回归 | 全部现有测试绿 |

---

## 七、风险

| # | 项 | 说明 |
|---|---|---|
| 1 | **`_react_loop_stream` 的 5 处 yield** | 逐处改，漏一处会造成「事件被当文本」或反之——用 test_stream 全覆盖 |
| 2 | **B1 的分母** | 实测工具毫秒级，但那是**热缓存**。冷启动（首次 embedding/rerank 加载 ~8s）另说——预热已在 lifespan 里，生产路径不受影响 |
| 3 | **流式化丢 usage** | **已实测解决**：DeepSeek 支持 `include_usage`，usage 绑最后一个 chunk（choices=1，非空数组）。对 ReAct 路径也是纯赚 |
| 4 | **改动面是所有任务里最大的** | 4 模块 + 后端/前端/config + 3 张表 + 新页面，且互相耦合 |
| 5 | **隐私** | trace 存了用户完整对话，生产需脱敏/保留期（表注释标注） |
| 6 | **与 `demo-polish-plan` 的同文件冲突** | 都改 `App.tsx` / `styles.css` / `main.py:event_gen`。**demo-polish 已实施完但未提交**——本方案**排在它之后**，共用侧栏 |
| 7 | **OTel 暂不做** | 调研结论已存（双轨制 + Phoenix + async context 必撞坑），本轮不实施，记 TODO |
| 8 | 前端技术栈 | 用户是 Cocos 前端出身，React 状态管理（`steps`/`traceId`/评分态）若有阻力可降样式复杂度 |

---

## 八、本轮不做（记 TODO）

- **OTel 接入**（调研已完成，结论已存）：双轨制适配器 ~150 行 + Phoenix 单容器；**必撞的坑** = `asyncio.gather` 与 async generator 的 contextvars 丢失
- **隐式信号采集**（调研：密度比显式评分高 50–100 倍）：转人工率、改写重问率、会话中断
- **judge 与用户评分的一致性分析**（Kappa）——调研称这是「飞轮」最亮的面试故事
- 差评的自动聚类
