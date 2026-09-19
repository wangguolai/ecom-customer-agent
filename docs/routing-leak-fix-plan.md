# 修复批次 2026-09-20（整合版）

> 本文件取代同日早先的同名版本（旧版只覆盖路由+泄漏，且被 plan-review 查出 3 处硬伤）。
> 本批共 8 项，全部来自 2026-09-20 的实测或独立审核，**没有一条是推测**。

---

## 0. 批次清单（按优先级）

| # | 项 | 级别 | 状态 |
|---|---|---|---|
| **P0** | 摘要 / 画像抽取线上失效（推理吃光 max_tokens） | 🔴 线上功能失效 | 修法**已实测验证** |
| **P1** | 路由词表「比较」误匹配（今日 bug 根因） | 🔴 正确性 | 已知修法 |
| **P1** | 工具调用文本泄漏防御（放大器） | 🔴 正确性 | 已知修法（分层） |
| **P2** | `--effort` 三条静默失效 + 裁判污染 + 注释错误 | 🟡 实验可信度 | 已知修法 |
| **P2** | 推理 token 落库/聚合出口（原 T3） | 🟡 可观测 | 已知修法 |
| **P3** | 评测集守卫（含一条存量失败 case） | 🟡 测试 | 已知修法 |
| **P3** | 文档漂移 + 挂起决策落盘 | ⚪ 记录 | 已知 |
| **—** | 词表治理机制 / Qdrant server 模式 | ⚪ 待定 | 本批**不做** |

---

## 1. P0 · 摘要与画像抽取线上失效（最高优先）

### 现象（实测，非推断）

用 `_summarize` 的真实调用形态（`system=SUMMARY_INSTRUCTION` + 历史）实测：

```
max_tokens=300（= SUMMARY_MAX_TOKENS）→ completion=300, reasoning=300, content = 0 字
max_tokens=400（= MEMORY_EXTRACT_MAX_TOKENS）→ completion=400, reasoning=400, content = ''  ← 抽取同样空
```

**推理 token 把预算整段吃光，`content` 一个字都没产出。**

### 影响链

```
deepseek-v4-pro 是推理模型（默认思考开启、effort=high）
  → 推理 token 计入 completion_tokens，而 max_tokens 封顶的正是 completion_tokens
  → SUMMARY_MAX_TOKENS=300 被推理吃光 → 摘要返回空
  → agent.py:118 的守卫触发（打印一行 stderr 警告后 return）
  → **上下文压缩从未生效**：历史永不压缩，持续膨胀 → Token 爆炸（五类生产坑之一）
```

画像抽取同理：空输出 → `_parse_extract_output("")` 返回 `[]` → **画像从未被自动写入过**。

⚠️ **佐证更正（二轮审核查出）**：早先版本写「后台 active 记录是手动 `POST /memory` 写入的，
`source_turn=None` 可佐证」——**错**。`main.py:669-675` 的 INSERT 列清单里**没有 `source_turn`**，
全仓只在 DDL（`main.py:96`）出现一次 → 该列**恒为 NULL**，区分不了手动/抽取写入；且抽取路径
**也**走 `POST /memory`（`memory.py:331`），"POST /memory = 手动"这个区分本身不成立。
结论（画像从未被写入）仍成立，但**依据必须换**：只能靠「抽取调用恒返回空」这条实测。
写进文档的错误佐证会被下一轮当成已验证结论复用——这条教训本身值得记。

**这是切到 `deepseek-v4-pro` 时引入的静默回归**——两条路径都只打一行警告，谁都看不见。

### 修法（已实测验证，两个候选都跑过）

| 场景 | 结果 |
|---|---|
| 摘要 · 现状（指令在 system，思考开，300） | 🔴 空 |
| 摘要 · 关思考，指令仍在 system，300 | 🔴 **不摘要，直接续写对话** |
| 摘要 · **指令尾置 + 关思考**，300 | ✅ **摘要完整**（completion=60，关键实体/未解决问题全抓到） |
| 摘要 · 指令尾置 + 思考开，300 | 🔴 空（推理仍吃光） |
| 抽取 · 关思考，400 | ✅ **JSON 完美**（两条事实都抽对） |

