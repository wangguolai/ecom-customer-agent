# 量化实验 + 压测 + 鲁棒性方案

> 对应 TODO：A2「RRF k 值实验」、A3「后端接口压测」、A4「鲁棒性 fuzzing（不调 LLM 部分）」。
> 三者共同点：**零 API 成本**，产出的是「真数字 / 真 bug」，用来把口头结论换成可量化证据。

## 一、A2：RRF k 值实验

### 1.1 要回答的问题

TODO 深入阶段挂着「RRF k=60 为什么」。现在 `RRF_K = 60` 是抄来的默认值，没实验支撑。
被问「为什么 60」而答「论文推荐」，是背答案；答「我扫了 10/30/60/100，
在我的数据上差异 X，因为 Y」，才是做过。

### 1.2 关键洞察（决定实验怎么设计）

RRF 公式 `1/(k+rank_bm25) + 1/(k+rank_vec)`：

- **k 小** → 头部排名权重差距被放大（rank 0 和 rank 1 分差大）→ 更信任「某一路的头部」
- **k 大** → 各 rank 分差被抹平 → 更接近「两路都出现过就加分」的投票语义

但本项目的链路是 **RRF → 取 top-10 候选 → CrossEncoder rerank → top-3**。
**k 只影响「哪 10 条进候选」，不影响最终排序**（rerank 会重排）。
所以预期：**k 对最终 Recall@3 影响很小，会被 rerank 稀释**。

⇒ 只测完整链路会得到「k 没影响」的假结论（其实是被 rerank 掩盖了）。
**必须同时测纯 RRF 链路（跳过 rerank），才能看出 k 的真实作用。**
这个「k 敏感度被 rerank 稀释」本身就是最有价值的结论。

### 1.3 改动

1. `HybridRetriever.__init__(self, rrf_k=RRF_K)` → 存 `self._rrf_k`，`search()` 里用它
   （模块级 `RRF_K = 60` 保留为默认值，生产行为零变化）
2. `HybridRetriever.search(..., use_rerank=True)` 新增开关。
   正当性：降级路径「rerank 不可用退回 RRF」本来就存在，这个开关只是让它可显式触发，
   便于对比评测，不是为测试而加的后门。
3. `tests/eval_retrieval.py` 加 `--rrf-k-sweep`：对 k ∈ {10,30,60,100} × 链路 ∈ {纯RRF, RRF+Rerank}
   跑 8 组，输出 Recall@3 / MRR 对比表。

**性能**：sweep 时复用同一个 retriever 实例（只改 `_rrf_k`），**不重建 BM25 索引、不重载 embedding 模型**。
30 条 case × 8 组，rerank GPU 约 16ms/次，整体分钟级。

### 1.4 产出

- 对比表落盘 `tests/eval_results/rrf_k_sweep.json`
- k 的物理含义 + 实测敏感度 + 为什么最终仍选 60

---

## 二、A3：后端接口压测

### 2.1 目标

拿真实 P50/P95/P99/QPS，量化两件已有设计的价值：
1. **Redis 缓存值不值**（冷 vs 热的 P99 差距）
2. **限流是不是真的生效**（不是「代码写了」，是「压到阈值真的返 429」）

只压 FastAPI 接口，**不走 agent**（不调 LLM，零成本）。

### 2.2 三档场景

| 档 | 做法 | 看什么 |
|----|------|--------|
| 冷缓存 | 先 `FLUSHDB`，再打请求 | 走 MySQL 的真实延迟；**连接池 maxconnections=5 的排队效应** |
| 热缓存 | 重复打同一批 id | Redis 命中延迟；与冷档对比得出缓存收益倍数 |
| 限流验证 | 10s 内故意打 200 次（阈值 100/10s） | 429 出现在第几次、429 率，证明限流真实生效 |

### 2.3 限流会污染性能压测——怎么绕

read 桶 100 次/10s，性能档很容易撞 429，把 429 的快速失败混进延迟统计会**让 P99 变好看**（假数据）。

处理：
- 性能档**分轮**跑：每轮 90 次请求（< 100 阈值），轮间 `sleep 11s` 让窗口滑过，跑 4 轮 = 360 样本
- 统计时**排除 429 样本**，并单独报告 429 计数（若性能档出现 429 说明分轮策略失效，要报警）
- 诚实标注：**P99 基于 360 样本，样本量偏小，只作冷热相对对比，不宣称是生产级压测结论**

