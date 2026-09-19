# -*- coding: utf-8 -*-
"""trace 落盘 —— 让「一次对话到底发生了什么」在请求结束后仍可回溯

**为什么必须落盘**：Trace（`src/infra/observability.py`）是**纯内存对象，请求结束即丢**。
没有落盘，用户评分就只是一个孤立数字 —— 不知道当时的 query、检索命中了哪些 chunk、
调了哪些工具、prompt 是什么版本。调研原话：「**这正是飞轮死掉的地方**」。

**表结构沿用 `user_memories` 的规矩**：`CREATE TABLE IF NOT EXISTS`，
**不参与 `_init_db` 的 DROP 重建**（trace 是运行时累积数据，不是 seed 派生）。

⚠️ **隐私口径与 `user_memories` 刻意不同，是有意的不是疏漏**：
  · `user_memories` 用 `MEMORY_SENSITIVE_PATTERNS` **拒绝**存订单号/手机号/地址——
    因为那是**面向用户展示的画像**，存了就有泄露面；
  · 本表**无条件**存下 query/answer 的完整内容——因为它是**内部排障数据**，
    脱敏了就复现不了问题（比如「用户贴了订单号、agent 答错了」，脱敏后根本看不出错在哪）。
  口径不同是因为**用途不同**。生产环境仍需评估保留期与访问控制（PIPL 视角下这是个人信息）。

落盘失败**不阻塞**（可观测是增强不是依赖，同 `session_store` 哲学），只打日志。
"""

import sys
import os
import json
import time
import hashlib

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.config.settings import TRACE_QUERY_MAX, TRACE_JSON_MAX

