# -*- coding: utf-8 -*-
"""派生层：意图识别触发词 + jieba 词典（从 Product 实体 + 映射表生成）

- jieba 词典 = 品牌名（自动，从 Product.brand）+ 特征词（人维护，映射表）
- 意图触发词表 = 完整人维护（映射表），是「用户会怎么问」的查询侧领域知识
"""

import sys
import os
import re

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.domain.products import parse_products

SYNONYMS_PATH = os.path.join(_project_root, "data", "category_synonyms.md")


def _load_synonyms():
    """读映射表，返回 (意图触发词 dict, jieba 特征词 list)"""
    with open(SYNONYMS_PATH, encoding="utf-8") as f:
        content = f.read()

    # 意图触发词段：`- 类别: 词1、词2`（含类别名本身，作为直接命中词）
    category_words = {}
    for m in re.finditer(r"^- (\S+):\s*(.+)$", content, flags=re.MULTILINE):
        cat = m.group(1)
        words = [w.strip() for w in re.split(r"[、,，]", m.group(2)) if w.strip()]
        category_words[cat] = words

    # jieba 特征词段：`## jieba 分词特征词` 之后、下一个 `##` 之前的每行一个词
    jieba_words = []
    seg = re.search(r"## jieba 分词特征词.*?\n(.*?)(?=\n## |\Z)", content, flags=re.S)
    if seg:
        for line in seg.group(1).split("\n"):
            w = line.strip()
            if w and not w.startswith(("#", "-")):
                jieba_words.append(w)
    return category_words, jieba_words


def build_category_keywords() -> dict:
    """意图识别触发词表（类别 → 触发词），读映射表"""
    category_words, _ = _load_synonyms()
    return category_words


def build_jieba_words() -> list:
    """jieba 词典 = 品牌名（自动，从 Product.brand）+ 特征词（映射表）"""
    brands = sorted({p.brand for p in parse_products() if p.brand})
    _, jieba_words = _load_synonyms()
    return brands + jieba_words