配套正当改动：`ratelimit` 阈值配置外部化（`RL_READ_MAX` / `RL_READ_WINDOW` / `RL_WRITE_MAX` /
`RL_WRITE_WINDOW` 环境变量，默认值 = 现在的硬编码值，**行为零变化**）。
理由和模块 7 的「配置外部化」同规范：阈值硬编码在 `main.py` 里本来就是味道。

### 2.4 预期能压出的真东西

- **连接池 5 条连接是真瓶颈**：并发 50 时冷档请求会在池上排队，P99 明显抬头。
  这是「连接池大小 vs 并发度」的真实体感，比背「连接池是为了复用连接」强。
- 缓存收益倍数（预期 5~20×，看 MySQL 冷启动状态）

### 2.5 实现

`tests/bench_backend.py`，`asyncio + httpx.AsyncClient`（与工具层同栈）。
输出 `tests/eval_results/bench_backend.json` + 终端表格。

---

## 三、A4：鲁棒性 fuzzing（不调 LLM 部分）

### 3.1 定位

按记忆里的规范：**测试要对抗式，构造边界/冲突/异常 case 去找问题，不是证明能跑**。
这一轮的目标是**抓到真 bug**，抓不到才需要解释为什么。

### 3.2 两层靶子

**① `tests/fuzz_backend.py` — 直接打后端 HTTP 接口**

载荷集（每类若干）：超长字符串（10KB）、SQL 注入片段（`' OR '1'='1`、`1; DROP TABLE orders--`）、
路径穿越（`../../etc/passwd`）、null 字节、Unicode 边界（emoji / 零宽字符 / RTL 覆写 / 孤立代理对）、
格式化串（`%s%n`）、超大数、负数、空串、纯空格、XSS（`<script>alert(1)</script>`）、
命令注入（`; ls`）、CRLF 注入（`%0d%0a`）。

靶点：`/orders/{id}`、`/logistics/{id}`、`/products/{id}`、`POST /refund`（body）、`/auth/token`（body）。

断言（失败即 bug）：
- **不返回 5xx**（4xx 是正确行为——正则校验拦下了）
- 不超时
- 响应体不原样回显载荷（防反射型 XSS）
- 跑完 `orders` 表仍在、行数不变（证明 SQL 注入没打穿）

**② `tests/fuzz_tools.py` — 直接调工具层函数（不走 LLM）**

对 `search_orders` / `search_logistics` / `check_stock` / `refund_order` /
`search_products` / `get_return_policy` / `transfer_to_human` 灌同一套脏参数。

断言：
- **工具函数必须返回 str，绝不抛异常**——工具抛异常会穿透 `_react_loop` 炸掉整个 agent 回合
- 返回串不含 `Traceback` / 异常类名泄露

### 3.3 已经预判到的一个真 bug（fuzz 要验证它）

`tools.py` 里 URL 拼接不一致：

- `check_stock`：`f"{BACKEND_URL}/products/{quote(product_id)}"` ← 有 `quote`
- `search_orders` / `search_logistics`：`f"{BACKEND_URL}/orders/{order_id}"` ← **没有 `quote`**

order_id 含空格 / `#` / `?` / 换行时 URL 畸形，httpx 抛 `httpx.InvalidURL`（**不是 `TransportError`**），
`_http_request` 的 `except httpx.TransportError` 接不住 → 异常穿透。

再看调用侧：`agent.py` 的 `_exec` 只 `except (TypeError, KeyError)`，
所以 **ReAct 路径下工具异常会炸掉整个 `chat()`**（对比 `_run_routed` 是 `except Exception`，接得住）。

⇒ 预期 fuzz 能复现这条。修法两处：
1. `tools.py` 三个 URL 拼接统一 `quote(...)`（治标，堵住这一类）
2. `agent.py` 的 `_exec` 把兜底扩到 `except Exception`，返回「工具执行异常」字符串（治本，
   保证任何工具异常都降级成一条 tool 消息，不炸回合）

第 2 条是关键：**工具层是外部输入的边界，边界上不该有「未预期异常能穿透」的路径**。

### 3.4 产出

失败项落 `tests/eval_results/fuzz_report.json`；抓到的 bug 修完重跑至全绿。


---

# 四、plan-reviewer 审核修订（2026-08-28）

## 4.1 A2：k 的作用表述修正 + 防「自证假结论」

