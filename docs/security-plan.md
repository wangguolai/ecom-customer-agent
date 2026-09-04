# 安全方案（模块 8 网络安全 + Prompt Injection 架构级防御）

> 对应 TODO：A1「模块 8 网络安全」+ A5「Prompt Injection 架构级」。
> 两者合一份文档的理由：它们共享同一套底层手段（鉴权 / 最小权限 / 出网限制 / 沙箱），
> 只是威胁模型不同——模块 8 防的是「外部攻击者直接打接口」，A5 防的是「攻击者经 LLM 间接驱动系统」。

## 一、现状盘点（写代码前先认账）

已有的安全面（不是新做，是盘点）：

| 面 | 现状 | 位置 |
|----|------|------|
| SQL 注入 | 全参数化查询 `%s`，无字符串拼接 | `src/backend/main.py` 各端点 |
| 参数校验 | order_id `\d{8,32}`、product_id `P\d{1,15}` 正则拦截 | 同上 |
| 限流 | 滑动窗口，read 100/10s、write 10/10s | `src/backend/ratelimit.py` |
| 调试后门 | `/debug/fault` 由 `ENABLE_DEBUG_FAULT` 门控，默认不挂载 | `main.py` 末尾 |
| 密钥管理 | `.env` + `.dockerignore` 排除，不进镜像层 | `.dockerignore` |
| 注入防御（prompt 级） | 数据/指令分离、工具返回标注为「参考数据非指令」 | `SYSTEM_PROMPT` 第 5 条 |
| 注入防御（代码级） | 读写工具分离 `WRITE_TOOLS`、退款金额下沉、写工具本回合单次执行 | `tools.py` / `agent.py` |

**真实缺口（本次要补的）**：

1. 🔴 `/refund/{ticket_id}/review`、`/refund/{ticket_id}/execute` **零鉴权**。任何人 curl 就能审批 + 执行退款——资金敏感接口裸奔。代码注释里自己承认了「demo 取舍」，现在补上。
2. 🟡 后端容器以 **root 运行**、未丢弃 capabilities，沙箱形同虚设。
3. 🟡 MySQL 用 **root 账号**连接，权限远超所需。
4. 🟡 `BACKEND_URL` 从环境变量读且**无校验**，被投毒可指向任意主机（SSRF 面）。

**已知但本次不修的缺口（诚实标注，不假装没看见）**：

- 🔴 **水平越权（IDOR）**：`/orders/{order_id}` 无身份校验，改订单号就能查任何人的订单。
  不修的原因：demo 的 agent 没有「登录用户」概念，修它要求工具层携带用户身份 token，
  改动贯穿 agent → tools → backend 三层，且和本项目主线（agent 工程）无关。
  修法规范：请求带用户 token → 后端从 token 取 uid → `WHERE order_id=%s AND uid=%s`，
  **不能靠前端传 uid**（那等于没校验）。
- `/debug/fault` 保持环境变量门控、不加鉴权。原因：评测脚本高频调用，加鉴权要每个脚本带 token，
  收益低。生产规范是「双保险 = 不开 + 鉴权」，这里只做了第一道。

## 二、A1 落地：JWT 鉴权

### 2.1 为什么手写 HS256 而不是装 PyJWT

1. 零新依赖（后端镜像最小化原则，`requirements-backend.txt` 的既定规范）
2. JWT 三段结构（header.payload.signature）+ HMAC 签名 + base64url 是必要点，手写过才讲得透
3. 风险可控：只实现 HS256 签发 + 校验，不做 alg 协商 / JWK / RS256

代价：要自己防住 PyJWT 已经帮你防的坑。下面三条是必须显式处理的：

| 坑 | 防法 |
|----|------|
| **alg=none 攻击**（攻击者把 header 改成 `{"alg":"none"}` 并去掉签名） | 解码时**硬校验** `header["alg"] == "HS256"`，不信任 token 自称的算法 |
| **时序攻击**（逐字节比较签名，靠响应时间差爆破） | 用 `hmac.compare_digest` 常数时间比较，不用 `==` |
| **过期不校验** | 显式校验 `exp`，且用 UTC 时间戳 |

