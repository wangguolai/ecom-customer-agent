# 管理后台（Admin Console）设计方案

> 状态：**阶段 2 修订稿**（已过 plan-reviewer 审核，18 条意见 + 4 项用户决策已吸收）
> 范围：**L2**——只读查看 + 操作能力。4 模块（退款工单 / 订单物流 / 用户画像 / 运行指标）+ 登录 + 路由守卫
> 技术栈：复用 `frontend/`（React 18 + Vite 6 + TS），引入 `react-router-dom`（钉 v6.x，React 18 兼容）

---

## 〇、为什么做 / 交付形态（已被用户拍板收窄）

**用户诉求**：起一个能看数据库数据的后台。

**交付形态（2026-09-18 用户拍板）**：**本机 localhost，不对外**。不改 Dockerfile、不做部署。

⚠️ **这直接收窄了本方案的价值定位**，必须写明避免自欺：

| | |
|---|---|
| 原设想 | 「补上 09-16 面试缺失的『能点的链接』」 |
| **实际** | **自用工具 + 面试素材**（可截图/现场演示，但别人点不到） |

09-16 那次面试的死因之一是「图 → 视频 → 链接，最后一环缺」——本机 localhost **不解决这个问题**。用户已知悉并选择此范围（「后面再补也行」）。部署另开一单（`TODO.md` 已记）。

**仍然成立的价值**：① 自用——直接看画像是否真的沉淀了、工单状态对不对；② 面试素材——能演示「我做过管理后台」；③ 前后端联调、鉴权、分页、状态机这些**考点**照常落地。

**前置事实**（已核对）：
- 数据在**本机 MySQL 3306**（`.env` 口径），Docker 的 3307 未监听、容器未跑
- 零成本替代方案已存在：DBeaver 看原始表、`/docs` 调接口。**本方案是在它们之上做「能看能点的界面」**
- `frontend/dist` 是 **gitignored**（`.gitignore:52-56`），必须本地 `npm run build`

---

## 一、目标与非目标

### 目标
1. 登录后进后台，未登录跳登录页（**路由守卫**）
2. **退款工单**：列表 + 状态筛选 + 审批/执行（走退款状态机）
3. **订单/物流**：列表 + 搜索 + 详情（含物流轨迹）
4. **用户画像**：按用户分组的列表 + 详情 + 删除
5. **运行指标**：技术成功率 / P50-P95-P99 / 平均 token / 缓存命中率
6. 与聊天界面共存（`/` 聊天，`/admin/*` 后台），复用同一套构建

### 非目标
- ❌ 用户注册/多账号/权限分级（只有 admin 单角色）
- ❌ 数据编辑（除退款审批/画像删除外只读——**避免造出第二个写入口**）
- ❌ 实时推送（轮询/手动刷新，数据量小）
- ❌ 移动端适配
- ❌ 部署 / Dockerfile 改造（用户已定：本机）
- ❌ 审计日志界面（后端无审计表）

---

## 二、鉴权设计（本方案核心）

### 2.1 现状
- 手写 HS256 JWT（`backend/auth.py`）+ `auth.require_role("admin")`
- `POST /auth/token` 签发（独立 auth 桶，**5 次/60s**）
- `/refund/{id}/review`、`/refund/{id}/execute`、`DELETE /memory/{id}` 已挂 admin
- **所有读接口（`/orders/{id}` 等）无鉴权**，挂 read 桶

### 2.2 鉴权边界：列表接口挂鉴权——**但先说清「不能用什么理由」**

❌ **站不住的理由**（方案初稿写过，审核推翻）：
> 「单条查询要猜 ID（有枚举成本），列表直接 dump 全量」

**错**：订单号是 `20` 开头的 11 位**半序列号**（`seed.py` 里就是顺序排的），枚举成本极低；且 `/orders/{id}` 本来就公开 → **数据本来就能全量获得**。用这个理由，面试一追就崩。

