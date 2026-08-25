# -*- coding: utf-8 -*-
"""派生层：评测白名单（从 Product 实体 + backend 种子生成，全自动）

评测的结构化层抓「编造硬事实」时，和这份白名单精确比对。
全自动生成：品牌/价格/商品名从 products.md，订单号/状态/物流从 backend 种子。
数据迁移（改 products.md / 改 _SEED_*）时，白名单自动更新，判定代码零改动。
"""

import sys
import os

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.domain.products import parse_products
from src.backend.seed import _SEED_ORDERS, _SEED_LOGISTICS, _SEED_PRODUCTS


def build_facts() -> dict:
    """评测白名单（全自动）"""
    products = parse_products()
    brands = {p.brand for p in products if p.brand}
    product_names = {p.title for p in products}
    # 价格从 MySQL seed（动态属性 SSOT）拿，不再从 products.md（价格已拆走，Product 无 prices 字段）
    prices = {sp[2] for sp in _SEED_PRODUCTS}

    order_ids = {o[0] for o in _SEED_ORDERS}
    order_statuses = {o[1] for o in _SEED_ORDERS}
    logistics_locations = {l[2] for l in _SEED_LOGISTICS}
    logistics_statuses = {l[3] for l in _SEED_LOGISTICS}

    return {
        "order_ids": order_ids,
        "brands": brands,
        "product_names": product_names,
        "prices": prices,
        "order_statuses": order_statuses,
        "logistics_locations": logistics_locations,
        "logistics_statuses": logistics_statuses,
    }
