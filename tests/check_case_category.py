# -*- coding: utf-8 -*-
"""评测集自检：expected 标题一致性 + 类别预过滤一致性

防的坑（2026-09-18 重建评测集时实测踩到）：
  `detect_category("皇家牌泌尿道处方粮")` 返回 **狗粮**（因为 `data/category_synonyms.md` 的
  狗粮行含「处方粮」这个触发词），而该 query 的 expected 里含猫粮商品。评测脚本会把 category
  传进 `retriever.search(...)` 做预过滤 → 猫粮商品被滤掉 → **Recall 恒掉一半，且不报警**
  （`_validate_expected` 只校验标题在不在库中，管不了类别冲突）。这是静默失败，最难发现。

两类校验：
  ① expected 标题必须逐字存在于 `data/products.md`（否则该 case 的 Recall 恒为 0）
  ② query 判出的类别必须与 expected 的类别相容（至少有一条 expected 落在该类里）

用法：
    python -m tests.check_case_category          # 自查，退出码非 0 = 有冲突
    python -m tests.check_case_category -v       # 额外打印命中预过滤的 case（信息性，非错误）

新增 / 修改 RETRIEVAL_CASES 后跑一次，别等跑完评测看到 Recall 掉才回头查。
"""

import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.cases import RETRIEVAL_CASES
from src.tools import detect_category

_PRODUCTS_MD = os.path.join(_project_root, "data", "products.md")


def load_title_categories() -> dict:
    """解析 products.md：标题 -> 类别（`## 标题` 后跟 `- 类别：X`）"""
    title_cat, cur = {}, None
    with open(_PRODUCTS_MD, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("## "):
                cur = line[3:].strip()
                title_cat[cur] = None
            elif cur and line.startswith("- 类别："):
                title_cat[cur] = line.split("：", 1)[1].strip()
    return title_cat


def check(verbose: bool = False) -> int:
    title_cat = load_title_categories()
    missing, conflicts, filtered, neg = [], [], [], 0

    for query, expected in RETRIEVAL_CASES:
        if not expected:
            neg += 1
            continue  # 负样本 expected 为空，无类别可校验
        for e in expected:
            if e not in title_cat:
                missing.append((query, e))
        cat = detect_category(query)
        if not cat:
            continue
        cats = {title_cat.get(e) for e in expected}
        if cat not in cats:
            # query 被判成某类别，但没有任何一条 expected 属于该类别 → 预过滤会把正确答案全滤掉
            conflicts.append((query, cat, sorted(c for c in cats if c)))
        else:
            filtered.append((query, cat))

    print(f"知识库标题 {len(title_cat)} 个 | 检索 case 正样本 {len(RETRIEVAL_CASES) - neg} 条、负样本 {neg} 条")
    print()

    if missing:
        print("🔴 expected 标题不在知识库（这些 case 的 Recall 恒为 0）:")
        for q, e in missing:
            print(f"   {q}  ->  {e}")
    else:
        print("✅ expected 标题全部与知识库逐字一致")

    if conflicts:
        print()
        print("🔴 类别冲突（query 判出的类别里没有一条 expected，会被预过滤误杀）:")
        for q, c, cs in conflicts:
            print(f"   {q}  ->  detect={c}，expected 类别={cs}  修法：改 query 去掉类别触发词，或收窄 expected")
    else:
        print("✅ 无类别冲突")

    if verbose and filtered:
        print()
        print("🟡 命中了类别预过滤（信息性：确认 expected 确实都落在该类里）:")
        for q, c in filtered:
            print(f"   {q}  ->  {c}")

    bad = len(missing) + len(conflicts)
    print()
    print(f"自检{'通过' if bad == 0 else f'失败（{bad} 处问题）'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(check(verbose="-v" in sys.argv))