✅ **真实理由**（两条，都要说）：
1. **工程示范**：后台接口按「后台会话鉴权」的标准做法实现，体现的是「权限粒度匹配数据暴露面」的设计意识，**不是假装在保护数据**
2. **IDOR 未修**（`architecture.md` §7 缺口 #1）：单条接口无身份校验是**已知缺口**，后台不在这之上叠新接口

**据此（用户 2026-09-18 拍板）——详情也移进 `/admin`**：
`/admin/orders/{id}`、`/admin/logistics/{id}` 走 admin 鉴权。
**代价**：聊天链路不能复用这两个接口，前端要调两套（聊天页走公开接口、后台走 admin 接口）。这是「逻辑自洽」换来的成本，接受。

| 接口 | 现状 | 本方案 |
|---|---|---|
| `/orders/{id}` 等单条 | 无鉴权 | **不动**（聊天链路依赖） |
| `/admin/**` 全部 | — | **新增，挂 admin** |
| `GET /memory` | 内部无鉴权 | **改挂 admin**（已全仓 grep：**无任何调用方**，agent 检索走本地 Qdrant 不经 HTTP） |
| `POST /memory` | 无鉴权 | **不动**（agent 进程调用无 token；已有服务端敏感复校 + 白名单 + 桶） |

### 2.3 前端 token 存放：`sessionStorage`

| 方案 | XSS 可读 | 持久性 | 结论 |
|---|---|---|---|
| `localStorage` | ✅ | 永久 | ❌ 后台 token 不该永久驻留 |
| `sessionStorage` | ✅ | 关标签页即清 | ✅ **采用** |
| httpOnly Cookie | ❌ | 可控 | 最安全，但要防 CSRF + 后端改造（本项目是 Authorization 头方案）——**二期** |

**三条必须写进文档的诚实口径**（审核补充）：
1. ✅ **真实收益**：Authorization 头方案**天然免 CSRF**（浏览器不会自动带上自定义头）——这才是选它的实际好处
2. ⚠️ **防不住 XSS**：能读 DOM 就能读 sessionStorage。真正的解法是 httpOnly Cookie。**这是权衡不是银弹**
3. ⚠️ **登出只是前端删 token**：服务端无状态，token 在 TTL（1h）内**仍然有效**，curl 可复现（`architecture.md` 已列为缺口）
4. ⚠️ **sessionStorage 是 per-tab**：开新标签页要重新登录
5. ⚠️ 不引第三方 CDN 脚本（放大 XSS 面）

### 2.4 请求链路与契约（审核修正）

```
LoginPage → POST /auth/token {username, password}
          ← {"access_token": "...", ...}      ⚠️ 字段名是 access_token，不是 token
          → sessionStorage.setItem('admin_token', access_token)
          → navigate('/admin/refunds')
```

**`api.ts` 统一处理（不在每个页面重复）**：
| 状态码 | 动作 |
|---|---|
| 401 | 清 token + 跳登录（**并发请求需幂等**，避免多次跳转；**登录页自身的请求要排除**，否则死循环） |
| 403 | **不清 token**（已登录但权限不足，与「未登录」是两回事） |
| 429 | **不清 token、不跳转**（只是限流，提示重试即可） |

**已知开发/演示风险**：登录桶 5 次/60s，调试时频繁登录会**把自己锁死**（`test_auth.py` 专门写了清桶函数，证明是真痛点）。开发期改 `RL_AUTH_MAX` 环境变量。

### 2.5 路由守卫
`<RequireAuth>` 包裹后台路由：无 token → `<Navigate to="/admin/login" replace />`。

**必须写进注释**：这只是**前端体验层**的守卫，真正安全边界在**后端鉴权**。前端守卫可被绕过（改 JS / 直接 curl）。验证方式：**清掉 token 直接 curl `/admin/orders`，必须 401**。

---

## 三、后端改动

### 3.1 新增接口（统一 `/admin` 前缀）