**结论：两个调用都需要「关思考」，摘要还需要额外「指令尾置」。**

- 摘要为什么必须指令尾置：`system=摘要指令 + 历史` 在关掉思考后**看起来就是一段待续写的对话**，
  模型会接着回答最后一条用户消息（实测输出「订单 20240818001 的物流显示已签收。」）。
  把指令放到**最后一条 user 消息**（摘要任务的标准形态）才能让"总结"成为明确的当前任务。
  ⚠️ 同时要改 `SUMMARY_INSTRUCTION` 的措辞：「把**以下**…」→「把**以上**…」，否则指代反了。
- 为什么不用「保持思考 + 大预算」（实测 1500 也能出好摘要）：成本与延迟都差一个量级
  （680 vs 60 completion token），而机械任务不需要推理。

### 改动

| 文件 | 改什么 |
|---|---|
| `src/config/prompts.py` | `SUMMARY_INSTRUCTION` 的「以下」改「以上」 |
| `src/agent.py` `_summarize` | 指令由 `messages[0]` 的 system 改为**尾置 user 消息**；传 `reasoning_effort="off"` |
| `src/memory.py` 抽取调用 | 传 `reasoning_effort="off"` |
| `src/config/settings.py` | 新增 `REASONING_EFFORT_MECHANICAL = "off"` |
| `src/infra/llm.py` | `_reasoning_params` 支持 `"off"`（→ `extra_body {"thinking":{"type":"disabled"}}`）；两个入口加 `reasoning_effort` 显式参数 |

---

## 2. P1 · 路由词表「比较」误匹配（今日 bug 的根因）

已实测复现，链路：`"比较喜欢吃"`（副词）→ 命中 `SUMMARIZE_WORDS` 裸词 `"比较"`（动词"对比"）
→ `route_by_rule` 返回 `("summarize", None, None)` → `_run_summarize` → **纯生成不带 tools**
→ 模型想调 `search_products` 却无工具 → 把调用写成文本 → 原样透传给用户。

**判据（为什么删而不是补否定词表）**——不对称代价：

| 形态 | 后果 | 严重度 |
|---|---|---|
| **漏**（该 summarize 没识别）→ `None` → 走 ReAct（带 tools） | 多一次决策调用，答案仍正确 | 低 |
| **错**（不该 summarize 却识别）→ 纯生成无 tools | 反问走偏 / 工具调用泄漏 / 空承诺 | 高 |

`intent_router.py` 的既定原则本就写着「宁可漏（走 LLM）不可错（错误路由）」。「比较」是中文里
歧义最大的词之一，裸词形态**必然**持续误伤口语（比较好/比较喜欢/比较便宜），补否定词表是打地鼠。

**改动**：`rules.py` 的 `SUMMARIZE_WORDS` **去掉裸 `"比较"`，只加 `"比较一下"`**。

⚠️ **二轮审核纠正了早先版本的加词方案**——早先写「加 `比较一下` / `比较下` / `比较哪`」，
其中两条会把同类 bug 引回来：

- **`"比较下"` 必须删**：`SUMMARIZE_WORDS` 是**纯子串匹配**（`intent_router.py:122` 的 `w in compact`），
  `"比较下"` 会命中副词「**比较下来**还是第一款好」「**比较下去**」→ 又是「错路由 → 纯生成无 tools
  → 泄漏/走偏」，与本节自己的不对称代价表（错 > 漏）**直接冲突**。
- **`"比较哪"` 冗余**：`"比较哪个好"` 已被既有的 `"哪个好"` 覆盖（子串包含），加了是重复项。
- `"比较一下"` 保留：`一下` 强制动词读法（"比较一下这两款"），无副词歧义。

