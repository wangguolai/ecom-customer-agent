# -*- coding: utf-8 -*-
"""反馈评分落盘 —— 用户对某轮回答的 👍/👎 及其原因

**为什么独立成文件，不塞进 `trace_store.py`**：两者的**失败姿态正好相反**——
  · trace 落盘是**后台可观测**：失败只打日志，绝不能影响对话主链路（那是 trace_store 的契约）；
  · 评分是**用户在等结果的请求**：失败必须如实报错。否则用户以为评上了、数据却没进库
    ——差评静默丢失比报错更糟：飞轮少一个样本，而且没有任何人知道少了。
放同一个文件，很容易让「吞异常」的写法从 trace 路径蔓延到评分路径。

⚠️ **本模块（及其调用方 `main.py` 的评分路由）不得 import `tests.regression`**：
评分**不进坏 case 池**。池的毕业判据是客观的 `top1 ∈ expected`，而用户评分是**主观**的、
没有 expected——直接进池会在一次回归跑之后被**假阳性毕业**（判定「已修复」，其实压根没修）。
低分进的是「待复核队列」，通道**必须经过人工**：
低分 trace → 人读样本 → 聚类失败模式 → 手写 case 进 `tests/cases.py`
（expected 用**修正后的答案**，不是被嫌弃的原答案）。
`tests/test_feedback.py` 有一条静态守卫钉死这条约束（源码文本断言，不靠自觉）。

表结构沿用 `user_memories` / `traces` 的规矩：`CREATE TABLE IF NOT EXISTS`，
**不参与 `_init_db` 的 DROP 重建**——用户评分是运行时累积数据，真源就是它自己，DROP 一次就没了。

⚠️ **隐私**：`comment` 是用户自由输入，可能含订单号/联系方式。
它与 `traces.query/answer` 同属「内部排障数据」口径（见 trace_store 的模块注释），
生产环境同样需要保留期与访问控制。本表额外多一层：**只挂 admin 读**（写是匿名）。
"""

import sys
import os
import time

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pymysql

# ⚠️ `feedback_id` 直接就是 `trace_id`，**不加 `:user_rating` 这类后缀**。
#    后缀是「为将来接第二种评分预留」的写法，但本项目没有第二种评分来源，
#    而代价是每个读到它的人都要先想「后缀是什么意思」——**用不到的预留就是负债**。
#    真接 judge 评分时再改成 `{trace_id}:{source}`，那时 ALTER 主键 + 迁移一次即可。
CREATE_SQL = (
    "CREATE TABLE IF NOT EXISTS feedback ("
    "feedback_id VARCHAR(64) PRIMARY KEY,"
    "trace_id VARCHAR(36) NOT NULL,"
    "session_id VARCHAR(64),"
    "rating TINYINT NOT NULL,"
    "reason VARCHAR(32),"
    "comment VARCHAR(512),"
    "created_at VARCHAR(32),"
    "updated_at VARCHAR(32),"
    "KEY idx_trace (trace_id),"
    "KEY idx_rating (rating, created_at),"
    "KEY idx_created (created_at)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

# 时间格式与 traces / user_memories 一致（VARCHAR(32) 存字符串，排序按字典序=时间序）
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def upsert(trace_id: str, session_id: str, rating: int, reason: str, comment: str) -> str:
    """写入/更新一条评分。返回 `"created"` 或 `"updated"`。

    **幂等语义 = 用户改主意是「更新」不是「新增」**（调研的标准做法）：
    同一条回答先点了👎后改成👍，库里应当只有一行、created_at 保留首次时间、updated_at 记录改动。

    **为什么用 `except IntegrityError` 而不是 `INSERT ... ON DUPLICATE KEY UPDATE`**：
    全仓的幂等写法统一是前者（`main.py` 的画像写入、`mq.py` 的退款工单），
    `ON DUPLICATE` / `REPLACE INTO` 零使用。多引入一种写法，读代码的人就要多判一次
    「这里为什么不一样」——**一致性本身就是可读性**，而这两种写法在这里没有性能差异。

    ⚠️ 与 `trace_store.save` 相反，本函数**不吞异常**：调用方（路由）要据它返回 5xx，
    让用户知道没评上。吞掉的话就是「点了没反应，刷新后也没有」——最难排查的一类现象。
    """
    from src.backend.db import get_conn, close_conn

    now = time.strftime(_TIME_FMT)
    conn = get_conn()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO feedback (feedback_id, trace_id, session_id, rating, reason, "
                "comment, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (trace_id, trace_id, session_id, rating, reason, comment, now, now),
            )
            return "created"
        except pymysql.err.IntegrityError:
            # 主键冲突 = 同一条回答被再次评分。**改主意是更新**：
            # created_at 不动（「第一次什么时候评的」是时间趋势的基准），只刷新 updated_at。
            # session_id 也一起刷：会话标识可能变（用户换了会话再改评分），
            # 留旧的会让后台显示「这条评分属于另一个会话」。
            cur.execute(
                "UPDATE feedback SET rating=%s, reason=%s, comment=%s, session_id=%s, updated_at=%s "
                "WHERE feedback_id=%s",
                (rating, reason, comment, session_id, now, trace_id),
            )
            # ⚠️ **这里刻意不检查 `cur.rowcount`** —— 踩过坑（2026-09-18）：
            #   MySQL 的 affected_rows 返回的是「**实际改变了值的行数**」，不是「匹配到的行数」。
            #   用户在同一秒内重复提交**完全相同**的评分（rating/reason/comment/session_id 都没变，
            #   updated_at 秒级精度也一样），MySQL 判定「没有列被改变」→ 返回 **0**。
            #   我起初按「0 = 行不存在」写了 raise，结果**正常重复提交全部 500**，
            #   是 tests/test_feedback.py 的限流用例（连发 20 次相同评分）把它抓出来的。
            #   想拿「匹配行数」得开 `CLIENT.FOUND_ROWS`，或额外 SELECT 一次——两者都不值得：
            #   INSERT 刚因**主键冲突**失败，这本身就证明那行存在（不存在就没有冲突可言）。
            #   （MySQL 原生 `mysql` 客户端默认开 FOUND_ROWS，所以口头验证时容易被误导。）
            return "updated"
    finally:
        close_conn(conn)