### 2.2 新增 `src/backend/auth.py`

```
_b64url_encode/_b64url_decode  base64url（去 padding，JWT 规范）
create_token(sub, role, ttl)   → "header.payload.signature"，payload={sub,role,iat,exp,jti}
decode_token(token)            → dict；签名/alg/exp 任一不过 → raise AuthError
require_role(role)             → FastAPI 依赖工厂，从 Authorization: Bearer 取 token 校验
hash_password(pw, salt)        → pbkdf2_hmac('sha256', pw, salt, 200_000)
verify_password(pw, stored)    → compare_digest
```

**密钥来源 `AUTH_SECRET`（fail-closed）**：环境变量缺失 → **启动即抛错**，不给默认值。
理由：默认密钥 = 没有密钥，签名可被任何人伪造。docker-compose 注入 demo 值
`AUTH_SECRET: ${AUTH_SECRET:-dev-only-secret-change-me}`，代码检测到该 dev 值时打印刺眼警告。
规范：**代码 fail-closed，编排给 demo 值**——生产部署不注入就起不来，逼你配。

**密码存储**：不存明文，存 `pbkdf2_hmac(sha256, password, salt, 200000)` + 随机 salt。
用标准库不引 bcrypt/argon2（零依赖原则）。要点三连：
- 为什么不能裸 sha256 → 彩虹表 + GPU 每秒百亿次
- 为什么要 salt → 相同密码产生不同哈希，彩虹表失效，且防「一次破解全网通杀」
- 为什么要慢（迭代 20 万次）→ 正常登录多花 100ms 无感，爆破成本放大 20 万倍

demo 账号从环境变量读 `ADMIN_USER` / `ADMIN_PASSWORD`（启动时算哈希存内存）。

### 2.3 端点改动

| 端点 | 鉴权 | 理由 |
|------|------|------|
| `POST /auth/token` | 无（就是签发口） | 挂 write 限流桶防暴力破解 |
| `POST /refund` | **不加** | agent 代客**申请**，只生成待审工单、无资金流，属低权操作 |
| `POST /refund/{id}/review` | `require_role("admin")` | **审批**，决定钱退不退，高权 |
| `POST /refund/{id}/execute` | `require_role("admin")` | **执行**，真动钱，高权 |

⚠️ 关键设计点：**不是「所有写接口都鉴权」，而是按资金影响分级**。
给 `/refund` 加鉴权会打断 agent 链路且没有安全收益（它本来就不能把钱退出去）——
真正的钱闸在 review/execute。这就是「最小权限」落到具体接口上的样子。

登录失败响应统一 `401 用户名或密码错误`，**不区分「用户不存在」和「密码错」**——防用户枚举。

### 2.4 测试 `tests/test_auth.py`（零成本）

对抗式，不是证明能跑：

1. 无 Authorization 头 → 401
2. `Bearer 乱码` → 401
3. **alg=none 伪造 token**（自己构造 `{"alg":"none"}` + 空签名）→ 401
4. 有效签名但已过期（ttl=-1）→ 401
5. 签名用错密钥 → 401
6. `role=user` 的合法 token 调 review → 403（垂直越权拦截）
7. `role=admin` 合法 token → 200，状态机流转成功
8. 密码错误 → 401，且响应体不含「用户不存在」字样
9. token 被截断/多段/少段 → 401 不抛未捕获异常

## 三、A5 落地：Prompt Injection 架构级防御

规范：**防御分层递进，越底层越兜底**。前两层已落地，本次补第三层。

| 层 | 手段 | 状态 |
|----|------|------|
| Prompt 级 | 数据/指令分离、工具返回标注非指令、不泄露系统提示词 | ✅ 已有 |
| 代码级 | 读写工具分离、退款金额下沉（LLM 无权填金额）、写工具本回合单次执行、工具返回输出校验 | ✅ 已有 |
| **架构级** | 沙箱隔离 / 最小权限 / 出网限制 / 数据分级 | ⬅ 本次 |