即：**只做减法 + 一条无歧义的加法**，不为覆盖率引入新的歧义面。

**已确认的存量失败**：`tests/cases.py` 的 `"幼犬吃什么粮比较好"`（ReAct-咨询类误调订单工具）
同样中招。⚠️ 更正早先的错误说法：它**不是"假通过"**，而是 `docs/deep-dive-points.md` 坑 7
**已记录的已知失败**（`eval_react_boundary.py:42` 断言 `search_products >= 1`，路由成 summarize
后工具数为 0 → 必然 ❌）。坑 7 的面试指向比本文档更准，**修 case 时以坑 7 为准**。

---

## 3. P1 · 工具调用文本泄漏防御

### 检测范围

「不给工具，却输出了工具调用」是协议违例。全项目「不带 `tools=` 的 LLM 调用」共 4 处：

| 调用点 | 输出直达用户 | 处置 |
|---|---|---|
| `_pure_generate` / `_stream_generate`（规则路由纯生成） | **是** | **检测** |
| `_run_routed` / `_run_routed_stream`（规则工具路径，工具已执行） | **是** | **检测，但处置不同** |
| `_compress_history` 摘要 / `memory` 抽取 / `rag_pipeline` | 否 | 不检测 |

⚠️ **早先版本漏了 `_run_routed`（非流式，`agent.py:134`）**——它自带内联生成
（`chat_with_usage(messages)` 无 tools），同属「工具结果注入 + 不带 tools 生成 + 输出直达用户」。
**共 4 个生成调用点，不是 2 个。**

### 分层处置（安全关键）

⚠️ **`WRITE_TOOLS = {"refund_order", "transfer_to_human"}`，而规则路由的 tool 分支包含
`transfer_to_human`。** 但安全论证的事实要按代码写准（早先版本写错了）：

- `transfer_to_human` **不落任何库**：`tools.py:378-398` 只生成 `TK{uuid4}` 字符串 + 一次 `GET /online`。
  重复调用的真实代价是**同轮/跨轮出现两个不同工单号、话术自相矛盾**，不是"重复建工单"。
- `refund_order` 的重复已被后端去重（`mq.py:109-112` 命中已有 order_id 返回 `duplicate:True`，
  `main.py:78` 有 `UNIQUE KEY uk_order`），不是"重复退款"。
- 成立的部分：tool 分支确实含 `transfer_to_human`；`WRITE_TOOLS` 确实含它；
  `called_write` 幂等集确实是**回合内局部变量**（`agent.py:388`、`:522`），跨回合不保留。

| 路径 | 是否已执行工具 | 处置 |
|---|---|---|
| browse / summarize 纯生成 | **否** | **降级重跑 ReAct（带 tools）** |
| `_run_routed` / `_run_routed_stream` | **是** | **禁止重跑**；改为「重生成一次话术」+ 明确禁止输出工具调用语法；**仍命中则按行剔除 + `end_reason` 记「泄漏」**，重试上限 1 次 |

**为什么不能无脑重跑**：跨回合重跑会重新决策。虽然 `transfer_to_human` 不落库、`refund_order`
有后端去重，但会产出**两个不同工单号 + 自相矛盾的话术**（对用户是可见错误），且这是可避免的。

### 实现约束

1. **`_stream_generate` 是共用函数**：`_pure_generate_stream`（:334）与 `_run_routed_stream`（:375）
   以**完全相同的签名**调它，函数内部无从区分路径。**必须显式加判别参数**（如
   `on_leak="react" | "regenerate"`），不能靠 `messages[-1]` 前缀猜（隐式耦合）。