**限流**：新增独立桶 `_limit_admin`，阈值 10s/100，走 `_int_env("RL_ADMIN_WINDOW"/"RL_ADMIN_MAX")` 外置规范。
⚠️ 桶 key 里的 client 硬编码 `"demo"` → **这是全局共享配额，不是 per-IP/per-user**（`ratelimit.py:42`），文档要写明。
⚠️ `GET /memory` 改鉴权后**挪到 admin 桶**——否则与聊天链路的 `POST /memory` 共用 `_limit_memory`，违背故障隔离原则。

**🔴 依赖顺序：`require_role` 必须写在限流参数之前**。
现网 `/review` 是「限流在前、鉴权在后」，匿名洪泛会先消耗配额；后台反过来，让**未认证流量不消耗 admin 桶**。

| 接口 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `GET /admin/orders` | `q?`、`page`、`size` | 分页 | `q` 精确匹配订单号 / 模糊匹配商品 |
| `GET /admin/orders/{order_id}` | — | 订单 + 物流聚合 | traces 为空返 `[]`（**不 404**） |
| `GET /admin/refunds` | `status?`、`page`、`size` | 分页 | 字段需支撑审批决策，见 3.3 |
| `GET /admin/users` | `page`、`size` | 用户级列表 | active 数 + **总数**（见 3.4） |
| `GET /admin/metrics` | — | MetricsStore 快照 | 统一 `/admin` 前缀 |
| `GET /memory` | `user_id` | 画像详情 | **改挂 admin + admin 桶** |

### 3.2 分页（审核列的四个坑，逐个处理）

| 坑 | 处理 |
|---|---|
| 无 `ORDER BY` → 翻页重复/漏行 | **必须显式排序**（如 `ORDER BY created_at DESC, order_id DESC`） |
| `total` 与列表**过滤条件漂移** | 两次查询但**共用同一个 WHERE 构造**（抽成变量） |
| `page: int` 非数字返 **422 不是 400**；负数不拦 → `LIMIT -1` 直接 500 | 用 `Query(ge=1, le=100)` 约束；测试按 **422** 断言 |
| `LIKE %...%` 通配符未转义（`q=%` 拉全量） | **转义 `%`/`_`** 后再拼；且 `q` 为纯数字时走 `order_id = %s` 精确命中 |

`size` 上限 100（防 `?size=999999` dump 全表）。

### 3.3 退款工单（审核指出字段不足）

`refunds` 表只有 `ticket_id / order_id / amount / status`（`main.py:58`）——**审批人看不到时间/商品，无法决策**。

处理：
1. **补 `created_at`**（该表启动时 DROP 重建，**零迁移成本**）
2. `GET /admin/refunds` 返回**订单商品名 + 金额 + 创建时间 + 状态**
3. `status` 值**是中文**（`rules.py:136-139`：`待人工审批/已批准/已退款/已拒绝`），筛选参数按此枚举校验
4. ⚠️ **`refunds` 表重启即清空** → 测试/演示用例需**前置造数**

### 3.4 用户画像页语义（审核指出自相矛盾）

方案初稿 §三 是「用户级列表（GROUP BY user_id）」，§七 却断言「该条消失」（记忆级）——**两级混淆**。

修正为两级：
```
/admin/users            → 用户列表：user_id + active 条数 + 总数 + 最后更新时间
/admin/users/{uid}      → 该用户记忆列表（含已 superseded/deleted，标注状态）+ 删除按钮
```
⚠️ **只统计 `active` 会让「删光记忆的用户」整行消失**，反而无法验证删除 → **active 数和总数都要显示**。

⚠️ **画像列表大概率全是 UUID**：`user_id` = JWT sub，无 token 时回落 `session_id`，而聊天页无登录 → UI 必须标注「会话标识（回落到 session_id 的口径）」，否则看起来像 bug。

### 3.5 `GET /admin/metrics` 与 MetricsStore 补齐（审核阻塞项）

