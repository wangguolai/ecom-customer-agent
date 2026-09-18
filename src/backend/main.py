# -*- coding: utf-8 -*-
"""电商客服后端 —— 订单/物流/库存 真实数据源（FastAPI + MySQL）

替代 tools.py 里的内存 mock。agent 的工具通过 HTTP 查这里，才叫「真实工具」。
启动：uvicorn src.backend.main:app --port 8000
"""

import sys
import os
import re
import time
import json
import asyncio
import threading
from contextlib import asynccontextmanager

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, HTTPException, Depends, Request, Response, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.backend.db import get_conn, close_conn
from src.backend import cache
from src.backend import session_store
from src.backend import fault
from src.backend import mq
from src.backend import ratelimit
from src.backend import auth
from src.backend.seed import _SEED_ORDERS, _SEED_LOGISTICS, _SEED_PRODUCTS
from src.config.settings import MEMORY_MAX_PER_CATEGORY
from src.config.rules import (
    MEMORY_CATEGORIES,
    MEMORY_SENSITIVE_PATTERNS,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_APPROVED,
    REFUND_STATUS_REFUNDED,
    REFUND_STATUS_REJECTED,
)

import pymysql  # 只为捕获唯一的 IntegrityError（画像写入的幂等分支）


def _init_db():
    """建表 + 灌种子数据。全部表 DROP 重建：seed 是唯一源，保证 schema/数据每次启动最新。

    refunds 也 DROP 重建：模块 5 改 UNIQUE(order_id,amount) → UNIQUE(order_id)（状态机幂等，
    一个订单一个工单），schema 变更需重建；demo 历史工单是本地测试产物，无需保留。
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS orders")
        cur.execute("DROP TABLE IF EXISTS logistics")
        cur.execute("DROP TABLE IF EXISTS products")
        cur.execute("DROP TABLE IF EXISTS refunds")
        cur.execute("CREATE TABLE orders (order_id VARCHAR(32) PRIMARY KEY, status VARCHAR(20), created_at VARCHAR(32), product VARCHAR(128), amount VARCHAR(32))")
        cur.execute("CREATE TABLE logistics (order_id VARCHAR(32), time VARCHAR(32), location VARCHAR(64), status VARCHAR(20), KEY idx_order_id (order_id))")
        cur.execute("CREATE TABLE products (product_id VARCHAR(16) PRIMARY KEY, name VARCHAR(128), price DOUBLE, qty INTEGER)")
        # created_at：管理后台的审批页要展示工单时间（审批人要看到「什么时候申请的」才能决策）。
        # 该表本来就走 DROP 重建，加列零迁移成本。
        cur.execute("CREATE TABLE refunds (ticket_id VARCHAR(32) PRIMARY KEY, order_id VARCHAR(32), amount DOUBLE, status VARCHAR(20), created_at VARCHAR(32), UNIQUE KEY uk_order (order_id))")
        cur.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s,%s)", _SEED_ORDERS)
        cur.executemany("INSERT INTO logistics VALUES (%s,%s,%s,%s)", _SEED_LOGISTICS)
        cur.executemany("INSERT INTO products VALUES (%s,%s,%s,%s)", _SEED_PRODUCTS)

        # ⚠️ 用户画像表刻意**不走上面的 DROP 重建**，这是本文件唯一一张 IF NOT EXISTS 的表。
        # 上面四张表由 seed.py 派生，seed 是唯一源 → 每次启动重写是安全的（重建=回到真源）。
        # 而 user_memories 是**运行时累积的用户数据**，不是任何 seed 的派生物：DROP 一次
        # 就把所有用户画像清空了，而且没有任何地方能恢复（真源就是它自己）。
        # schema 变更时请用 ALTER/迁移脚本，不要图省事改成 DROP。
        cur.execute(
            "CREATE TABLE IF NOT EXISTS user_memories ("
            "memory_id VARCHAR(36) PRIMARY KEY,"
            "user_id VARCHAR(64) NOT NULL,"
            "content VARCHAR(512) NOT NULL,"
            "category VARCHAR(32),"
            "confidence DOUBLE DEFAULT 0.5,"
            "raw_snippet VARCHAR(256),"
            "source_turn VARCHAR(64),"
            "status VARCHAR(16) DEFAULT 'active',"
            "created_at VARCHAR(32),"
            "updated_at VARCHAR(32),"
            "KEY idx_user (user_id, status, category)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
    finally:
        close_conn(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 DB。不在 import 时执行——MySQL 连不上不该让 import 崩，移到这里报错更明确。

    _init_db() 会 DROP 重建 seed 表（重写真源），和 refresh.py 同属「重写真源」路径，
    缓存副本必须同步失效（cache.flush()），否则重启后最长 60s 返回旧数据——SSOT 单向链路闭环。
    """
    # 鉴权配置自检（模块 8）：AUTH_SECRET 缺失直接抛错，不让后端带着「无密钥」状态起来。
    # 放在 _init_db 之前——配置错误要在碰数据之前就暴露。
    auth.check_auth_config()
    _init_db()
    cache.flush()
    # Redis 探活（非致命）：没起来就降级查库，但启动日志要能区分「Redis OK / 不可用」方便排障
    print("✅ Redis OK（缓存可用）" if cache.ping() else "⚠️ Redis 不可用，降级查库（缓存旁路）")
    # 启动 MQ 消费者（daemon 线程）：BRPOP 阻塞拉取退款工单消息，异步落库。
    # stop_event 让 shutdown 干净退出（BRPOP 最多 5s 后返回）；daemon=True 保证进程退出不阻塞。
    _consumer_stop = threading.Event()
    _consumer_thread = threading.Thread(target=mq.consume_loop, args=(_consumer_stop,), daemon=True, name="refund-consumer")
    _consumer_thread.start()

    # 模型预热（embedding + rerank）：政策/商品检索要走向量 + 精排，而模型是懒加载单例——
    # 不预热的话 **第一次走检索的请求** 会现装模型（实测 +8s，用户视角就是「点一下卡住了」）。
    # ⚠️ 必须在这个位置同步调用（主线程）：进事件循环后检索走 asyncio.to_thread，
    #    torch 在 worker 线程首次初始化 CUDA 会死锁（不报错、直接挂起）——warmup.py 的文档写了这个坑。
    # 容错（非致命）：后端容器只装 requirements-backend.txt（无 torch），agent 跑宿主机时后端用不到模型，
    # 所以预热失败只告警、不阻断启动。
    try:
        from src.infra.warmup import warmup_models
        warmup_models()
        print("✅ 模型预热完成（embedding + rerank 已就绪）")
    except Exception as e:  # noqa: BLE001 —— 预热失败绝不能挡住服务启动，任何异常都降级为告警
        print(f"⚠️ 模型预热跳过（{type(e).__name__}: {e}）——不走向量检索的部署可忽略")

    yield
    _consumer_stop.set()