2. **检测正则不能用 `\b`**：`rules.py:23-27` / `intent_router.py:45-48` 已记录过这条教训——
   Python 正则里中文也算 `\w`，「查`search_products(`」中 `查` 与 `s` 之间**没有词边界**，
   `\b` 匹配不到 → 检测静默失效。改用负向断言 `(?<![A-Za-z0-9_])`（对齐 `ORDER_ID_PATTERN` 的 `(?<!\d)`）。
   判据同时覆盖 JSON 形（`{"name": "search_products"`）与 `<tool_call` 形——
   **本 bug 的 CoT 原文里工具调用恰好是 JSON 形**。
3. **按行缓冲必须有第三条规则**：按 `\n` 切分只发完整行 + 超 N 字符强制 flush，
   **再加上「`stream_events` 迭代结束时 flush 缓冲区剩余部分」**。缺第三条会**静默截尾**
   （回答多半不以换行结尾），且 `if not content` 兜底判不到（截尾不为空）→ 无告警。这是早先版本的硬伤。
4. **降级前必须 pop 掉 guide**：`_pure_generate_stream:333` / `_pure_generate:189` 都是先
   `messages.append(guide)` 再生成。降级重跑时若 guide 仍在上下文（「请反问用户想对比哪些商品」
   「不要直接推荐具体商品」），**与降级目的正相反**，且会污染 session 历史与摘要。
5. **提前中断不能跳过收尾**：如果在 `async for ev in stream_events(...)` 内 `return`，
   则 `trace.add_llm`、空回复兜底、assistant 回填全跳过；usage 挂在**最后一个 chunk**上（`llm.py:113-117`），
   提前 return = 放弃 usage → 该轮 token 记 0。**降级路径必须自己补 usage 与收尾。**
6. **降级后的 trace 归因**：`route_source` 标 `规则降级`，且必须在**所有赋值之后**标
   （`agent.py` 有 6 处 `route_source=` 赋值，收尾赋值会把标注覆盖回「规则」）。
   ⚠️ 同时要改 `observability.py:131` 的「规则路由占比」——它是**精确等值** `r[3] == "规则"`，
   降级轮会被漏算。

### 前端观感（记录，不修）

降级后会有两条并行叙事：`_run_summarize_stream` 已发「准备商品对比」，`_react_loop_stream`
又从「思考中（第 1 步）」重编号。已被截断的前缀话术（「好的我帮您查一下」）无法回收。
这是**已接受的观感代价**，写在这里备查。

---

## 4. P2 · `--effort` 与观测的五条静默失效

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| a | `--effort` 值缺失时**静默跳过**（`idx+1 < len` 为假） | 数据标成 low、实际 high → **评测结论被误标** | 缺值 `sys.exit(1)` + 打印用法 |
| b | `--effort --out low` → `val="--out"` 被当强度发出 | 把 flag 当参数发给服务端 | 加 `startswith("--")` 守卫（`--out` 已有，口径要统一） |
| c | 无取值域校验 → `--effort medium` 服务端映射成 high | **两臂同参** → 得出「推理强度无影响」的假结论 | 白名单 `{none,off,low,high,max}` 校验 |
| d | `--effort` **把评分裁判也切了**（`judge.py` 走同一 `chat_with_usage`） | 两臂**评分标准不同** → 分数不可比（方法学缺陷） | judge 显式钉死 effort，与实验臂解耦 |
| e | 落盘 JSON 只记 `--out` 名字，不记实际生效的 effort | A/B 结果靠文件名承载口径 | summary 写入实际 effort |

**另外两条注释/接口错误**：

- `observability.py` 的动机注释写「'答案为什么这么慢'在看板上完全不可见」——但本批**不做落库**
  前，看板上其实**仍然不可见**。注释必须如实描述范围（改为「CLI / 评测可见；落库见 P2-T3」）。
- `reasoning_ratio` 的注释说「同 `cache_hit_rate` 的处理规范」选 None——但同函数里
  `cache_hit_rate` 无数据时返回的是 **`0.0`**，用 None 的是聚合层 `MetricsStore.snapshot()`。
  **引用错了对象**，改注释。