**现状问题**：`MetricsStore.record()`（`observability.py:88-90`）只存 `(总耗时, 总token, 结束原因, 路由来源)`——**Trace 的 cache_hit/miss token 在此丢弃**；`summary()` 只有 P99 + 平均延迟，**无 P50/P95、无缓存命中率**。所以原方案「加个 snapshot() 就行」是**做不到的**。

**必须先补齐**：
1. `record()` 增存 cache hit/miss token
2. 补 P50 / P95 计算
3. 加 `snapshot()` 供 `/metrics` 序列化
4. **历史样本缺失的字段显示 `—`，不显示 0**（否则是假数据）

**返回体必须标注的三条诚实边界**：
- `scope: "single-process"` —— MetricsStore 是 `src/agent.py:487` 的**进程内单例**，多 worker 不聚合
- `sample_source: "web"` —— **只有 `/chat/stream` 路径写它**，CLI/评测跑的对话不计入
- `sample_since` —— 重启清零

（`records` 是无界 list，可加环形缓冲；`summary()` 的中文键当 API 契约别扭，加英文字段映射。）

### 3.6 SPA fallback（🔴 落点必须精确）

```
🔴 `/` 路由在 main.py:142，位于所有 API 之前（FastAPI 按注册顺序匹配）
   在它那里加 catch-all → /orders、/logistics、/products、/online、/refund、
   /chat/stream 全部返回 index.html（整条 API 被吞）
```

**正确做法**：只加两条，放在现有路由之后
```python
@app.get("/admin")
@app.get("/admin/{path:path}")
```
（若将来确需 catch-all，必须放文件**最末尾**并显式排除 `/docs`、`/openapi.json`、`/assets`、`/product_images`）

**两个必补细节**：
1. **dist 缺失时不能回落 `web/index.html`**——legacy 页没有后台，会**白屏**。应返回明确 404 + 「请先 `npm run build`」
2. ⚠️ **`/assets` 是 import 期条件挂载**（`main.py:137-139`）：先起后端再 `npm run build`，JS 会 404 白屏，**必须重启后端**

---

## 四、前端改动

### 4.1 结构
```
frontend/src/
├── main.tsx                 # 改：包 BrowserRouter，路由分流
├── App.tsx                  # 聊天界面（挪到路由 /）
├── admin/
│   ├── AdminApp.tsx         # 后台路由表
│   ├── AdminLayout.tsx      # 侧边栏 + 顶栏 + 登出
│   ├── LoginPage.tsx
│   ├── RequireAuth.tsx
│   ├── api.ts               # token 注入 + 401/403/429 分流
│   ├── types.ts
│   ├── admin.css            # 统一 .admin- 前缀
│   └── pages/{Refunds,Orders,Profiles,Metrics}Page.tsx
└── （现有聊天组件不动）
```

**路由表**：
```
/                 → App（聊天，现有）
/admin/login      → LoginPage
/admin/*          → RequireAuth + AdminLayout
  /admin/refunds  → 默认落地页（唯一带写操作 + 状态机 + 鉴权，演示价值最高）
  /admin/orders
  /admin/users    → 用户列表 → /admin/users/:uid 记忆详情
  /admin/metrics
```

### 4.2 `vite.config.ts`（🔴 审核阻塞项）

现状只代理了 `/chat`、`/product_images`（`:12-18`）——**`/admin`、`/auth`、`/orders`、`/logistics`、`/memory` 全无代理，dev 模式请求根本到不了后端**。

**必须补**：代理 `/admin`、`/auth`、`/memory`（或统一用 `VITE_API_BASE`）。

### 4.3 样式（审核修正）
⚠️ 「复用现有 CSS 变量」**不成立**——`styles.css` 里**没有 `:root`/`var()`**；且它是**全局引入**（`main.tsx:4`）并含 `.header`/`.menu`/`.chat` 等**通用类名**。
→ 后台样式**统一 `.admin-` 前缀**，避免与聊天页样式互相污染。

---

## 五、改动文件清单