app = FastAPI(title="电商客服后端", lifespan=lifespan)


# 商品图静态服务（图片本体在 data/product_images，前端通过 /product_images/{file} 加载）
app.mount("/product_images", StaticFiles(directory=os.path.join(_project_root, "data", "product_images")), name="product_images")


# 前端对话页面
# 两代并存：优先 React 构建产物（frontend/dist，npm run build 生成），未构建时回落到原版单文件页
# （web/index.html）。回落分支保证「clone 下来没装 node 也能跑」——前端构建不是启动后端的前置条件。
_FRONTEND_DIST = os.path.join(_project_root, "frontend", "dist")
_FRONTEND_ASSETS = os.path.join(_FRONTEND_DIST, "assets")
_LEGACY_INDEX = os.path.join(_project_root, "web", "index.html")
if os.path.isdir(_FRONTEND_ASSETS):
    # vite 会把 js/css 打进 dist/assets/ 并带内容哈希，挂成静态目录
    app.mount("/assets", StaticFiles(directory=_FRONTEND_ASSETS), name="assets")


@app.get("/", include_in_schema=False)
def index():
    dist_index = os.path.join(_FRONTEND_DIST, "index.html")
    if os.path.exists(dist_index):
        return FileResponse(dist_index)
    return FileResponse(_LEGACY_INDEX)