- `"none"` 字符串 ≠ `None`：CLI 用 `none` 表示"不传参数"，但若有人把
  `REASONING_EFFORT = "none"` 写进 settings，`if not REASONING_EFFORT` 拦不住非空字符串
  → 真的发出 `reasoning_effort="none"`，而 SDK 的 `'none'` 是**合法值**、语义是「不做推理」，
  **与「用服务端默认 high」完全相反且不报错**。修法：`_normalize_effort()` 归一化
  （`""`/`"none"`/`"default"` → `None`）+ 白名单，非法值 raise。

---

## 5. P2 · 推理 token 落库（T3 剩余部分）

已完成的只到 `Trace` 层（`reasoning_tokens` 累计 + `summary()` 两项）。**落库/聚合尚未做**，
所以线上「哪一轮在烧推理」仍看不见。剩余：

1. `trace_store.py`：DDL 加 `reasoning_tokens INT`；**幂等迁移**（`CREATE TABLE IF NOT EXISTS`
   不会给已存在的表加列）。迁移位置必须在 `main.py::_init_db` 的 `TRACES_DDL` 之后；
   用 `SHOW COLUMNS FROM traces LIKE '...'`（连接默认库作用域，天然不跨库误命中，
   比 `information_schema` + `TABLE_SCHEMA=DATABASE()` 更简单）；迁移后 `SELECT ... LIMIT 0` 校验，
   失败**中止启动**（否则 `save()` 吞异常 → 此后每条 trace 静默丢失）。
2. `MetricsStore`：records 元组加列 + `snapshot()` 暴露 `reasoning_token_ratio`。
3. `main.py` 的 trace 详情接口是**按列位置显式拼 dict**，要手动加字段
   （`frontend/src/admin/types.ts` 同步）；否则只能手写 SQL 查。

---

## 6. P3 · 评测集守卫

`tests/cases.py`：

```python
# ROUTING_CASES 新增（纯规则、零成本）
("坑-比较副词误判",  "是成猫了，比较喜欢吃三文鱼相关的", None, None, None),
("坑-比较副词误判2", "幼犬吃什么粮比较好",            None, None, None),
("总结-比较一下",    "帮我比较一下三文鱼猫粮和鸡肉猫粮", "summarize", None, None),
```

⚠️ **第三条的措辞是刻意改过的**：早先版本写「帮我比较一下**这两款**猫粮」——「这两款」是
**指代**，按 `intent_router.py:11` 的既定原则（多轮指代走 LLM）本就该走 LLM。用它做守卫
等于**把一个错误行为锁进 regression lock**，固化下来。改成**自包含措辞**（两个指代对象都点名）。

`REACT_BOUNDARY_CASES` 的 `"幼犬吃什么粮比较好"`：query **保留**（真实用户说法，不回避 bug），
守卫交给上面那条 ROUTING_CASES。按坑 7 的说法，它是**已知失败 case**，不是假通过。

---

## 7. 本批**不做**的

1. **词表治理机制**（TODO.md line 98 ①「词表膨胀后评估权重制」）——需先攒够误判样本，
   现在只有 1 个样本（「比较」），做出来必然是空想。符合项目「数据飞轮」哲学：攒够再系统性修。
2. **Qdrant server 模式**（解后端与评测的文件锁互斥）——要改代码 + 迁数据，独立一条。
   本批只把坑记进 `TODO.md`。
3. **CoT 全文落盘**——实测 CoT 原文**复述了 SYSTEM_PROMPT 条款原文**，落盘等于把系统提示词写进 DB。
   代价诚实记录：**今天能定位根因恰恰因为临时抓了 CoT**，不落盘意味着下次同类问题要多写一次临时脚本。
4. **`REASONING_EFFORT` 全局默认改 `low`**——**挂起决策**：A/B 评测未跑（用户叫停），
   无质量数据支撑。默认保持 `None`（=现状，零行为变更）。此挂起状态**必须记进 `TODO.md`**，
   否则旋钮停在最贵的档谁都不知道。