| 文件 | 改动 | 类型 |
|---|---|---|
| `src/backend/main.py` | 6 个 `/admin/*` 接口 + `_limit_admin` 桶（鉴权在前）+ `GET /memory` 改鉴权并挪桶 + `refunds` 表补 `created_at` + SPA fallback 两条路由 | 改 |
| `src/infra/observability.py` | `record()` 存 cache token + P50/P95 + `snapshot()` | 改 |
| `frontend/package.json` | 加 `react-router-dom`（**钉 v6.x**）；`package-lock.json` 一并更新 | 改 |
| `frontend/vite.config.ts` | **补代理**（`/admin`、`/auth`、`/memory`） | 改 |
| `frontend/src/main.tsx` | 包 `BrowserRouter` + 路由分流 | 改 |
| `frontend/src/admin/*` | **新建 12 个文件** | 新 |
| `docs/architecture.md` | 后端横切线加 `/admin` 组 + 前端结构 + **§7 缺口表**（本次引入「列表已鉴权、单条仍公开」的新不一致） | 改 |
| `docs/user-memory-plan.md` | 同步 `GET /memory` 改鉴权的口径（`:255`） | 改 |
| `references/INTERVIEW_POINTS.md` | 加管理后台考点（**只记问句**，规则要求） | 改 |
| `TODO.md` | 更新 `:27` 实测项 #16 口径 + 部署单独立项 | 改 |
| `tests/test_admin_api.py` | **新建** —— 见 §七 | 新 |
| `tests/fuzz_backend.py` | 写死的路由清单纳入 `/admin/*` | 改 |

**明确不改**：`src/agent.py`、`src/memory.py`、`src/tools.py`、`src/intent_router.py`、聊天前端组件、`Dockerfile`。

---

## 六、实施步骤

1. `MetricsStore` 补齐（先做——指标页依赖它）
2. `refunds` 表补 `created_at`
3. 后端 6 个 `/admin/*` 接口 + `_limit_admin`（鉴权在前）+ `GET /memory` 改鉴权挪桶
4. SPA fallback 两条路由
5. 后端测试（零成本）
6. 前端脚手架（装 router + 路由分流 + **vite proxy** + build 验证）
7. 鉴权链路（LoginPage + api.ts + RequireAuth + 401/403/429 分流）
8. 4 个页面（退款 → 订单 → 画像 → 指标）
9. 端到端验证（§七）
10. 文档同步 + 回归

**每页验收含 `npm run build` 通过**（`tsc --noEmit` + strict + `noUnusedLocals` 类型门）。

---

## 七、验证标准（对抗式）

### 7.1 鉴权（本次最大回归风险）
| # | 用例 | 期望 |
|---|---|---|
| 1 | **清 token 后 curl `/admin/orders`** | 401（证明前端守卫不是安全边界） |
| 2 | **`GET /memory` 无 token** | 401；**且 `POST /memory` 仍可用**（改鉴权不能误伤 agent 写入） |
| 3 | role=user 的 token 调 `/admin/*` | **403 不是 401**（对齐 `test_auth.py:246-256`） |
| 4 | 伪造/过期 token | 401，前端清 token 跳登录 |
| 5 | 429 | **不清 token、不跳转** |
| 6 | 无 token 洪泛 `/admin/*` | **不消耗 admin 桶**（鉴权在前） |
| 7 | 登录失败 | 401 统一话术，不区分「用户不存在/密码错」 |

### 7.2 SPA / 静态
| # | 用例 | 期望 |
|---|---|---|
| 8 | 登录后**刷新** `/admin/refunds` | **不 404**（SPA fallback） |
| 9 | fallback **不吞** `/docs`、`/openapi.json`、`/product_images`、`/assets` | 各返回自身内容 |
| 10 | 未知 API 路径（如 `/admin/nope`） | 404 JSON（不是 index.html） |
| 11 | dist 缺失 | 明确 404 提示 build，**不白屏** |