**表述错了**：原文「k 只影响候选集、不影响最终排序」说过头了。
准确说法：**k 决定 top-10 候选集的成员，rerank 只能在候选集内重排** ——
所以 k 通过「相关 chunk 是否落在候选集边界」**间接影响**最终 top-3。
正确措辞：*k 的敏感度被 rerank 局部稀释；只有当相关 chunk 处在 k 变动的候选边界附近时，k 才改变最终结果。*
实验设计（纯 RRF vs 完整链路 双组对照）本身是对的，不用改。

**新增防呆自检（审核提的，很关键）**：`hybrid_retriever.py` 第 116 行 RRF 计算用的是
**模块级 `RRF_K`**。若参数化时漏改这一行，sweep 八组测的全是 k=60 → 结果全同 →
得出「k 没影响」的假结论，而且**毫无察觉**。
⇒ sweep 开头强制自检：同一 query 分别用 k=10 / k=100 跑纯 RRF，断言候选集排序**必须有差异**，
否则直接报错退出。**测不出差异的实验，先怀疑实验本身坏了。**

## 4.2 A3：压测的三处污染，原方案只堵了一处

| 污染源 | 原方案 | 修订 |
|--------|--------|------|
| 性能档**内部**撞限流 | 分轮 90 次 + sleep 11s ✅ | 保留 |
| 性能档与限流档**档间**污染（ZSET 残留 90 条 → 限流档第 11 次就 429，不是第 101 次） | ❌ 没考虑 | 限流验证档前显式清 `rl:*`，且放在最后跑 |
| `FLUSHDB` 误伤 | ❌ 会连 `rl:*`（限流计数）和 `mq:*`（在途消息 + 幂等标记）一起清 | 改用 `cache.flush()`（只清 `ecom:*`） |

**「冷档半热」——我原来担心的方向反了**：轮间隔 11s 远小于缓存 TTL（orders/logistics 60s、
products 30s），所以**热档不会变冷**；真正出问题的是**冷档会变热**——第 1 轮打完就有缓存了，
第 2~4 轮其实全是热的，「冷档=走 MySQL」只对第 1 轮成立。

修订后的冷热对照：
- **冷档**：3 个真实订单号，**每次请求前清 `ecom:*`**，串行跑约 30 个样本（小样本，只看 P50/P95）
- **热档**：不清缓存，重复打同一批 id，300 样本
- 诚实标注：冷档是串行小样本，不是并发压测；两档比的是「单请求路径延迟」，不是吞吐

**归因修正**：并发 50 时，FastAPI 同步端点跑 AnyIO 线程池（默认 40），
10 个请求先卡在**线程池**——此时 P99 抬头是「线程池 + 连接池」两个饱和点叠加，
不能单独归因给「连接池 5 条」。⇒ 论证连接池用并发 30（< 40），
另跑一档并发 50 并如实说明是两处叠加。

## 4.3 A4：fuzz 载荷集的关键修正

原方案说 order_id 含 `#` / `?` 会触发 `httpx.InvalidURL` —— **错了**。
`#` 和 `?` 在 httpx 的 ALLOWED_CHARS 里，不会抛异常，只会被解析成 fragment / query，
**静默改变请求语义**（这本身也值得记一笔，但不是崩溃）。

**真正触发 `InvalidURL` 的是空格 / 换行 / 控制字符。**
⇒ 载荷集必须**确保含空格、`\n`、`\r`、`\t`、`\x00` 变体**，否则这一轮根本抓不到那个 bug，
还会误以为「代码是好的」。

推理链其余部分经审核读码验证**成立**：
`InvalidURL` 是 `HTTPError` 直接子类、**不是** `TransportError` 子类 → 穿透 `_http_request`；
`_exec` 只 `except (TypeError, KeyError)` → 炸掉整个 `chat()`。

**「治本」标错了层**：原方案把「`_exec` 扩 `except Exception`」叫治本，
但它只保护 agent 循环——`fuzz_tools.py` 直接 `await TOOL_MAP[name](**args)`，
不经过 `_exec`，工具内异常照样炸。
⇒ 两处都要修，且认清各自职责：
- `tools.py` 统一 `quote()` + 自身兜底 = **工具层是外部输入的边界，边界上不该漏异常**（真治本）
- `agent.py` `_exec` 扩 `except Exception` = **循环层兜底，保证任何工具异常都降级成一条 tool 消息**（防炸回合）