---

## 8. 验证方式

| 层 | 验证 | 花钱 |
|---|---|---|
| P0 | 单测补：摘要/抽取用真实 usage fixture；端到端跑一次多轮对话，断言压缩后历史真的变短 | 是（少） |
| P1 路由 | `python -c` 直调 `route_by_rule` 断言三元组；跑 `tests/eval_routing.py` | 否 |
| P1 泄漏 | 单测：mock 含泄漏文本的响应，断言不 yield 该行、走对应降级分支、**残留缓冲被 flush** | 否 |
| P2 | 冒烟：`--effort` 五种非法/边界输入各断言一次 | 否 |
| 落库 | 跑一次真实对话，断言 `traces.reasoning_tokens` 非空 | 是（少） |
| 回归 | `test_stream` / `test_agent_defense` / `test_history_trim` / `fuzz_tools` 全绿 | 否 |

⚠️ **本节早先版本太弱，二轮审核已指出**——「断言历史真的变短」抓不到 P0：
`_trim_history` 也能让它变短，变短**不证明摘要成功**。正确断言三条：
(a) 出现 `SUMMARY_PREFIX` 消息（`agent.py:126-129`）；
(b) 摘要正文**保留了压缩区间内的关键实体**（订单号）——这才是摘要区别于丢轮次的设计卖点；
(c) **断言发出去的请求形态**：`messages[-1]` 是 user 指令、请求参数带关思考标记。
(c) 最关键——整个 P0 押在「指令尾置 + 关思考」上，没有任何测试断言它。
`test_history_trim.py:48` 直接 `mock.patch.object(agent, "_summarize")`，连 `_summarize` 内部都测不到。

---

## 9. 二轮审核吸收（2026-09-20）

### 已采纳的修改

