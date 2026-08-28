# Docker 部署安装与验证教程（模块 7 部署运维）

> 用途：本机装 Docker，跑通 `docker compose up` 真机验证后端容器化。
> 交付物已写好：`Dockerfile` + `docker-compose.yml` + `requirements-backend.txt` + `.dockerignore`（项目根目录）。

## ✅ 验证已完成（2026-08-28）

三容器全部跑通，接口实测通过：

```
backend-1   Up                        0.0.0.0:8000->8000/tcp
mysql-1     Up (healthy)              0.0.0.0:3307->3306/tcp
redis-1     Up (healthy)              0.0.0.0:6379->6379/tcp

/orders/20240818001    → {"status":"已发货","product":"幼犬成长粮（皇家牌）1.5kg",...}
/logistics/20240818001 → 3 条轨迹（杭州分拨 → 杭州转运 → 上海转运）
/products/P001         → {"price":89.0,"qty":120}
/products/P001（二次）  → 3.16ms，Redis 缓存命中
```

启动日志顺序 `mysql Started → Waiting → Healthy → backend Starting`，证明 `condition: service_healthy` 生效。
本机另有 mysqld 占着宿主机 3306，backend 全程未连到它 —— 证明容器间走的是内部网络（服务名 `mysql:3306`）。

**设计要点已归档到 `references/_BACKEND.md` 模块 7**，本文只留操作记录和环境踩坑。

## 模块 8 安全加固后的机器状态（2026-08-28）

在模块 7 基础上又加了一层安全加固（见 `docs/security-plan.md`），涉及 compose 变化，这里同步记录：

| 项 | 变化 |
|----|------|
| backend 容器用户 | root → `appuser`（非 root）+ `cap_drop: ALL` + `no-new-privileges` |
| MySQL 连接账号 | root → `ecom`（只授 `ecommerce.*`，手工 `CREATE USER` 建，**不删卷**） |
| 鉴权 | `/refund/{id}/review`、`/refund/{id}/execute` 挂 JWT（`src/backend/auth.py`）；新增 `POST /auth/token` 签发 |
| 调试端点 | `/debug/fault`、`/debug/online` 由 `ENABLE_DEBUG_FAULT` 门控（默认关） |
| 环境变量 | 新增 `AUTH_SECRET` / `ADMIN_USER` / `ADMIN_PASSWORD`（demo 默认值，生产必须外部注入） |

**重建 backend 的命令**（改了 Dockerfile/compose 后）：

```bash
ENABLE_DEBUG_FAULT=1 docker compose up -d --build backend   # 跑评测时带调试端点
docker compose up -d --build backend                        # 正常不带
```

**首次切换 ecom 账号**（已有 volume 时 `MYSQL_USER` 不自动建账号，必须手工建）：

```bash
docker compose exec -T mysql mysql -uroot -p"<密码>" -e \
  "CREATE USER IF NOT EXISTS 'ecom'@'%' IDENTIFIED BY '<密码>'; \
   GRANT ALL PRIVILEGES ON ecommerce.* TO 'ecom'@'%'; FLUSH PRIVILEGES;"
```

⚠️ **宿主机脚本连库要显式打 3307**：本机 `.env` 的 `MYSQL_PORT=3306` 指向本机 mysqld，
是另一个同名 `ecommerce` 库。宿主机脚本查容器 MySQL 的数据要连 `127.0.0.1:3307`，否则「查得通、有数据、断言还全绿」，是最危险的一类静默错误。

**评测脚本里登录**：`AUTH_SECRET` / `ADMIN_USER` / `ADMIN_PASSWORD` 与 compose 注入值一致
（demo 默认 `dev-only-secret-change-me` / `admin` / `admin123`），生产换真值后评测脚本要同步改。

## 最终机器状态

| 项 | 状态 |
|----|------|
| BIOS 虚拟化（VT-x） | ✅ 已开 |
| Hypervisor | ✅ 已启用（重启 WSL 安装后生效） |
| WSL 本体 | ✅ 2.7.12.0，内核 6.18.33.2-2 |
| Linux 发行版 | ❌ 无（不影响 Docker，见坑 1） |
| Docker | ✅ 29.7.2 / Compose v5.4.0（用户级安装） |
| 本机 MySQL | ⚠️ 仍占宿主机 3306（compose 已改用 3307，不冲突） |

## 环境踩坑记录（操作层，非技术要点）

> 这几条是「Windows 上装 Docker」的环境问题，实际工作不涉及，但重装/换机器会再遇到。

### 坑 1 · `wsl --install` 重启后没弹 Ubuntu 设置终端

- **现象**：管理员 PowerShell 跑 `wsl --install`，重启电脑后没有「自动继续」。
- **定位**：`wsl --version` 有输出（本体 + 内核都在）、`wsl --status` 显示默认版本 2，但 `wsl -l -v` 报「没有已安装的分发版」→ 核心组件装了，发行版那步没执行。
- **根因**：Windows 11 build 26200 起 WSL 改为 Microsoft Store 独立分发，`wsl --install` 检测到组件已在启用流程中时只装核心 + 内核，跳过发行版。
- **结论**：**不用管**。Docker Desktop 自带 `docker-desktop` WSL 发行版当运行时，不依赖 Ubuntu。真要 Ubuntu 单独跑 `wsl --install -d Ubuntu`。

### 坑 2 · Docker 装在用户级目录，常规路径找不到

- **现象**：`C:\Program Files\Docker` 不存在，全盘搜 `docker.exe` 也搜不到。
- **根因**：装的是**用户级**（注册表在 `HKCU` 不是 `HKLM`），实际路径
  `C:\Users\<用户>\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`（层级很深，`-Depth 6` 的递归搜索够不到）。
