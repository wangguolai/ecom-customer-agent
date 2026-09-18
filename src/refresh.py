# -*- coding: utf-8 -*-
"""统一 refresh 入口：从源数据重建所有「持久化派生」（向量库 + SQLite）

改源数据（products.md / policies.md / seed.py）后跑 `python -m src.refresh`，一次性重建：
- SQLite（seed 表）：backend._init_db() 的 DROP 重建
- 向量库（Qdrant）：index_products.build_knowledge_base()（商品 + 政策两个知识域）

⚠️ 部署约束：先 `python -m src.refresh` 再启动服务进程——单例 BM25 索引在进程启动时
从 Qdrant scroll 全量建，政策必须在启动前已入库，否则 kb_type 过滤会使商品/政策检索整体失效。

内存派生（jieba 词典 / 意图触发词 / 评测白名单）不需要 refresh——运行时从源生成，重启即同步。
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _validate_product_ids():
    """校验商品 ID 一致性（防人工加漏/加重 ID 导致 products 表主键冲突、md↔seed 漂移）。

    - ID 非空 + 集合内唯一（防加漏/加重）
    - md 的 id+title ↔ seed 的 product_id+name 双向一致（防两处漂移）
    """
    from collections import Counter
    from src.domain.products import parse_products
    from src.backend.seed import _SEED_PRODUCTS

    products = parse_products()
    md_ids = [p.id for p in products]
    # 先判缺 ID（空串），再判重复——否则 ≥2 个缺 ID 会被误报成「重复 ID {''}」
    empty = [p.title for p in products if not p.id]
    if empty:
        raise ValueError(f"products.md 有商品缺 ID：{empty}")
    dupes = {pid for pid, n in Counter(md_ids).items() if n > 1}
    if dupes:
        raise ValueError(f"products.md 有重复 ID：{dupes}")

    md = {p.id: p.title for p in products}
    seed = {sp[0]: sp[1] for sp in _SEED_PRODUCTS}
    md_only = set(md) - set(seed)
    seed_only = set(seed) - set(md)
    if md_only or seed_only:
        raise ValueError(f"md ↔ seed 不一致：md 有 seed 无 {md_only}，seed 有 md 无 {seed_only}")
    drift = {pid: (md[pid], seed[pid]) for pid in md if md[pid] != seed[pid]}
    if drift:
        raise ValueError(f"md title ↔ seed name 漂移：{drift}")


def _validate_orders():
    """校验订单数据与商品库一致（演示数据自相矛盾的护栏）。

    三条都是真实缺陷换来的：
      ① **订单商品名必须存在于商品库** —— 否则用户查订单时顺带问「这个还有货吗」会查无此物，
         演示当场暴露数据不一致。此前**没有任何自动化守卫**。
      ② **订单金额必须 ∈ 商品价格集合** —— `tests/judge.py` 把回答里所有 `¥数字` 与
         商品价格白名单精确比对；订单金额若不在集合里，退款流程把金额原样返回、
         agent 正确转述，**反而被判「编造价格」（faithfulness=0）**。
      ③ **订单号必须满足 `20\\d{9}`** —— 否则 `intent_router` 的正则命中不了，
         用户查单会掉进 LLM ReAct（慢且不确定）。
    """
    import re as _re
    from src.backend.seed import _SEED_PRODUCTS, _SEED_PRODUCTS_RAW, _SEED_ORDERS, _QTY_OVERRIDE

    names = {sp[1] for sp in _SEED_PRODUCTS}
    prices = {sp[2] for sp in _SEED_PRODUCTS}
    price_by_name = {sp[1]: sp[2] for sp in _SEED_PRODUCTS}

    # 库存覆盖表的 id 存在性：打错一个字符（P009 → P090）不报错、不加警告，
    # 库存会静默保持 100、缺货档消失——而唯一的活断言在 tests/test_redis_cache.py，
    # 要跑到那一步才发现。这里零成本堵住。
    unknown = set(_QTY_OVERRIDE) - {p[0] for p in _SEED_PRODUCTS_RAW}
    if unknown:
        raise ValueError(f"库存覆盖表引用了不存在的商品 id：{sorted(unknown)}")

    bad_names = [o[3] for o in _SEED_ORDERS if o[3] not in names]
    if bad_names:
        raise ValueError(f"订单引用了商品库里不存在的商品：{bad_names}")

    bad_amounts = []
    for o in _SEED_ORDERS:
        m = _re.fullmatch(r"¥(\d+)", o[4] or "")
        if not m:
            bad_amounts.append((o[0], o[4], "格式不是 ¥整数"))
            continue
        amt = int(m.group(1))
        # 两级校验，缺一不可：
        #   ① **必须等于该订单商品的 price** —— 价格只有 10 个值、每个重复 16+ 次，
        #      只判「∈ 集合」的话，把洁齿骨（¥99）的单写成 ¥189 也能通过；
        #      演示时「查订单 → 问这商品多少钱」两步一对就露馅。
        #   ② 同时 ∈ 价格集合（tests/judge.py 的编造价格白名单就是这个语义，作兜底）。
        if amt != price_by_name.get(o[3]) or amt not in prices:
            bad_amounts.append((o[0], o[4], f"应为 ¥{price_by_name.get(o[3])}"))
    if bad_amounts:
        raise ValueError(f"订单金额与商品价格不符（会被判编造价格）：{bad_amounts}")

    bad_ids = [o[0] for o in _SEED_ORDERS if not _re.fullmatch(r"20\d{9}", o[0])]
    if bad_ids:
        raise ValueError(f"订单号格式非法（intent_router 命中不了）：{bad_ids}")


def refresh():
    """重建所有持久化派生数据"""
    print("📍 [0/3] 校验商品 ID 一致性（md ↔ seed 双向）...")
    _validate_product_ids()
    _validate_orders()
    print("    ID 校验通过。")

    print("📍 [1/3] MySQL — 重建 seed 表（DROP + 灌种子）...")
    from src.backend.main import _init_db
    _init_db()
    print("    MySQL 已重建。")

    print("📍 [2/3] Redis — 清空缓存副本（ecom:* 前缀删）...")
    from src.backend import cache
    cache.flush()
    print("    Redis 缓存已清空。")

    print("📍 [3/3] 向量库 — 重建知识库（商品 + 政策）...")
    from src.index_products import build_knowledge_base
    build_knowledge_base()

    print("✅ refresh 完成：MySQL + Redis + 向量库 已从源数据重建。")


if __name__ == "__main__":
    refresh()