| # | 审核发现 | 采纳的处置 |
|---|---|---|
| **B1** | `to_compress` 过滤后可能**只剩一条 user 消息**（`agent.py:110-113` 过滤 `tool`/`assistant(tool_calls)`，但死循环 `:446`/超工具数 `:427`/超步数 `:505`/异常 `:402`、`_run_routed` 异常 `:146/:168`、`_pure_generate` `:196`、`_stream_generate` `:299` 等路径**都不 append 最终 assistant**）→ 尾置后是「user + user」两条连续消息 | **已实测**：退化形态（历史只剩一条 user + 尾置指令）**正常工作**（`completion=33`，摘要正确）。定为**补两形态单测**（历史以 assistant 结尾 / 以 user 结尾），不改设计 |
| **B2** | 修完后若 `extra_body` 管道在服务端不被支持，空摘要守卫会**再次静默触发**，直接回到 P0 原症状 | **采纳**：空摘要时**降级到 `_trim_history`**（`agent.py:65-75`，现成、有单测）——把"静默失败"变成"有界降级"。同时把警告升级（不再只是一行 stderr） |
| **B3** | 哨兵值若用字符串（如 `"default"`），会被 §4 的归一化抹成 `None` → 摘要的 `"off"` **静默失效**，正是我们在修的那类坑 | **采纳**：哨兵用**非字符串对象** `_USE_SETTINGS = object()`，三态写死：哨兵=读 settings / `None`=不传（服务端默认 high）/ `"off"`=关思考 / 其余白名单透传 |
| **S1** | judge 若靠默认哨兵 = 照样跟着 `--effort` 变，等于没解耦 | **采纳**：judge **传具体值**（钉死 `"off"`——评判是机械任务）。并补：judge 同属「无 tools 生成」，§3 的枚举要含它 |
| **S2** | §4c 白名单允许全局设 `"off"` → 主循环失去思考且 temperature 生效，`settings.py:20-24` 实测「答案塌到 62 字符」 | **采纳**：**`"off"` 不进全局取值域、不进 CLI**，只允许按调用点显式传。全局取值域 = `{None, "low", "high", "max"}` |
| **S3** | `add_summary` 不收推理 token、`reasoning_ratio` 分母只含主循环 → **P0 找到的黑洞（每次压缩 300 reasoning）永远不会出现在那一列**；另 `add_summary` 在空摘要守卫**之后**调用，P0 期间摘要调用**完全不在 trace 里** | **采纳**：`add_summary` 也收 `reasoning_tokens`；`推理 token` = 主循环 + 摘要；ratio 的分母同步含摘要。**并记录口径跳变**：修好后「总 token/LLM 次数」会因"以前没记、现在记了"而上升，**别误读成成本上涨**。另：`MetricsStore.records` 新列**必须追加在末尾**（`observability.py:144-153` 与 `snapshot()` 全按位置下标取值，插中间会全线错位） |
| **S5** | 没跑 `low` 中间档，也没评估换非推理模型 | **已实测**（见下表），**选 `off`**。换模型记入待评估 |
| **S6** | §8 验证太弱，且漏了现成抓手 | **采纳**：用 `tests/eval_context_compression.py --real`（`:64` 直接用真实 `_summarize` 跑 `_compress_history`）加断言——它**只打印不 assert**（`:63-92`），**这正是它没抓到 P0 的原因**。另补实现约束：**`_summarize` 签名必须保持 `(history_messages)`**（`test_history_trim.py:30`、`eval_context_compression.py:39` 都用单参 mock，扩签名直接 TypeError）；抽取端到端断言**必须显式 `flush_memory()`**（`memory.py:452-459` 明写 `asyncio.run` 会 cancel 挂起 task，漏了就是假阴性）；§2 最该跑的回归是 `eval_react_boundary.py` 的「ReAct-咨询类误调订单工具」，修完后它应**由 ❌ 转 ✅**，这是「修得彻不彻底」的判据 |
| **S7** | §0 写「共 8 项」但 §7 列了 4 条不做 | 已对齐（§0 的「不做」行补全为 4 项） |
| **S8** | §1 内部数据不一致（`content=''` vs `content='[]'`） | 已统一。另记一条**别改错**：`SUMMARY_PREFIX` 注入文本里的「**以下**是此前对话的摘要」（`agent.py:128`）是**正确的**（摘要正文确实在其后），改「以上→以下」时**别顺手一起改** |

### S5 实测对照（选定 `off` 的依据）

| 方案 | completion | reasoning | 结果 |
|---|---|---|---|
| **`off` + 指令尾置 + 300** | **60** | 0 | ✅ 摘要完整 |
| `low` + 指令尾置 + 600 | 327 | 278 | ✅ 摘要完整（但贵 5.5×） |
| `low` + 不尾置 + 600 | 600（**撞上限截断**） | 572 | ⚠️ 输出断在「用户现有」 |

**为什么不是 `low`**：① 质量无差别而成本高 5.5 倍、延迟更高；② `low` 仍属思考模式 →
`temperature=0.0` **仍是空操作**，而代码注释明写「抽取要确定性」（`memory.py` 的 `temperature=0.0`）
→ `off` 反而**让这个既有意图真正生效**，顺带修好第二个静默失效；③ 不需要额外调预算。

### 需用户拍板的三项 —— 本轮按「按你的设想行动」自行决定

| 项 | 决定 | 理由 |
|---|---|---|
| `none` vs `off` 命名 | `_normalize_effort` 把 `""`/`"none"`/`"default"` 一律归一成 `None`**且不发送** | 从根上消除「字符串 `none` 被当成 SDK 的 `reasoning_effort="none"`（=不做推理）」这个语义反转 |
| `off` 是否进全局 | **不进** | 见 S2 |
| 补跑 `low` / 换模型 | `low` 已用 2 次调用验证并否决；**换非推理模型记入待评估**（`chat_with_usage` 已有 `model` 参数，是一行改动，但要先确认有合适的常驻模型） | 用户已叫停批量评测，不为已否决的方案继续花钱 |
