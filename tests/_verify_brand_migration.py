# -*- coding: utf-8 -*-
"""品牌迁移验证（临时，零付费）——只验证 parse / facts 白名单 / judge 前缀 / seed 一致性，不调 embedding"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.domain.products import parse_products
from src.derived.facts import build_facts
from src.backend.seed import _SEED_PRODUCTS

products = parse_products()
print(f"商品数: {len(products)}")
brands = sorted({p.brand for p in products if p.brand})
print(f"品牌集合: {brands}")
assert brands == ["冠能牌", "渴望牌", "皇家牌"], f"品牌集合不对: {brands}"

facts = build_facts()
print(f"facts brands: {sorted(facts['brands'])}")
assert facts["brands"] == {"皇家牌", "冠能牌", "渴望牌"}

# judge.py 的 known_brand_prefixes = {b[:-1]}，必须全部是 2 字（否则 ([一-龥]{2})牌 抓 2 字会对不上）
prefixes = sorted({b[:-1] for b in facts["brands"]})
print(f"judge known_brand_prefixes: {prefixes}")
assert prefixes == ["冠能", "渴望", "皇家"]
assert all(len(p) == 2 for p in prefixes), f"品牌前缀必须 2 字: {prefixes}"

# md title 与 seed name 一致性（refresh 的 md↔seed 校验约束）
md_titles = {p.title for p in products}
seed_names = {sp[1] for sp in _SEED_PRODUCTS}
diff = md_titles ^ seed_names
print(f"md title == seed name: {len(diff) == 0} 条漂移")
if diff:
    print(f"漂移项: {sorted(diff)}")
assert md_titles == seed_names, f"md↔seed 漂移: {sorted(diff)}"

# 无品牌商品（零食/玩具/猫砂/用品）不应受影响
no_brand = [p.title for p in products if not p.brand]
print(f"无品牌商品数: {len(no_brand)}（零食/玩具/猫砂/用品）")

print("✅ 品牌迁移验证通过：parse / facts 白名单 / judge 前缀 / seed 一致性 全部正确")