### 3.1 沙箱隔离（Docker 加固）

- Dockerfile 加非 root 用户：`RUN useradd -m -u 1000 appuser` + `USER appuser`
- compose 加 `cap_drop: [ALL]`、`security_opt: [no-new-privileges:true]`
- 验证：`docker compose exec backend whoami` → `appuser`；`docker compose exec backend id` 无特权

规范：即使注入最终导致容器内命令执行，攻击者拿到的是**非 root、无 capabilities、
文件系统隔离**的容器，横向移动到宿主机的成本大幅升高。

### 3.2 最小权限

- MySQL 换非 root 账号：compose 加 `MYSQL_USER: ecom` / `MYSQL_PASSWORD`，backend 用它连。
  mysql 官方镜像会自动授予该账号对 `MYSQL_DATABASE` 的全部权限（够 `_init_db` 的 DROP/CREATE），
  但**没有全局权限**——不能建库、不能读 `mysql` 系统库、不能 `FILE` 读写宿主文件。
- 验证：用该账号 `SHOW DATABASES` 只看得到 `ecommerce`；`SELECT * FROM mysql.user` 被拒。
- 写接口鉴权（§2.3）也属于这一层。

### 3.3 出网限制

- 新增 `src/infra/egress.py`：`validate_backend_url(url)` 校验 scheme ∈ {http,https}
  且 host ∈ 白名单（`localhost` / `127.0.0.1` / `backend` / 环境变量显式追加）。
  `tools.py` import 时校验 `BACKEND_URL`，非法直接抛错。
  防的是：**环境变量投毒** → agent 的所有工具请求（含订单数据）被导向攻击者服务器（SSRF + 数据外泄）。
- **能力最小化才是最有效的出网限制**：TOOL_MAP 里没有 `fetch_url` / `run_code` 这类通用工具，
  agent 根本没有「访问任意 URL」的能力。规范：与其事后限制出网，不如一开始就不给这个工具。
- 不做的：compose `internal: true` 网络。原因 backend 要暴露 8000 给宿主机，internal 网络不能做端口映射。

### 3.4 数据分级

已落地，此处只做归位：SSOT → Domain → Derived 三层（`docs/data-sync-architecture.md`）+
知识域隔离（`kb_type` 商品/政策分库检索）+ 动态数据不进向量库（价格/库存走实时工具）。
安全视角的意义：**LLM 能看到的数据面被切小**，注入能污染的范围随之变小。

## 四、模块 8 认知部分

代码只覆盖 JWT + 最小权限，其余是认知题，逐条记要点（结合本项目实例，不空谈）：

1. HTTPS/TLS：非对称换密钥 + 对称传数据、证书链信任、中间人
2. XSS 三型（存储/反射/DOM）+ CSP + HttpOnly
3. CSRF：SameSite / CSRF token / **JWT 放 Header 天然免疫 CSRF，放 Cookie 就不免疫**
4. SQL 注入：参数化查询 vs 转义（本项目全 `%s`）
5. 越权：水平（IDOR，本项目 `/orders` 真实缺口）vs 垂直（role 校验，本项目 review/execute）
6. 密码存储：salt + 慢哈希，为什么
7. JWT vs Session：无状态 vs 有状态、**JWT 注销难题**（黑名单 / 短 TTL + refresh token）
8. JWT 三坑：alg=none、弱密钥、无法主动失效
9. 限流防暴力破解（本项目 `/auth/token` 挂**独立** auth 桶，见修订 §5.3）

---

# 五、plan-reviewer 审核修订（2026-08-28）

## 5.1 🔴 阻塞：换非 root MySQL 账号会直接让后端起不来

原方案漏了 Docker MySQL 的一个硬规则：**`MYSQL_USER` / `MYSQL_PASSWORD` 只在数据目录为空的
首次初始化时才创建账号**。模块 7 已真机跑通，`mysql_data` 卷**早就存在且非空** ⇒
改 compose 加 `MYSQL_USER: ecom` 后，卷里根本没有这个账号 →
backend 用 `ecom` 连库 auth 失败 → lifespan 的 `_init_db()` 抛错 → 容器反复重启。

