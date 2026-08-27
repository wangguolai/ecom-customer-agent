# -*- coding: utf-8 -*-
"""坏 case 回归池（数据飞轮：失败 case 自动回流 + 复测毕业）

机制（数据飞轮的最小闭环）：
  1. 回流：评测跑完，失败 case（top1 错 / 答错）自动写进池（按 query 去重），持久累积。
  2. 回归：下次跑评测，池里 open case 一起复测，输出「回归通过率」。
  3. 毕业：fix 型 case 复测通过（top1 对了）→ open 转 graduated，移出活跃池。
  4. 锁定：lock 型 case 复测「行为变了」（软推 → 拒答）→ 报警，防误修。

池 = 「系统当前没做好的 case 全集」，跨会话累积，是「已知坑不复发」的守护。
不塞进 EVAL_SET（四档能力基线），单独一个文件——能力测整体、池防复发，职责分开。

池格式（tests/regression_<name>.json，可 git diff）：
  {
    "version": 1,
    "cases": [
      {
        "query": "狗凉",                          # 唯一键（去重依据）
        "expected": ["幼犬成长粮（皇家牌）", ...],  # 期望的正确召回/回答
        "expect_type": "fix",                     # fix=待修复（当前错）；lock=行为锁定（当前对但反直觉，防误修）
        "fail_type": "排序偏",                     # 排序偏 / 误召回 / 空返回 / 拒答
        "first_actual": "狗窝（大号）",            # 当前实际 top1（fix=错的；lock=应保持的）
        "note": "错别字「凉」→「粮」同音，期望纠正",  # 为什么进池 / 为什么锁定
        "added_at": "2026-08-26",
        "status": "open"                          # open / graduated
      }
    ]
  }
"""

import json
import os

_REGRESSION_DIR = os.path.dirname(os.path.abspath(__file__))


def pool_path(name: str) -> str:
    return os.path.join(_REGRESSION_DIR, f"regression_{name}.json")


def load_pool(name: str) -> dict:
    """读池，文件不存在 / JSON 损坏 / 结构非法都兜底为空池（手改池文件是 git diff 常态）。"""
    path = pool_path(name)
    if not os.path.exists(path):
        return {"version": 1, "cases": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            pool = json.load(f)
        if isinstance(pool, dict) and isinstance(pool.get("cases"), list):
            return pool
    except (json.JSONDecodeError, OSError):
        pass
    return {"version": 1, "cases": []}


def save_pool(name: str, pool: dict) -> str:
    path = pool_path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)
    return path


def add_failures(name: str, failures: list) -> int:
    """失败 case 回流进池（fix 型）。返回新增数。

    - 新 query → 追加 open
    - 已 open 的 query → 跳过（去重）
    - 已 graduated 的 query 复发 → 复活（改回 open + 字段刷新为最新失败），不追加重复记录

    failures 每个元素至少含 query；其余字段（expected/fail_type/first_actual/note）可选。
    """
    pool = load_pool(name)
    by_query = {c["query"]: c for c in pool["cases"]}
    added = 0
    changed = False
    for f in failures:
        q = f.get("query")
        if not q:
            continue
        case = dict(f)  # 复制，避免原地 mutate 调用方入参
        case.setdefault("expect_type", "fix")
        case.setdefault("expected", [])
        case.setdefault("fail_type", "未知")
        case.setdefault("first_actual", "")
        case.setdefault("note", "")
        case.setdefault("added_at", "")
        case["status"] = "open"
        if q in by_query:
            if by_query[q].get("status") == "graduated":
                by_query[q].update(case)  # 复活：graduated → open，字段刷新
                changed = True
            # 已 open：跳过（去重）
            continue
        pool["cases"].append(case)
        by_query[q] = case
        added += 1
        changed = True
    if changed:
        save_pool(name, pool)
    return added


def open_cases(name: str) -> list:
    """池里待回归的 case（status=open）"""
    pool = load_pool(name)
    return [c for c in pool["cases"] if c["status"] == "open"]


def graduate(name: str, passed_queries: set) -> list:
    """复测通过的 fix 型 case 毕业（open → graduated）。返回毕业的 query 列表。"""
    pool = load_pool(name)
    graduated = []
    for c in pool["cases"]:
        if c["status"] == "open" and c["expect_type"] == "fix" and c["query"] in passed_queries:
            c["status"] = "graduated"
            graduated.append(c["query"])
    if graduated:
        save_pool(name, pool)
    return graduated
