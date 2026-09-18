# -*- coding: utf-8 -*-
"""画像派生层重建 + 真源↔派生对账

    MySQL  user_memories          ← 真源（唯一真相）
        ↓ materialize
    Qdrant user_memory collection ← 派生（语义检索索引）

为什么需要这个入口：方案里说「派生可重建」，但**一个函数不是一条链路**——
没有调用入口、没有触发时机、没有一致性检测，就是空头承诺。这里补齐三样。

**与 src/refresh.py 的区别**（别混）：
    refresh.py        走「重写真源」路径——seed.py 是唯一源，DROP 重建所有业务表是安全的
    refresh_memory.py 走「真源 → 派生」路径——画像是运行时累积的用户数据，**真源就是它自己**，
                      任何情况下都不能重建（重建 = 永久丢失所有画像）

什么时候该跑：
    - Qdrant 索引损坏 / 迁移到新机器
    - 换了 embedding 模型（向量维度或语义空间变了，旧向量失效）
    - 怀疑真源与派生不一致（对账报差异）

⚠️ **前置条件：先停掉持有 Qdrant 文件锁的进程**（backend / agent CLI）。
   Qdrant 本地文件模式用 portalocker 加锁，两个进程同时开同一个路径会抛 AlreadyLocked。

用法：
    python -m src.refresh_memory            # 对账，不一致则重建
    python -m src.refresh_memory --check    # 只对账不改（CI/巡检用）
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _load_active_from_source(user_id: str = None) -> list[dict]:
    """从真源（MySQL）读 active 画像。user_id 为空则读全部。"""
    from src.backend.db import get_conn, close_conn

    conn = get_conn()
    try:
        cur = conn.cursor()
        if user_id:
            cur.execute(
                "SELECT memory_id, user_id, content, category, confidence "
                "FROM user_memories WHERE status='active' AND user_id=%s",
                (user_id,),
            )
        else:
            cur.execute(
                "SELECT memory_id, user_id, content, category, confidence "
                "FROM user_memories WHERE status='active'"
            )
        return [
            {"memory_id": r[0], "user_id": r[1], "content": r[2],
             "category": r[3], "confidence": r[4]}
            for r in cur.fetchall()
        ]
    finally:
        close_conn(conn)


def reconcile(check_only: bool = False, user_id: str = None) -> dict:
    """对账真源与派生，不一致则重建。返回统计字典。

    对账口径：活跃行数（MySQL）vs 派生的有效点数（Qdrant）。
    刻意**只比总数**而不逐条比 id —— 逐条比对要全量 scroll 出 Qdrant 的 payload，
    而重建的成本只是「重算一遍 embedding」，对画像这个量级（每人几十条）远低于
    全量比对的复杂度。不一致就整体重建，简单且不会漏。
    """
    from src.infra.vector_store import get_qdrant_store

    rows = _load_active_from_source(user_id)
    store = get_qdrant_store()
    points = store.count_memory_points(user_id)

    stats = {"source": len(rows), "derived": points, "rebuilt": 0, "consistent": len(rows) == points}
    print(f"  📊 真源(MySQL)={len(rows)} 条，派生(Qdrant)={points} 点")
    if stats["consistent"]:
        print("  ✅ 一致，无需重建")
        return stats
    if check_only:
        print("  ⚠️ 不一致（--check 只报告不修改）")
        return stats

    print("  🔧 不一致，按真源重建派生层...")
    # 重建 = 清空派生 + 从真源重灌。刻意用 drop + recreate 而不是「逐条 diff 增删」：
    # 派生层本就是可丢弃的副本，diff 的复杂度换不来任何收益（画像量级小，重灌很快）；
    # 而且 drop 能顺带清掉**幽灵点**（真源已删、派生还留着的点）——这正是漂移最典型的形态。
    store.drop_collection("user_memory")
    store.ensure_collections(vector_size=512)

    if not rows:
        print("  ✅ 真源为空，派生层已清空")
        stats["consistent"] = True   # 重建后已一致（否则 main() 会把「已修复」当失败返回 1）
        return stats

    from src.infra.embedding import embed_texts

    payloads = [
        {"memory_id": r["memory_id"], "user_id": r["user_id"], "content": r["content"],
         "category": r["category"], "confidence": r["confidence"]}
        for r in rows
    ]
    vectors = embed_texts([r["content"] for r in rows])
    store.upsert_memory(payloads, vectors)
    stats["rebuilt"] = len(payloads)
    # 重建完成 = 已修复。consistent 必须跟着更新：它原本只在开头算过一次（重建前的状态），
    # 不更新的话 main() 会按旧值返回 exit 1，CI/巡检把「刚修好」误判成「仍不一致」。
    stats["consistent"] = True
    print(f"  ✅ 已重建 {stats['rebuilt']} 条（差异已修复）")
    return stats


def main():
    check_only = "--check" in sys.argv
    user_id = None
    for i, a in enumerate(sys.argv):
        if a == "--user" and i + 1 < len(sys.argv):
            user_id = sys.argv[i + 1]

    print("=" * 60)
    print("画像派生层对账" + ("（只检查）" if check_only else ""))
    print("=" * 60)
    print("⚠️ 前置条件：持有 Qdrant 文件锁的服务进程（backend / agent）已停止\n")

    stats = reconcile(check_only=check_only, user_id=user_id)
    print("=" * 60)
    return 0 if stats["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