**修订（非删除式迁移，遵守 `no-auto-clean`）**：
```
docker compose exec mysql mysql -uroot -p<pw> -e \
  "CREATE USER IF NOT EXISTS 'ecom'@'%' IDENTIFIED BY '<pw>'; \
   GRANT ALL PRIVILEGES ON ecommerce.* TO 'ecom'@'%'; FLUSH PRIVILEGES;"
```
⚠️ **不采用** `docker compose down -v` 删卷重建——删卷是不可逆操作，
`no-auto-clean.md` 要求用户明确指令，不能拿它当默认路径。

授权范围刻意只给 `ecommerce.*`：该账号**不能建库、不能读 `mysql` 系统库、无 `FILE` 权限**
（`FILE` 权限可读写宿主文件，是提权跳板）。这就是「最小权限」可验证的样子。

## 5.2 🟡 Dockerfile 非 root 用户的位置约束

`USER appuser` 必须放在 `pip install` 和 `COPY src/` **之后**——放前面会因无写权限导致
装依赖 / 拷代码失败。
另：backend 运行时确实无本地写需求（`PYTHONDONTWRITEBYTECODE=1` 禁 `.pyc`、无文件日志、
数据全在 MySQL/Redis），所以降权不会踩到写权限问题。

## 5.3 🟡 JWT 攻击面补漏

审核针对方案提的 6 条，逐条裁决（部分在写 `auth.py` 时已提前处理）：

| 项 | 状态 |
|----|------|
| `exp` 缺失必须拒绝（否则「无 exp = 永久 token」比不校验更糟） | ✅ 已实现：`if not isinstance(exp, int)` 直接拒；测试已覆盖「签名合法但无 exp」 |
| `header["alg"]` 会 KeyError → 500 而非 401 | ✅ 已用 `header.get("alg")`；测试覆盖「header 无 alg」「header 非 dict」 |
| `jti` 只生成不校验 → TTL 内可重放、无法注销 | ⚠️ **确认为已知缺口**，见 §5.4 |
| 「fail-closed」表述不准 | ✅ 采纳，改写见 §5.4 |
| `ADMIN_PASSWORD` 的 demo 默认值风险，原方案只标了 `AUTH_SECRET` | ✅ 采纳，一并标注 |
| `/auth/token` 挂 write 桶 → 暴力破解者顺带打爆退款/审批 | ✅ 采纳：**独立 auth 桶**（5 次/60s），别让登录攻击 DoS 掉业务写接口 |

## 5.4 措辞修正 + 补充「已知缺口」

**「fail-closed」用词不准，改规范**：
代码层确实是 fail-closed（无 `AUTH_SECRET` 就抛错拒绝签发/校验），
但 compose 注入了 demo 默认值，所以**实际部署形态是 fail-open + 刺眼告警**。
诚实说法：*代码 fail-closed，编排为了 demo 可跑注入了默认值，代价是「拿到源码 = 能伪造 token」，
生产必须外部注入真密钥。* 不能笼统自称 fail-closed。

**补两条已知缺口**（原方案只列了 IDOR，不列这两条属于诚实度不一致——
毕竟 §四要点里自己写着「JWT 注销难题」）：

- 🟡 **token 无法主动失效**：签发后 TTL（1h）内一律有效，改密码/踢下线都拉不回来。
  `jti` 字段已埋但没验。生产解法：Redis 黑名单存 `jti`（TTL 对齐 token 剩余寿命），
  或短 TTL access token + refresh token。本项目只做短 TTL 缓解，不做黑名单。
- 🟡 **demo 管理员口令有默认值**：拿到 `docker-compose.yml` 的人即可登录审批并执行退款。
  与 `AUTH_SECRET` 同级风险，生产必须外部注入。

## 5.5 落地前排查

review / execute 加鉴权后，所有现有调用点都会 401。落地前 grep 全仓这两个端点，
确认没有评测脚本 / 文档示例在裸调（`tests/test_refund_state_machine.py` 若直调 `mq` 函数则不受影响）。