- **定位姿势**：别全盘搜文件，查 `winget list --id Docker.DockerDesktop` 或读注册表 `HKCU:\...\Uninstall\*` 的 `InstallLocation`，一步到位。

### 坑 3 · 装完 Docker，已开着的终端里 `docker: command not found`

- **根因**：进程的 PATH 是**启动时从父进程继承的快照**。安装程序改的是注册表里的 PATH 并广播 `WM_SETTINGCHANGE`，但只有 Explorer 这类 GUI 会响应刷新，**已经在跑的控制台进程收不到**。
- **解**：① 重开终端（最简单）；② 当前 shell 里临时补：
  ```bash
  export PATH="$PATH:/c/Users/<用户>/AppData/Local/Programs/DockerDesktop/resources/bin"
  ```
  ③ PowerShell 里从注册表重读：
  ```powershell
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
  ```

### 坑 4 · 拉镜像超时（国内网络）

- **现象**：`docker run hello-world` 报 `dial tcp 104.244.43.231:443: connectex: A connection attempt failed...`
  （注意这个 IP 是 Twitter 的段 —— DNS 污染的典型表现）
- **解**：Settings → Docker Engine 加 `registry-mirrors`，Apply & restart。**本机实测通过的三个源**（2026-08-28）：
  ```json
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
  ```
  按实测延迟排序（0.07s / 0.27s / 0.93s）。验证是否在线：`curl -o /dev/null -w "%{http_code}" https://<源>/v2/`，**返回 401 就是通的**（registry v2 未认证的正常响应）。
- **⚠️ 别抄老教程**：中科大 `docker.mirrors.ustc.edu.cn`、网易 `hub-mirror.c.163.com` 这批 2026 年已全部失效（USTC 连 DNS 都解析不出来）。镜像源列表必须现查现验。
- 下载速度参考：本机实测 70~210 KB/s 波动，`mysql:8.0` 那个大层下了约 20 分钟。慢但不是故障，看数字是否在推进即可判断。

## 完整步骤

### 第一步：装 WSL2（需重启电脑）

**管理员身份**打开 PowerShell，执行：

```powershell
wsl --install
```

- 理论上这一条装齐「WSL 功能 + Ubuntu 发行版 + WSL2 内核」
- 装完**重启电脑**（整个 Windows 重启，不是重启终端——WSL2 要加载内核驱动）
- 重启后**可能弹**终端让你设 Ubuntu 用户名 + 密码

> ⚠️ **实测（2026-08-28，Win11 build 26200）：没有弹，发行版根本没装上**，只装了 WSL 核心 + 内核。
> 详见上方「坑 1」。**结论是不用管** —— Docker Desktop 自带运行时，不依赖 Ubuntu，可直接进第二步。
> 验证本步是否够用：`wsl --version` 有版本号输出即可，不必纠结 `wsl -l -v` 是空的。

> 若 `wsl --install` 报错「找不到功能」，手动开两个开关（重启后生效）：
> ```powershell
> dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
> dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
> ```

### 第二步：装 Docker Desktop

1. 官网下载：https://www.docker.com/products/docker-desktop/ （Windows 版）
2. 双击安装，勾选 "Use WSL 2 instead of Hyper-V"（默认已勾）
3. 装完重启

> 收费：个人 / 小企业（<250 员工或 <$1000 万美元年收入）免费，个人学习不用付费，跳过订阅页。

### 第三步：验证

```bash
docker --version
docker compose version
docker run --rm hello-world   # 打印 "Hello from Docker!" 即通
```

### 第四步：跑本项目

```bash
docker compose up --build -d     # 首次要构建 + 拉镜像，慢；之后 up -d 秒级
docker compose ps                # 看状态，mysql/redis 应为 (healthy)
docker compose logs backend      # 看后端启动日志
docker compose down              # 停止（加 -v 连 volume 一起删，数据会没）
```

**端口冲突坑（已踩，已修）**：本机 MySQL 占宿主机 3306，原来的 `"3306:3306"` 启动直接失败：

```
Error response from daemon: ports are not available: exposing port TCP 0.0.0.0:3306
-> 127.0.0.1:0: listen tcp 0.0.0.0:3306: bind: Only one usage of each socket address
(protocol/network address/port) is normally permitted.
```

两个解法，**本项目选了后者**（不影响本机开发环境）：
- 停本机 MySQL：`net stop mysql`（干净，但本机开发受影响）
- 改端口映射：`"3306:3306"` → `"3307:3306"`（已改，见 `docker-compose.yml` 注释）

> ⚠️ 改的时候**只改宿主机侧**。backend 的 `MYSQL_PORT` 必须保持 `3306` —— 它走 compose 内部网络连 `mysql:3306`，不经过宿主机端口映射。跟着改成 3307 反而连不上。

### 常用运维命令

```bash
docker compose ps -a                     # 含已退出的容器
docker compose logs -f backend           # 跟踪日志
docker compose port mysql 3306           # 查某容器端口映射到宿主机哪个端口
docker compose exec mysql mysql -uroot -proot ecommerce   # 进容器连库
docker compose up -d --force-recreate    # 强制重建容器（改了 env 后）
docker system df                         # 看镜像/容器/卷占了多少磁盘
```

## 部署要点

已归档到 **`references/_BACKEND.md` → 模块 7 · 部署运维**（8 条，含 3 条 🔴 ）。

本文只留操作步骤和环境踩坑，要点不在这里重复维护，避免两处漂移。
