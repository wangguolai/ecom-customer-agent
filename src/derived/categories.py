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


GENERIC_HEADER = "## 意图识别泛义词"


def _parse_category_lines(text: str) -> dict:
    """解析 `- 类别: 词1、词2` 形式的行 → {类别: [词]}"""
    out = {}
    for m in re.finditer(r"^- (\S+):\s*(.+)$", text, flags=re.MULTILINE):
        words = [w.strip() for w in re.split(r"[、,，]", m.group(2)) if w.strip()]
        out[m.group(1)] = words
    return out


def _load_synonyms():
    """读映射表，返回 (具体词 dict, 泛义词 dict, jieba 特征词 list)

    ⚠️ **具体词与泛义词必须分段解析，不能一起扫**：两段的行格式完全相同
    （`- 类别: 词`），一起扫的话泛义词会被吃进具体词表，进而在 `detect_category`
    里跟具体词抢分（实测回归，见 `category_synonyms.md` 的说明）。
    所以主段的扫描范围**显式截断到泛义段标题为止**。
    """
    with open(SYNONYMS_PATH, encoding="utf-8") as f:
        content = f.read()

    g = content.find(GENERIC_HEADER)
    main_part = content[:g] if g >= 0 else content
    generic_part = content[g:] if g >= 0 else ""

    category_words = _parse_category_lines(main_part)
    generic_words = _parse_category_lines(generic_part)

    # jieba 特征词段：`## jieba 分词特征词` 之后、下一个 `##` 之前的每行一个词
    jieba_words = []
    seg = re.search(r"## jieba 分词特征词.*?\n(.*?)(?=\n## |\Z)", content, flags=re.S)
    if seg:
        for line in seg.group(1).split("\n"):
            w = line.strip()
            if w and not w.startswith(("#", "-")):
                jieba_words.append(w)
    return category_words, generic_words, jieba_words


def build_category_keywords() -> dict:
    """意图识别**具体词**表（类别 → 触发词），读映射表的主段"""
    category_words, _, _ = _load_synonyms()
    return category_words


def build_generic_keywords() -> dict:
    """意图识别**泛义词**表（猫/狗/猫猫/狗狗）——由 `detect_category` 在
    具体词全落空时兜底使用，**绝不与具体词同表打分**。"""
    _, generic_words, _ = _load_synonyms()
    return generic_words


def build_jieba_words() -> list:
    """jieba 词典 = 品牌名（自动，从 Product.brand）+ 特征词（映射表）"""
    brands = sorted({p.brand for p in parse_products() if p.brand})
    _, _, jieba_words = _load_synonyms()
    return brands + jieba_words
