# -*- coding: utf-8 -*-
"""商品知识库解析 —— products.md 的唯一解析入口（SSOT → Domain 实体）

所有需要读 products.md 的消费方（索引、检索、意图识别、评测白名单）都从这里拿 Product 实体，
不再各自写正则/切片解析。改 products.md 格式只改这里一处。
"""

import sys
import os
import re
from dataclasses import dataclass, field

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PRODUCTS_PATH = os.path.join(_project_root, "data", "products.md")


@dataclass
class Product:
    """商品实体（products.md 的 schema 化投影）"""
    title: str          # 完整标题，含品牌括号，如「幼犬成长粮（贝乐牌）」
    brand: str          # 品牌名，如「贝乐牌」；无品牌为空字符串
    category: str       # 类别，如「狗粮」
    prices: list        # 价格列表（元），如 [89, 219]
    raw_chunk: str      # 原始块文本（含「## 标题」开头，供向量化）


def _chunk_markdown(text: str) -> list[str]:
    """按 ## 标题分块，过滤掉文件一级标题等非商品块"""
    sections = re.split(r"\n(?=## )", text)
    return [s for s in sections if s.startswith("##")]


def parse_products() -> list[Product]:
    """读 products.md → 切块 → 提取字段 → Product 实体列表（唯一解析入口）"""
    with open(PRODUCTS_PATH, encoding="utf-8") as f:
        content = f.read()

    products = []
    for chunk in _chunk_markdown(content):
        m = re.match(r"## (.+)", chunk)
        title = m.group(1).strip() if m else ""
        bm = re.search(r"（([^（）]+牌)）", title)
        brand = bm.group(1) if bm else ""
        cm = re.search(r"类别：(\S+)", chunk)
        category = cm.group(1).strip() if cm else ""
        prices = [int(p) for p in re.findall(r"¥\s*(\d+)", chunk)]
        products.append(Product(
            title=title, brand=brand, category=category,
            prices=prices, raw_chunk=chunk,
        ))
    return products