### 7.3 分页 / 查询
| # | 用例 | 期望 |
|---|---|---|
| 12 | `?size=999999` | 被上限截断 |
| 13 | `?page=abc` | **422**（不是 400） |
| 14 | `?page=-1` / `size=0` | 400/422，**不是 500**（防 `LIMIT -1`） |
| 15 | `q=%`、`q=_` | 不被当通配符（拉全量） |
| 16 | `q=` 注入串 / 超长 / 中文 | 不崩、不注入 |
| 17 | 多页遍历 | 无重复、无漏行（验证 ORDER BY） |

### 7.4 业务
| # | 用例 | 期望 |
|---|---|---|
| 18 | pending → approved → refunded | 状态机正常 |
| 19 | 重复执行同一工单 | 条件 UPDATE 拦住，不重复退款 |
| 20 | 终态再审批 | 409 |
| 21 | **删画像后检索不再命中** | ⚠️ 见下 |
| 22 | 连续打 `/admin/*` | `/refund` 不受影响（独立桶） |
| 23 | 空数据（无工单/无画像） | 空态，不白屏 |
| 24 | 订单无物流（seed 只有 1 单有物流） | 显示「暂无物流」，**不是 404 报错** |

**⚠️ 用例 21 是被审核抓到的真 bug 的回归**：最初 `DELETE /memory` 只软删真源不删 Qdrant，而 `retrieve()` 只查 Qdrant → **删掉的画像仍会被注入对话**。
- 已修：删除时同步 `delete_by_memory_ids`（point id == memory_id，不需要 embedding）
- **断言必须是「检索结果里不再出现」，不能只断言「列表里消失」**——后者会测试全绿而功能坏

### 7.5 回归
`test_router_dirty_input`（5/5）/ `test_history_trim`（4/4）/ `test_agent_defense` / `test_memory`（53/53）/ `fuzz_backend`（路由清单更新后）/ `test_auth`（39 项）

---

## 八、风险与待决

| # | 项 | 说明 |
|---|---|---|
| 1 | **工程量最大的一块** | 后端 6 接口 + MetricsStore 改造 + 前端 12 个新文件 + 路由/鉴权/布局全新建 |
| 2 | **SPA fallback 落点** | 加错位置会吞掉整条 API（已在 §3.6 明确） |
| 3 | **vite proxy 缺失** | dev 模式请求到不了后端（已在 §4.2 明确） |
| 4 | **`/assets` 条件挂载** | 先起后端再 build 会白屏（已在 §3.6 标注） |
| 5 | **登录桶 5/60s** | 开发/演示会被自己锁死 |
| 6 | **token 存 sessionStorage 防不住 XSS** | 是权衡不是银弹（已在 §2.3 写明） |
| 7 | **登出后 token 1h 内仍有效** | 服务端无状态，`architecture.md` 已列为缺口 |
| 8 | **`POST /memory` 仍无鉴权** | agent 内部调用无 token；已有敏感复校 + 白名单 + 桶兜底，公网前必须处理（`TODO.md`） |
| 9 | **指标只反映单进程 + 只统计 Web 链路** | 返回体标注（§3.5） |
| 10 | **`AUTH_SECRET` 是仓库公开的 demo 值** | 对外演示前必须换（`auth.py:85-86` 已告警） |
| 11 | **与「不加新功能」节奏冲突** | 第三次，已点明，用户选择继续 |
| 12 | **前端技术栈迁移度未知** | 用户是 Cocos 游戏前端出身，React 生态熟悉度未确认——若吃力，降样式复杂度保功能链路 |

---

## 九、二期预留

- 部署（Dockerfile 多阶段构建 / 内网穿透）——「能点的链接」的真正解法
- httpOnly Cookie + CSRF 的鉴权方案
- 指标多 worker 聚合
- 会话查看页（Redis session 列表）
- Trace 落盘 + 查看页（依赖 `TODO.md` 的「trace 落盘」）
- 画像审计回滚界面（依赖画像二期）