CREATE_SQL = (
    "CREATE TABLE IF NOT EXISTS traces ("
    "trace_id VARCHAR(36) PRIMARY KEY,"
    "session_id VARCHAR(64),"
    "user_id VARCHAR(64),"
    "query VARCHAR(512),"
    "answer TEXT,"
    "route_source VARCHAR(16),"
    "end_reason VARCHAR(24),"
    "total_sec DOUBLE,"
    "total_tokens INT,"
    "cache_hit INT,"
    "cache_miss INT,"
    "reasoning_tokens INT,"
    "llm_steps INT,"
    "tool_calls VARCHAR(512),"
    "retrieved_ids VARCHAR(512),"
    "prompt_version VARCHAR(16),"
    "kb_version VARCHAR(16),"
    "created_at VARCHAR(32),"
    "KEY idx_created (created_at),"
    "KEY idx_session (session_id)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


# 增量列（只增不改；历史行留 NULL）。加列时必须同时改 `CREATE_SQL` 和 `save()` 的列清单，
# 三者是同一份 schema 的三个副本，漏一个就会「新库能跑、老库炸」。
_ENSURE_COLUMNS = (
    ("reasoning_tokens", "INT"),
)


def ensure_columns(conn) -> None:
    """幂等迁移：给**已存在**的 traces 表补新列。

    **为什么必需**：`CREATE TABLE IF NOT EXISTS` 对已存在的表**什么都不做**——DDL 里加了列，
    老库不会自己长出来。而 `save()` 的 INSERT 会因 `Unknown column` 整条失败，且 `save()`
    吞掉所有异常只打一行 stderr → **此后每条 trace 静默丢失**，而后台照常显示旧数据，
    看板看不出任何异常。这正是「静默降级比崩溃更难查」的形态。

    ⚠️ 用 `SHOW COLUMNS ... LIKE`（作用域是连接的默认库，天然不跨库误命中），
      不用 `information_schema` + `TABLE_SCHEMA=DATABASE()`——多一层拼接就多一个写错的机会。
    ⚠️ 迁移后**必须回读校验**：把校验失败留给 `save()` 去吞 = 白做。
    ⚠️ 调用点必须在 `_init_db` 里 `CREATE TABLE IF NOT EXISTS` **之后**（表不存在时
      SHOW COLUMNS 查不到、ALTER 会失败）。
    """
    cur = conn.cursor()
    for col, col_ddl in _ENSURE_COLUMNS:
        cur.execute(f"SHOW COLUMNS FROM traces LIKE '{col}'")
        if cur.fetchone() is None:
            cur.execute(f"ALTER TABLE traces ADD COLUMN {col} {col_ddl}")
    # 回读校验：迁移没生效就让调用方（启动流程）炸掉，别等到落盘时静默丢数据
    cols = ", ".join(c for c, _ in _ENSURE_COLUMNS)
    cur.execute(f"SELECT {cols} FROM traces LIMIT 0")


def _kb_version() -> str:
    """知识库源数据版本（products.md + policies.md 的内容 hash）。

    为什么光有 `PROMPT_VERSION` 不够：**同样决定答案的还有知识库本身**。
    prompt 没变而商品数据换了，答案照样会变——只记 prompt 版本，复现时会对不上。
    模块 import 时算一次（源文件只在 refresh 时变，不需要每次请求重算）。
    """
    h = hashlib.sha256()
    for name in ("products.md", "policies.md"):
        try:
            with open(os.path.join(_project_root, "data", name), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:8]


KB_VERSION = _kb_version()


def _truncate_json(value) -> str:
    """序列化并截断（超长会在 VARCHAR(512) 上 Data too long → 整条 trace 丢）。

    ⚠️ 截断**必须留痕**：死循环样本（多轮多工具）最容易超长，而它正是最该看的一类。
    静默截断会让人以为「当时只调了这些工具」。调用方据返回值判断是否被截。
    """
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= TRACE_JSON_MAX:
        return text, False
    return text[:TRACE_JSON_MAX], True


def _clean(text, limit: int) -> str:
    """边界清洗 + 截断（外部输入进系统第一件事，同 main.py 的 _clean_field）"""
    if not isinstance(text, str):
        return ""
    return text.encode("utf-8", "replace").decode("utf-8").strip()[:limit]


def save(trace, session_id: str, user_id: str, query: str, answer: str) -> bool:
    """落盘一次对话。失败返回 False（不抛）。

    ⚠️ **调用方必须把它单独包一层 try/except**：本函数内部已吞异常，但如果调用点
    （如 import 失败）在 finally 里抛出，会**吞掉同一个 finally 中排在它后面的语句**
    ——而 `session_store.save` / `spawn_extract` 正排在后面。

    `trace` 为 None 时直接返回（`stream_chat` 的 trace 在首次 `__anext__` 才创建，
    `FAKE_STREAM=1` 的早退分支压根不调它，此时 `get_last_trace()` 返回 None）。
    """
    if trace is None:
        return False
    try:
        from src.backend.db import get_conn, close_conn
        from src.config.prompts import PROMPT_VERSION

        s = trace.summary()
        tool_calls_json, tc_truncated = _truncate_json(
            # category = 该次检索锁的类别（含「类别下沉」注入的）。None=没锁。
            # 排障要点：跨轮混类时，必须能看到当时**锁了什么**，而不只是「检索错了」。
            [{"name": tc[0], "elapsed": round(tc[1], 3), "empty": bool(tc[3]),
              **(({"category": tc[4]}) if len(tc) > 4 and tc[4] else {})}
             for tc in trace.tool_calls]
        )
        retrieved_json, rid_truncated = _truncate_json(trace.retrieved_ids)
        # 截断留痕：读到 end_reason 带这个后缀就知道 tool_calls/retrieved_ids 不是全量
        end_reason = s["结束原因"] + ("+截断" if (tc_truncated or rid_truncated) else "")

        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO traces (trace_id, session_id, user_id, query, answer, "
                "route_source, end_reason, total_sec, total_tokens, cache_hit, cache_miss, "
                "reasoning_tokens, llm_steps, tool_calls, retrieved_ids, prompt_version, "
                "kb_version, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    _clean(trace.trace_id, 36),
                    _clean(session_id, 64),
                    _clean(user_id, 64),
                    _clean(query, TRACE_QUERY_MAX),
                    _clean(answer, 20000),          # answer 有 MAX_OUTPUT_TOKENS 封顶，不会更大
                    _clean(s["路由来源"], 16),
                    _clean(end_reason, 24),
                    float(s["总耗时(秒)"]),
                    int(s["总 token 消耗"]),
                    int(s["缓存命中 token"]),
                    int(s["缓存未命中 token"]),
                    int(s["推理 token"]),           # 思考模式思维链消耗（主循环 + 摘要）
                    int(s["LLM 调用次数"]),
                    tool_calls_json,
                    retrieved_json,
                    PROMPT_VERSION,
                    KB_VERSION,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        finally:
            close_conn(conn)
        return True
    except Exception as e:  # noqa: BLE001 —— 落盘失败绝不能影响对话主链路
        print(f"⚠️ trace 落盘失败（不影响对话）：{type(e).__name__}: {e}", file=sys.stderr)
        return False