# ── 限流依赖（模块 5）──
# 读接口挂 read 桶（松），写接口挂 write 桶（严：退款/审批/执行资金敏感）。
# Depends 依赖函数：超限 raise 429。client 固定 "demo"（单客户端），生产 per-IP + 全局双层。
#
# 阈值配置外部化（对齐模块 7「配置外部化」规范）：默认值 = 原硬编码值，行为零变化。
# 压测要临时放宽阈值时改环境变量即可，不用改代码。
def _int_env(name: str, default: int) -> int:
    """读整型环境变量，非法值回落默认（配置错不该让后端起不来）"""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _limit_read():
    if not ratelimit.rate_limit("read", "demo", window=_int_env("RL_READ_WINDOW", 10), max_req=_int_env("RL_READ_MAX", 100)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


def _limit_write():
    if not ratelimit.rate_limit("write", "demo", window=_int_env("RL_WRITE_WINDOW", 10), max_req=_int_env("RL_WRITE_MAX", 10)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


def _limit_auth():
    """登录接口独立限流桶（模块 8）。

    为什么不复用 write 桶：登录是暴力破解的靶子，攻击者狂刷密码会顺带把 write 桶打满，
    退款/审批这些正常业务写操作跟着被 429 —— 攻击一个接口，瘫痪一片。
    独立桶让爆破的代价只落在它自己身上（故障隔离，和熔断按下游分粒度同一个道理）。
    阈值也更严：5 次/60s。
    """
    if not ratelimit.rate_limit("auth", "demo", window=_int_env("RL_AUTH_WINDOW", 60), max_req=_int_env("RL_AUTH_MAX", 5)):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后重试")


def _limit_memory():
    """画像接口独立限流桶。

    为什么不复用 write 桶：画像每轮对话至少写一次，与 /refund、/review、/execute 共用
    write 桶（10 次/10s）会让**正常聊天把用户的退款请求打成 429**。
    同 _limit_auth 的故障隔离道理——攻击/打满一个接口，不该瘫痪一片。
    阈值放宽到 50 次/10s：画像读写是高频轻量操作，不该被当成写资金那样严管。
    """
    if not ratelimit.rate_limit("memory", "demo", window=_int_env("RL_MEMORY_WINDOW", 10), max_req=_int_env("RL_MEMORY_MAX", 50)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


def _limit_admin():
    """后台接口独立限流桶（10s/100）。

    为什么不复用 read 桶（100/10s）：后台列表会 JOIN + 分页，与全站读接口争抢配额；
    且后台是**人在点**不是机器刷，独立桶便于单独观察。
    同 _limit_auth / _limit_memory 的故障隔离道理。

    ⚠️ 定义必须在这里（所有路由定义之前）：`Depends(_limit_admin)` 是**默认参数值**，
    在函数**定义时**求值——定义在后会直接 NameError（`_clean_field` 那类在函数体内调用的
    才不受顺序影响）。
    """
    if not ratelimit.rate_limit("admin", "demo", window=_int_env("RL_ADMIN_WINDOW", 10), max_req=_int_env("RL_ADMIN_MAX", 100)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")


@app.get("/orders/{order_id}")
def get_order(order_id: str, _: None = Depends(_limit_read)):
    # 参数校验（穿透三层第一层）：非法格式直接 400，不查库不写空标记——
    # 否则任意不存在订单号都会写 30s 空标记，被 LLM 幻觉/攻击者刷爆 Redis。
    # 上界 32 与 orders.logistics 的 order_id VARCHAR(32) 对齐：超长纯数字在库中必然不存在，
    # 校验住它才真正堵住「刷空标记」口子。
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")
    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    # 注入的是 tools.py 已有的降级路径（超时→友好提示、脏数据→数据异常），不定义新降级行为。
    mode = fault.apply_fault("orders")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")  # 200 但 JSON 解析失败 → bad_response
    if mode == "empty":
        return {}  # 合法 JSON 缺字段，触发工具层「数据异常」降级
    cache_key = f"ecom:order:{order_id}"

    def rebuild():
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT status, created_at, product, amount FROM orders WHERE order_id=%s", (order_id,))
            row = cur.fetchone()
        finally:
            close_conn(conn)
        if row is None:
            return False, None
        return True, {"order_id": order_id, "status": row[0], "created_at": row[1], "product": row[2], "amount": row[3]}

    _, data = cache.get_or_rebuild(cache_key, 60, rebuild)
    if data is None:  # 命中空标记（不存在）
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id}")
    return data


@app.get("/logistics/{order_id}")
def get_logistics(order_id: str, _: None = Depends(_limit_read)):
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")
    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    mode = fault.apply_fault("logistics")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")  # 200 但 JSON 解析失败 → bad_response
    if mode == "empty":
        return {}  # 合法 JSON 缺字段，触发工具层「数据异常」降级
    cache_key = f"ecom:logistics:{order_id}"

    def rebuild():
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT time, location, status FROM logistics WHERE order_id=%s ORDER BY time", (order_id,))
            rows = cur.fetchall()
        finally:
            close_conn(conn)
        if not rows:
            return False, None
        return True, {"order_id": order_id, "traces": [{"time": r[0], "location": r[1], "status": r[2]} for r in rows]}

    _, data = cache.get_or_rebuild(cache_key, 60, rebuild)
    if data is None:
        raise HTTPException(status_code=404, detail=f"未查到订单号 {order_id} 的物流信息")
    return data


@app.get("/products/{product_id}")
def get_product(product_id: str, _: None = Depends(_limit_read)):
    """商品实时价格 + 库存（动态数据走工具实时查，数据分治：价格/库存不进向量库）"""
    # 格式校验（对齐 orders 的防御水平）：任意短字符串都会 404 写空标记，被 LLM 幻觉/攻击者刷爆 Redis。
    # 用格式 P\d{1,15} 拦掉，和 seed 的 P001 形态对齐。
    if not re.fullmatch(r"P\d{1,15}", product_id):
        raise HTTPException(status_code=400, detail=f"商品 ID 非法：{product_id}")
    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    mode = fault.apply_fault("products")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")  # 200 但 JSON 解析失败 → bad_response
    if mode == "empty":
        return {}  # 合法 JSON 缺字段，触发工具层「数据异常」降级
    cache_key = f"ecom:product:{product_id}"

    def rebuild():
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name, price, qty FROM products WHERE product_id=%s", (product_id,))
            row = cur.fetchone()
        finally:
            close_conn(conn)
        if row is None:
            return False, None
        return True, {"product_id": product_id, "name": row[0], "price": row[1], "qty": row[2]}  # qty=0 是真实数据（缺货），正常缓存

    _, data = cache.get_or_rebuild(cache_key, 30, rebuild)
    if data is None:
        raise HTTPException(status_code=404, detail=f"未查到商品 {product_id}")
    return data


# 客服在线状态的运行时覆盖（评测用）：None = 未覆盖，回落读 CS_ONLINE 环境变量。
# 为什么需要：CS_ONLINE 是启动时读的，容器里改要重启，而重启会 DROP 重建全部表。
# 转人工三分支（在线 / 不在线 / 查询失败）评测必须能在一次运行内切换状态。
_online_override = None


@app.get("/online")
def check_online(_: None = Depends(_limit_read)):
    """客服在线状态（动态数据走接口查，数据分治）。demo 用环境变量 CS_ONLINE mock（可测试切换），
    生产来自坐席系统（IM/客服工作台）实时状态。

    CS_ONLINE 布尔解析：字符串 "false" 是 truthy，必须 strip().lower() == "true" 判断。
    """
    # 故障注入（评测用）：让 transfer_to_human 走「在线状态查不到」的降级分支
    mode = fault.apply_fault("online")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")
    if mode == "empty":
        return {}  # 缺 online 字段 → tools 侧 .get("online") 为 None → 按不在线保守处理
    if _online_override is not None:
        return {"online": _online_override}
    # 默认 false（诚实优先）：本地 demo 没有真人坐席，默认答「不在线/已记录工单」是**真实**的；
    # 反过来默认 true 会让 agent 承诺「已为您转接人工客服」——本地根本没人接，是假承诺。
    # 要演示「已转接」场景就显式设 CS_ONLINE=true（评测走 /debug/online 覆盖，不受此处默认值影响）。
    online = os.environ.get("CS_ONLINE", "false").strip().lower() == "true"
    return {"online": online}


@app.post("/refund")
def refund_order(payload: dict, _: None = Depends(_limit_write)):
    """退款（写操作）—— 金额下沉 + MQ 异步化。

    金额下沉：LLM 只传 order_id，退款金额=订单金额（后端查，全额退款），
    LLM 无权指定金额（防被诱导填 0/负数/超额，数据分治：能结构化查到的参数不让 LLM 填）。
    代码级防御不变：只生成「待人工审批」工单，不直接退款。
    同步只做「校验 + 查订单金额」（即时反馈），「落库 + 通知人工」走 MQ 异步。
    降级：MQ（Redis）不可用 → 回退同步落库，退款不因 MQ 挂而失败。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="缺少 order_id")
    # 边界清洗：孤立代理等非法码点在「错误回显」时会再炸一次 UnicodeEncodeError（fuzz 实测）。
    # 外部输入进系统第一件事就是把非法码点降级成 ?，之后校验/回显/落库全程安全。
    if isinstance(order_id, str):
        order_id = order_id.encode("utf-8", "replace").decode("utf-8")

    # 故障注入（评测用）：命中直接 return，不写缓存（避免脏响应污染缓存）。
    # 注入的是 tools.py 已有的降级路径（超时→友好提示、脏数据→数据异常），不定义新降级行为。
    mode = fault.apply_fault("refund")
    if mode == "timeout":
        time.sleep(3)  # 超过 tools.py 的 2s 客户端超时，触发 timeout 降级路径
        return {}
    if mode == "dirty":
        return Response(content="这不是合法JSON", media_type="application/json")
    if mode == "empty":
        return {}

    # 校验 + 查订单金额（金额下沉：退款金额=订单金额，后端权威值）
    ok, err_msg, status_code, refund_amount = mq._validate_refund(order_id)
    if not ok:
        raise HTTPException(status_code=status_code, detail=err_msg)

    # 发消息到 MQ（消息只带 order_id，不带 amount，防篡改）
    pub = mq.publish_refund(order_id)
    if pub["ok"]:
        return {"status": "已受理", "message_id": pub["message_id"], "ticket_id": None, "refund_amount": refund_amount}

    # 降级回退：MQ 不可用 → 同步落库（refund_amount 是订单金额）
    ticket = mq._create_refund_ticket(order_id, refund_amount)
    return {"status": "已受理", "message_id": None, "ticket_id": ticket["ticket_id"], "refund_amount": refund_amount}


@app.post("/chat/stream")
async def chat_stream(request: Request, payload: dict, _: None = Depends(_limit_read)):
    """SSE 流式输出最终答案（text/event-stream，逐 token）。

    客户端断开检测：由 Starlette 1.6 的 StreamingResponse 内置兜住，代码不重复造轮子。

    踩坑复盘（2026-09-02 误判 → 2026-09-03 纠正）：
      - 早先以为「客户端断开后服务端继续把答案生成完」要自己写检测，加了
        request.is_disconnected()。但它不可靠：Starlette 1.6 用 anyio CancelScope(cs.cancel())
        做「非阻塞 receive」，cancel 立即触发，await _receive() 还没等到 uvicorn 的
        message_event 就被打断，永远拿不到 http.disconnect——这是死代码。
      - 实际 Starlette 1.6 的 StreamingResponse.__call__ 已按 ASGI spec_version 分支处理断开：
        spec < 2.4 用内置 listen_for_disconnect（start_soon 跑流式 + await listen_for_disconnect，
        断开 cancel 整个 task_group）；spec >= 2.4 靠 ASGI 2.4 规范「send 到断开连接抛 OSError」
        捕获。当前 uvicorn 0.52 发 spec_version 2.3，走 listen_for_disconnect 可靠分支
        （FAKE_STREAM 假流式 + 原始 socket 断开实测：只发 3 段就停止）。
      - 所以断开时 Starlette 主动 cancel 本生成器，这里只保留 CancelledError 兜底：接住让
        请求体面结束（LLM 流已随 cancel 级联关闭，停止烧 token）。
    """
    # 惰性 import：避免后端启动即加载 agent 的重依赖（只在 /chat/stream 请求时才加载）
    from src.agent import AgentSession
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    user_msg = payload.get("message")
    if not isinstance(user_msg, str) or not user_msg.strip():
        raise HTTPException(status_code=400, detail="缺少 message")
    # 边界清洗：孤立代理等非法码点在流式链路（LLM/SSE）里一样会炸，进系统第一件事先降级
    user_msg = user_msg.encode("utf-8", "replace").decode("utf-8")

    # 会话层：带 session_id 则恢复跨轮上下文（前端生成、localStorage 持久）。
    # 缺 id / 非字符串 → 退回无状态单轮（兼容旧前端与直接 curl 调用，不报错）——
    # 会话是增强不是依赖，拿不到就当新会话，不能让请求失败。
    raw_sid = payload.get("session_id")
    session_id = raw_sid.strip() if isinstance(raw_sid, str) and raw_sid.strip() else None
    history = session_store.load(session_id)

    # 记忆作用域（user_id）：**从认证上下文推导，不从请求体取**。
    #   有 Bearer token 且验签通过 → 用 claim 的 sub。这是业界做法（mem0：作用域必须服务端
    #   推导，漏传直接报错），也是本文件唯一「服务端说了算」的作用域来源。
    #   无 token → 回落 session_id（兼容现有前端链路；代价是画像寿命 = 会话寿命）。
    #   两者都没有 → None，记忆功能整体不启用（硬防呆在 src/memory.py，见其模块注释）：
    #   绝不能让 user_id 为空还去抽取/检索——既会写出无主脏数据，也会在测试里真实外呼 LLM。
    user_id = None
    raw_auth = request.headers.get("authorization") or ""
    if raw_auth.startswith("Bearer "):
        try:
            claims = auth.decode_token(raw_auth[7:].strip())
            sub = claims.get("sub")
            if isinstance(sub, str) and sub.strip():
                user_id = _clean_field(sub, 64)
        except Exception:
            # token 非法/过期一律当「未登录」：/chat/stream 本身不要求登录，
            # 带一个坏 token 不该让聊天失败（鉴权是增强不是依赖）。
            user_id = None
    if not user_id:
        user_id = session_id

    session = AgentSession(history, user_id=user_id)

    async def event_gen():
        answer = ""
        try:
            # 假流式（debug，零付费）：FAKE_STREAM=1 时用假内容 + sleep 模拟慢速流式，
            # 专门验证「断开检测」机制，不真调 LLM。和 ENABLE_DEBUG_FAULT 同性质的调试能力。
            if os.environ.get("FAKE_STREAM", "").strip().lower() in ("1", "true", "yes"):
                for i in range(500):
                    yield f"data: {json.dumps({'delta': f'假内容第{i}段'}, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(0.02)
                print("假流式跑完 500 段（客户端未断开）")
                return
            async for delta in session.stream_chat(user_msg):
                answer += delta  # 累积完整答案供会话层落历史（SSE 单向流，生成器外面拿不到最终文本）
                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            # 流式结束：把本轮检索命中的商品图作为单独事件发给前端渲染（图不进 LLM 文本）
            from src.tools import pop_collected_images
            images = pop_collected_images()
            if images:
                yield f"data: {json.dumps({'images': images}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # Starlette 1.6 内置 listen_for_disconnect 检测到断开会 cancel 本生成器，
            # 接住让请求体面结束；LLM 流已随 cancel 级联关闭，停止烧 token。
            print("客户端已断开，停止流式输出")
        finally:
            # 落历史：answer 为空（断开/异常）时 save 内部直接返回，不把半截回复写进上下文
            session_store.save(session_id, history, user_msg, answer)
            # 画像抽取（后台任务，不阻塞返回）。
            # **为什么放 finally 而不是流式循环末尾**：
            #   ① answer 的聚合发生在本函数（循环里 answer += delta），stream_chat 内部拿不到完整文本；
            #   ② 客户端断开时 Starlette 会 cancel 本生成器，写在循环末尾的钩子根本不会执行，
            #      而 finally 一定会执行——断开时 answer 是半截的，但 user_msg 是完整的，
            #      抽取主要依据用户说了什么，半截回复影响有限。
            # spawn_extract 内部对 user_id/answer 为空已做防呆，且异常不外抛。
            from src.memory import spawn_extract
            spawn_extract(user_id, user_msg, answer)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════
# 用户画像（长期记忆）—— 真源在 MySQL，Qdrant 是派生层
# ═══════════════════════════════════════════════════════════════

def _clean_field(value, limit: int) -> str:
    """边界清洗 + 截断（外部输入进系统第一件事）。"""
    if not isinstance(value, str):
        return ""
    cleaned = value.encode("utf-8", "replace").decode("utf-8").strip()
    return cleaned[:limit]


_SENSITIVE_RES = [re.compile(p) for p in MEMORY_SENSITIVE_PATTERNS]


def _is_sensitive_memory(text: str) -> bool:
    """服务端敏感信息复校。

    客户端（agent 侧 `memory._parse_extract_output`）已经过滤过一道，这里**再校一遍**——
    不是冗余：客户端校验可被绕过（直连接口调用、旧版本 agent、被篡改的客户端），
    服务端才是权威边界。项目一贯口径：「外部输入都不可信，校验要分层」
    （同 `_http_request` 对工具返回复校 status/qty/price 的做法）。
    """
    return any(r.search(text or "") for r in _SENSITIVE_RES)


@app.post("/memory")
def create_memory(payload: dict, _: None = Depends(_limit_memory)):
    """写入画像（**真源**）。由 agent 抽取后调用。

    幂等：memory_id 由 agent 侧按 (user_id, 归一化 content) 确定性生成，重复抽取撞主键
    即视为「已存在」，静默跳过——这是「同一事实重复抽取不产生重复记录」的落地。

    最小缓冲：同 (user_id, category) 的 active 超过 MEMORY_MAX_PER_CATEGORY 条时，把最旧的
    若干条置 superseded 并**回传 id**，让 agent 侧删掉对应的 Qdrant 点（真源驱动派生）。
    为什么需要：一期不做 UPDATE/DELETE 裁决，用户改口（「我现在不养金毛了」）时旧条否则会
    永久驻留并被每轮注入。缓冲让旧条被自然挤出——注意是置 superseded 不是删除，
    保留可追溯性。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    user_id = _clean_field(payload.get("user_id"), 64)
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少 user_id")

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items 必须是非空数组")
    if len(items) > 20:
        raise HTTPException(status_code=400, detail="单次写入上限 20 条")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    created, superseded, touched = [], [], set()

    conn = get_conn()
    try:
        cur = conn.cursor()
        for it in items:
            if not isinstance(it, dict):
                raise HTTPException(status_code=400, detail="items 元素必须是对象")
            memory_id = _clean_field(it.get("memory_id"), 36)
            content = _clean_field(it.get("content"), 512)
            category = _clean_field(it.get("category"), 32)
            snippet = _clean_field(it.get("raw_snippet"), 256)
            try:
                confidence = float(it.get("confidence", 0.5))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="confidence 必须是数值")

            if not memory_id or not content:
                raise HTTPException(status_code=400, detail="memory_id / content 不能为空")
            if category not in MEMORY_CATEGORIES:
                raise HTTPException(status_code=400, detail=f"category 不在白名单：{category}")
            if not (0.0 <= confidence <= 1.0):
                raise HTTPException(status_code=400, detail="confidence 必须在 [0,1]")
            if _is_sensitive_memory(content) or _is_sensitive_memory(snippet):
                # 服务端复校：客户端能过滤不算数（见 _is_sensitive_memory 的说明）
                raise HTTPException(status_code=400, detail="content 含敏感信息，拒绝入库")

            try:
                cur.execute(
                    "INSERT INTO user_memories "
                    "(memory_id, user_id, content, category, confidence, raw_snippet, "
                    " status, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'active',%s,%s)",
                    (memory_id, user_id, content, category, confidence, snippet, now, now),
                )
                created.append(memory_id)
                touched.add(category)
            except pymysql.err.IntegrityError:
                continue  # 撞主键 = 该事实已存在（幂等），预期分支，不是错误

        # 最小缓冲：每个受影响 category 只留最新 N 条 active
        for cat in touched:
            cur.execute(
                "SELECT memory_id FROM user_memories "
                "WHERE user_id=%s AND category=%s AND status='active' "
                "ORDER BY created_at DESC, memory_id DESC",
                (user_id, cat),
            )
            rows = [r[0] for r in cur.fetchall()]
            for old_id in rows[MEMORY_MAX_PER_CATEGORY:]:
                cur.execute(
                    "UPDATE user_memories SET status='superseded', updated_at=%s "
                    "WHERE memory_id=%s",
                    (now, old_id),
                )
                superseded.append(old_id)
    finally:
        close_conn(conn)

    return {"created": created, "superseded": superseded}


@app.get("/memory")
def list_memory(
    user_id: str,
    _auth: None = Depends(auth.require_role("admin")),
    _rl: None = Depends(_limit_admin),
):
    """列出某用户的 active 画像（管理后台的画像详情页用）。

    已挂 admin 鉴权（2026-09-18 随管理后台一起收紧）。
    **为什么能安全收紧**：全仓 grep 确认**没有任何调用方**——agent 的画像检索走
    本地 Qdrant（`memory.retrieve` → `get_qdrant_store().search_memory`），不经 HTTP；
    写入走 `POST /memory`（那个保持无鉴权，因为 agent 进程没有 token）。

    限流桶从 `_limit_memory` 换成 `_limit_admin`：否则与聊天链路的 `POST /memory`
    共用同一个桶（key 里的 client 是硬编码 "demo"），后台翻几页就能把聊天写画像打成 429。

    也是二期「用户查看自己的画像」（PIPL 第 45 条查阅权）的雏形。
    """
    user_id = _clean_field(user_id, 64)
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少 user_id")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT memory_id, content, category, confidence, raw_snippet, created_at "
            "FROM user_memories WHERE user_id=%s AND status='active' "
            "ORDER BY category, created_at DESC",
            (user_id,),
        )
        return {
            "user_id": user_id,
            "items": [
                {"memory_id": r[0], "content": r[1], "category": r[2],
                 "confidence": r[3], "raw_snippet": r[4], "created_at": r[5]}
                for r in cur.fetchall()
            ],
        }
    finally:
        close_conn(conn)


@app.delete("/memory/{memory_id}")
def delete_memory(memory_id: str, _: None = Depends(auth.require_role("admin"))):
    """删除单条画像（软删真源 + 同步删派生）。

    **必须挂 admin 鉴权**：无鉴权的删除接口 = 任意人可删任意人的画像，比只读越权更重
    （「可查可删」是 PIPL 第 47 条给**用户本人**的权利，不是给任意访客的）。

    真源用软删（`status='deleted'`）、派生用硬删（Qdrant 删点）——这个不对称是刻意的：
      · 真源保留痕迹，可追溯「谁在什么时候删了什么」
      · 派生是检索索引，必须**立即物理移除**，否则被删的画像仍会被 retrieve 命中并注入对话
        （`memory.retrieve` 只查 Qdrant 不查 MySQL，只软删真源 = 删除不生效）
    删点只需 memory_id（Qdrant 的 point id 就是它），**不需要 embedding**。

    分层说明：后端在此触碰 Qdrant 是有代价的——容器模式下 backend 不带 qdrant-client。
    故惰性 import + 失败降级：删点失败只记日志，真源已删是对的，派生由
    `python -m src.refresh_memory` 对账重建兜底（派生可重建是这套架构的前提）。
    """
    memory_id = _clean_field(memory_id, 36)
    if not memory_id:
        raise HTTPException(status_code=400, detail="memory_id 非法")

    conn = get_conn()
    try:
        cur = conn.cursor()
        affected = cur.execute(
            "UPDATE user_memories SET status='deleted', updated_at=%s "
            "WHERE memory_id=%s AND status<>'deleted'",
            (time.strftime("%Y-%m-%d %H:%M:%S"), memory_id),
        )
        if not affected:
            raise HTTPException(status_code=404, detail="画像不存在或已删除")
    finally:
        close_conn(conn)

    # 同步派生层：不做的话，删除只改了真源状态，被删画像仍会被检索注入（功能静默失效）
    derived_synced = True
    derived_error = None
    try:
        from src.infra.vector_store import get_qdrant_store
        get_qdrant_store().delete_by_memory_ids([memory_id])
    except Exception as e:
        # 失败不抛（真源已删是对的，派生可由 refresh_memory 对账重建），
        # 但**必须让调用方知道**——静默降级正是这个 bug 最初没被发现的原因。
        derived_synced = False
        derived_error = f"{type(e).__name__}: {e}"
        print(f"⚠️ 派生层删点失败（真源已删，可跑 refresh_memory 对账修复）：{derived_error}",
              file=sys.stderr)

    return {
        "memory_id": memory_id,
        "status": "deleted",
        # False 表示「真源已删但索引未同步」——此时该画像**仍可能被检索命中**，
        # 调用方（管理后台）应据此提示用户跑 `python -m src.refresh_memory`
        "derived_synced": derived_synced,
        "derived_error": derived_error,
    }


# ═══════════════════════════════════════════════════════════════
# 管理后台接口（/admin/*）
# ═══════════════════════════════════════════════════════════════
#
# 为什么全部挂 admin 鉴权 —— 注意**不要**用这个理由：
#   ❌「单条查询要猜 ID、列表能 dump 全量」—— 订单号是 20 开头的 11 位半序列号
#      （seed.py 里就是顺序排的），枚举成本极低；且 /orders/{id} 本来就公开 →
#      数据本来就能全量获得。用这个理由，面试一追问就崩。
# ✅ 真实理由：
#   ① 工程示范——后台接口按「后台会话鉴权」的标准做法实现，体现的是权限设计意识；
#   ② 单条接口的 IDOR 是**已知缺口**（architecture.md §7 #1），后台不在这之上叠新接口。
#
# 依赖顺序刻意是「鉴权在前、限流在后」（与 /review 现有写法相反）：
# 未认证流量不该消耗 admin 桶——否则匿名洪泛就能把后台打成 429。

def _page_bounds(page: int, size: int) -> tuple[int, int]:
    """分页参数换算成 (limit, offset)。

    边界由 FastAPI 的 Query(ge/le) 保证（非数字 → 422，越界 → 422），
    所以这里不需要再校验负数——`LIMIT -1` 那种 500 在入口就被挡住了。
    """
    return size, (page - 1) * size


def _like_escape(q: str) -> str:
    """转义 LIKE 通配符，防 `q=%` 拉全量。

    用 `!` 作转义符而不是默认的反斜杠：反斜杠在 Python 字符串 + SQL 字符串 + MySQL
    转义规则之间要过三层，且受 `NO_BACKSLASH_ESCAPES` sql_mode 影响；`!` 无歧义。
    ⚠️ 必须先转义 `!` 自身，否则 `q=!%` 会绕过。
    """
    return q.replace("!", "!!").replace("%", "!%").replace("_", "!_")


@app.get("/api/admin/orders")
def admin_list_orders(
    q: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _auth: None = Depends(auth.require_role("admin")),
    _rl: None = Depends(_limit_admin),
):
    """订单列表（分页 + 搜索）。

    `q` 的两种语义：纯数字 → 按订单号精确匹配（用户最常贴的就是订单号）；
    否则 → 按商品名模糊匹配。**精确优先**避免「贴订单号却走 LIKE 全表扫」。
    `size` 上限 100：不限的话 `?size=999999` 一次 dump 全表，是列表接口最典型的失误。
    """
    limit, offset = _page_bounds(page, size)
    q = (q or "").strip()[:64]

    where, params = "", []
    if q:
        if q.isdigit():
            where = " WHERE order_id = %s"
            params = [q]
        else:
            # ORDER BY 必须显式给：不排序时 MySQL 不保证两次查询的顺序一致，
            # 翻页会重复/漏行（同一个「最典型失误」的另一半）
            where = " WHERE product LIKE %s ESCAPE '!'"
            params = [f"%{_like_escape(q)}%"]

    conn = get_conn()
    try:
        cur = conn.cursor()
        # 列表与总数**共用同一个 where/params 构造**——分开写会漂移（过滤条件改了只改一处）
        cur.execute(f"SELECT COUNT(*) FROM orders{where}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT order_id, status, created_at, product, amount FROM orders{where} "
            f"ORDER BY order_id DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        items = [
            {"order_id": r[0], "status": r[1], "created_at": r[2], "product": r[3], "amount": r[4]}
            for r in cur.fetchall()
        ]
    finally:
        close_conn(conn)
    return {"total": total, "page": page, "size": size, "items": items}


@app.get("/api/admin/orders/{order_id}")
def admin_order_detail(
    order_id: str,
    _auth: None = Depends(auth.require_role("admin")),
    _rl: None = Depends(_limit_admin),
):
    """订单 + 物流聚合详情。

    为什么聚合：前端要展示「订单 + 轨迹」两块，分成两个接口调用要处理两次失败、
    两次鉴权、两次限流计数。聚合成一个，前端一次拿到。

    ⚠️ `traces` 为空返 `[]` 而非 404：seed 里只有一单有物流数据，
    「订单存在但暂无物流」是**正常态**不是错误。返 404 会让前端把常态渲染成报错。
    """
    order_id = _clean_field(order_id, 32)
    # 与其他订单接口同口径校验（/orders/{id}、/logistics/{id} 用 \d{8,32}）：
    # 截断本身不会造成「查到错误订单」（列是 VARCHAR(32) 且用 = 精确匹配），
    # 但防御口径该统一——非法格式在入口就拦掉，不必进库空跑一次。
    if not re.fullmatch(r"\d{8,32}", order_id):
        raise HTTPException(status_code=400, detail=f"订单号格式非法：{order_id}")

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT order_id, status, created_at, product, amount FROM orders WHERE order_id=%s",
            (order_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"订单不存在：{order_id}")
        order = {"order_id": row[0], "status": row[1], "created_at": row[2],
                 "product": row[3], "amount": row[4]}
        cur.execute(
            "SELECT time, location, status FROM logistics WHERE order_id=%s ORDER BY time",
            (order_id,),
        )
        traces = [{"time": r[0], "location": r[1], "status": r[2]} for r in cur.fetchall()]
    finally:
        close_conn(conn)
    return {"order": order, "traces": traces}


@app.get("/api/admin/refunds")
def admin_list_refunds(
    status: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _auth: None = Depends(auth.require_role("admin")),
    _rl: None = Depends(_limit_admin),
):
    """退款工单列表（按状态筛选）。

    返回字段刻意带上**订单商品名**：审批人要看到「退的是哪笔订单的什么商品 + 多少钱 +
    什么时候申请的」才能决策，只有 ticket_id/amount 是没法审批的。

    `status` 是**中文**枚举（rules.py：待人工审批/已批准/已退款/已拒绝），
    非枚举值直接 400——不静默忽略，否则前端筛错了会看到「全部」还以为筛对了。
    """
    limit, offset = _page_bounds(page, size)

    where, params = "", []
    if status:
        if status not in (REFUND_STATUS_PENDING, REFUND_STATUS_APPROVED,
                          REFUND_STATUS_REFUNDED, REFUND_STATUS_REJECTED):
            raise HTTPException(status_code=400, detail=f"status 非法：{status}")
        where = " WHERE r.status = %s"
        params = [status]

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM refunds r{where}", params)
        total = cur.fetchone()[0]
        cur.execute(
            # LEFT JOIN：工单对应的订单被删时仍要列出工单（不能因为订单没了就让工单消失）
            f"SELECT r.ticket_id, r.order_id, r.amount, r.status, r.created_at, o.product "
            f"FROM refunds r LEFT JOIN orders o ON o.order_id = r.order_id{where} "
            f"ORDER BY r.created_at DESC, r.ticket_id DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        items = [
            {"ticket_id": r[0], "order_id": r[1], "amount": r[2], "status": r[3],
             "created_at": r[4], "product": r[5]}
            for r in cur.fetchall()
        ]
    finally:
        close_conn(conn)
    return {"total": total, "page": page, "size": size, "items": items}


@app.get("/api/admin/users")
def admin_list_users(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _auth: None = Depends(auth.require_role("admin")),
    _rl: None = Depends(_limit_admin),
):
    """有画像的用户列表。

    ⚠️ 刻意**同时返回 active 数和总数**，而不是只返回 active：
    只统计 active 的话，「记忆被删光的用户」整行会消失——反而无法验证「删除生效了」。
    总数 > active 数恰恰是「这个用户有被删/被挤掉的记忆」的证据。

    ⚠️ user_id 大多是 UUID 或 session_id（聊天页无登录时回落 session_id），
    前端必须标注这是**会话标识**而非真实账号，否则看起来像 bug。
    """
    limit, offset = _page_bounds(page, size)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM user_memories")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT user_id, "
            "       SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_count, "
            "       COUNT(*) AS total_count, "
            "       MAX(updated_at) AS last_update "
            "FROM user_memories GROUP BY user_id "
            # ⚠️ 必须带二级排序键：last_update 是秒级精度，同秒写入的多个用户顺序不确定，
            # 翻页边界落在 tie 组内会重复/漏行。orders 用 order_id、refunds 用 ticket_id
            # 都补了 tiebreaker，这里用 user_id（GROUP BY 后唯一）。
            "ORDER BY last_update DESC, user_id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        items = [
            {"user_id": r[0], "active_count": int(r[1]), "total_count": int(r[2]),
             "last_update": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        close_conn(conn)
    return {"total": total, "page": page, "size": size, "items": items}


@app.get("/api/admin/metrics")
def admin_metrics(
    _auth: None = Depends(auth.require_role("admin")),
    _rl: None = Depends(_limit_admin),
):
    """运行指标快照。

    返回体自带口径标注（`scope` / `sample_source` / `sample_since`）——
    不标注的话，看到「样本数 3」的人会以为这是全量统计，
    实际上是「本进程 + 只有 Web 链路 + 重启清零」。
    """
    # 惰性 import：src.agent 会拉起 qdrant/torch 等重依赖，不该在后端启动时就加载。
    # ⚠️ 隐藏依赖：backend-only 容器（只装 requirements-backend.txt，无 torch）
    # 调这个接口会 500。本机跑没问题（agent 与 backend 同进程跑）；容器部署时要
    # 么补依赖，要么把指标改成独立模块。面试被问「你的指标怎么采集」要能说清这条。
    from src.agent import METRICS
    return METRICS.snapshot()


# ═══════════════════════════════════════════════════════════════
# 后台 SPA fallback —— ⚠️ 必须注册在**所有 /admin API 之后**
# ═══════════════════════════════════════════════════════════════
#
# 为什么不能挂在 `/` 路由上：`/` 在 main.py 前面、位于所有 API 之前，
# FastAPI 按注册顺序匹配 → 在 `/` 那里加 catch-all 会让 /orders、/logistics、
# /products、/online、/refund、/chat/stream **全部返回 index.html**（整条 API 被吞）。
# 这里用 `/admin` + `/admin/{path:path}` 两条，只吃 /admin 前缀，碰不到其他路由。
#
# ⚠️ dist 缺失时**不能回落 web/index.html**：legacy 单文件页里没有后台，
#    回落过去会白屏（比 404 更难排查）。这里明确 404 并提示先构建。

@app.get("/admin", include_in_schema=False)
@app.get("/admin/{path:path}", include_in_schema=False)
def admin_spa(path: str = ""):
    dist_index = os.path.join(_FRONTEND_DIST, "index.html")
    if not os.path.exists(dist_index):
        raise HTTPException(
            status_code=404,
            detail="后台前端未构建，请先执行：cd frontend && npm run build",
        )
    return FileResponse(dist_index)


@app.post("/auth/token")
def issue_token(payload: dict, _: None = Depends(_limit_auth)):
    """签发 admin token（模块 8）。demo 账号来自环境变量，密码只存 pbkdf2 慢哈希，不存明文。

    失败一律返回同一个 401「用户名或密码错误」——不区分「用户不存在」和「密码错」，防用户枚举
    （能枚举出有效用户名，爆破范围就从「用户名×密码」缩小成「密码」，成本差几个数量级）。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    # 边界清洗：username/password 里的孤立代理会打崩 auth 的编码（fuzz 实测 500）。
    # 这里先把非法码点降级，auth.authenticate 内部也有 errors="replace" 双保险。
    username = payload.get("username")
    password = payload.get("password")
    username = username.encode("utf-8", "replace").decode("utf-8") if isinstance(username, str) else ""
    password = password.encode("utf-8", "replace").decode("utf-8") if isinstance(password, str) else ""
    role = auth.authenticate(username, password)
    if not role:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"access_token": auth.create_token(payload.get("username"), role), "token_type": "Bearer", "expires_in": auth.TOKEN_TTL}


@app.post("/refund/{ticket_id}/review")
def review_refund(ticket_id: str, payload: dict, _: None = Depends(_limit_write),
                  __: dict = Depends(auth.require_role("admin"))):
    """人工审批（代码层驱动状态机流转，不由 LLM 驱动）。action=approve/reject。

    并发安全：mq._apply_transition 用条件 UPDATE（WHERE status=当前状态）乐观锁，
    两个并发审批只有一个成功，另一个 409。
    鉴权（模块 8）：这是资金敏感操作，挂 require_role("admin")（垂直越权防线），
    和 execute 一致。IP 白名单是生产进一步的纵深，demo 未做（知道规范即可）。
    """
    # 路径参数格式校验（对齐 orders 的 \d{8,32} / products 的 P\d{1,15} 防御水平）：
    # 工单号由 mq._create_refund_ticket 生成，格式 RF + 8 位大写 hex。非法值拦在进库之前。
    if not re.fullmatch(r"RF[0-9A-F]{8}", ticket_id):
        raise HTTPException(status_code=400, detail=f"工单号格式非法：{ticket_id}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    action = payload.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail=f"非法 action：{action}，只支持 approve/reject")
    result = mq._apply_transition(ticket_id, action)
    if not result["ok"]:
        raise HTTPException(status_code=result["status_code"], detail=result["err"])
    return {"ticket_id": ticket_id, "status": result["status"]}


@app.post("/refund/{ticket_id}/execute")
def execute_refund(ticket_id: str, _: None = Depends(_limit_write),
                   __: dict = Depends(auth.require_role("admin"))):
    """退款执行（approved → refunded，mock）。真实场景接支付/财务，这里只改状态。

    execute 后 orders.status 不联动（demo mock），生产退款到账要联动订单状态。

    鉴权（模块 8）：这里和 review 是真正的「钱闸」，必须 admin。
    注意 /refund（agent 代客申请）刻意不加鉴权——它只生成待审工单、没有资金流，属低权操作。
    按「资金影响」分级，而不是「凡写接口一律鉴权」：后者会白白打断 agent 链路且没有安全收益。
    """
    if not re.fullmatch(r"RF[0-9A-F]{8}", ticket_id):
        raise HTTPException(status_code=400, detail=f"工单号格式非法：{ticket_id}")
    result = mq._apply_transition(ticket_id, "execute")
    if not result["ok"]:
        raise HTTPException(status_code=result["status_code"], detail=result["err"])
    return {"ticket_id": ticket_id, "status": result["status"]}


# ── 故障注入调试端点（模块：评测用，生产默认关闭）──
# ENABLE_DEBUG_FAULT 环境变量为真才挂路由——「能让系统出故障」的无鉴权端点不能裸奔
# （和 prompt injection 的写权限开关同类：调试后门代码级默认关闭）。
if os.environ.get("ENABLE_DEBUG_FAULT", "").strip().lower() in ("1", "true", "yes"):
    @app.post("/debug/fault")
    def debug_set_fault(payload: dict, _: None = Depends(_limit_write)):
        target = payload.get("target")
        mode = payload.get("mode")
        count = payload.get("count", 1)
        if target not in ("orders", "logistics", "products", "refund", "online"):
            raise HTTPException(status_code=400, detail=f"非法 target：{target}")
        if mode not in ("timeout", "dirty", "empty"):
            raise HTTPException(status_code=400, detail=f"非法 mode：{mode}")
        fault.set_fault(target, mode, count)
        return {"status": "已注入", "target": target, "mode": mode, "count": count}

    @app.post("/debug/fault/clear")
    def debug_clear_fault(payload: dict = None, _: None = Depends(_limit_write)):
        target = (payload or {}).get("target")
        fault.clear_fault(target)
        return {"status": "已清除"}

    @app.post("/debug/online")
    def debug_set_online(payload: dict, _: None = Depends(_limit_write)):
        """运行时切换客服在线状态（评测用）。online=null 清除覆盖、回落 CS_ONLINE 环境变量。

        和 /debug/fault 同属「评测用调试设施」，共用 ENABLE_DEBUG_FAULT 门控——
        能改业务状态的无鉴权端点，生产必须默认关闭。
        """
        global _online_override
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        value = payload.get("online")
        if value is not None and not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"online 必须是布尔或 null：{value!r}")
        _online_override = value
        return {"status": "已设置", "online_override": _online_override}
