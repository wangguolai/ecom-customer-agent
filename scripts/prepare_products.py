# -*- coding: utf-8 -*-
"""162 个官网商品 → demo 数据底座：补图 + 下载 + 生成 products.md + seed

步骤：
  1. 补全图片 URL（伯纳天纯单引号 bug / 麦富迪单独提图）
  2. 下载图片到 data/product_images/P{xxx}.{ext}
  3. 生成 data/products.md（标题/ID/类别/卖点/图片）
  4. 生成 seed 的 _SEED_PRODUCTS 片段（价格编合理默认，可后调）

用法：python scripts/prepare_products.py
"""
import sys
import re
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(__file__).resolve().parent
SRC = BASE / "official_products.json"
IMG_DIR = ROOT / "data" / "product_images"
PRODUCTS_MD = ROOT / "data" / "products.md"
SEED_TXT = BASE / "seed_snippet.txt"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"


def fetch(url, timeout=20):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url, timeout=20):
    return fetch(url, timeout).decode("utf-8", errors="replace")


# ── 1. 补全图片 URL ──────────────────────────────────────

def fix_purenatural_images(products):
    """伯纳天纯：重抓列表页，商品 img 是单引号 src='/uploads/allimg/...'，按标题对齐补图。"""
    html = fetch_text("https://www.en-purenatural.com/gouliang/list_68_1.html")
    html += fetch_text("https://www.en-purenatural.com/gouliang/list_68_2.html")
    # 商品条目：title + img（单引号或双引号）
    items = re.findall(r"<li>.*?</li>", html, re.S)
    title_img = {}
    for li in items:
        t = re.search(r'class="title">([^<]+)<', li)
        img = re.search(r"<img[^>]+src=['\"](/uploads/[^'\"]+)['\"]", li)
        if t and img:
            title_img[t.group(1).strip()] = "https://www.en-purenatural.com" + img.group(1)
    n = 0
    for p in products:
        if p["brand"] == "伯纳天纯" and not p["image"] and p["title"] in title_img:
            p["image"] = title_img[p["title"]]
            n += 1
    print(f"📍 伯纳天纯补图: {n} 个")


def fix_gambol_images(products):
    """麦富迪：/product/ 页商品图是独立 img，按出现顺序与商品对齐（简化：顺序补）。"""
    html = fetch_text("https://www.gambolpet.com/product/")
    imgs = re.findall(r'<img[^>]+src="(/static/upload/[^"]+)"', html)
    gambol = [p for p in products if p["brand"] == "麦富迪"]
    for i, p in enumerate(gambol):
        if i < len(imgs):
            p["image"] = "https://www.gambolpet.com" + imgs[i]
    print(f"📍 麦富迪补图: {min(len(gambol), len(imgs))} 个（顺序对齐，近似）")


# ── 2. 下载图片 ──────────────────────────────────────────

def download_images(products):
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for i, p in enumerate(products, 1):
        pid = f"P{i:03d}"
        img = p.get("image", "")
        if not img:
            continue
        ext = ".jpg"
        m = re.search(r"\.(jpe?g|png|webp)", img)
        if m:
            ext = "." + m.group(1).lower()
        dst = IMG_DIR / f"{pid}{ext}"
        try:
            data = fetch(img)
            dst.write_bytes(data)
            p["image_local"] = f"product_images/{dst.name}"
            ok += 1
        except Exception as e:
            p["image_local"] = ""
            fail += 1
            print(f"  ⚠️ 图片下载失败 {img[:60]}: {e}")
    print(f"📍 图片下载: 成功 {ok}，失败 {fail}")


# ── 3. 生成 products.md ──────────────────────────────────

def gen_products_md(products):
    lines = ["# 宠物商品知识库", ""]
    for i, p in enumerate(products, 1):
        pid = f"P{i:03d}"
        brand_suffix = f"{p['brand']}牌" if p["brand"] else ""
        title = p["title"]
        # 标题里去掉品牌名重复（麦富迪/网易严选等可能已含品牌词）
        lines.append(f"## {title}（{brand_suffix}）")
        lines.append(f"- ID：{pid}")
        lines.append(f"- 类别：{p['category']}")
        if p.get("intro"):
            lines.append(f"- 特点：{p['intro']}")
        if p.get("image_local"):
            lines.append(f"- 图片：{p['image_local']}")
        lines.append("")
    PRODUCTS_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"📍 生成 products.md: {len(products)} 个商品")


# ── 4. 生成 seed 片段 ────────────────────────────────────

def gen_seed(products):
    """生成 _SEED_PRODUCTS 片段（价格编默认：狗粮/猫粮 89~189 循环，可后调）。"""
    rows = []
    prices = [89, 99, 109, 119, 129, 139, 149, 159, 169, 189]
    for i, p in enumerate(products, 1):
        pid = f"P{i:03d}"
        price = prices[i % len(prices)]
        name = f"{p['title']}（{p['brand']}牌）"
        rows.append(f'    ("{pid}", "{name}", {price}, 100),')
    SEED_TXT.write_text("\n".join(rows), encoding="utf-8")
    print(f"📍 生成 seed 片段: {len(rows)} 行 → {SEED_TXT}")


def main():
    products = json.loads(SRC.read_text(encoding="utf-8"))
    print(f"📦 源数据 {len(products)} 个商品")
    fix_purenatural_images(products)
    fix_gambol_images(products)
    download_images(products)
    gen_products_md(products)
    gen_seed(products)
    # 回写补图后的 JSON（供后续复用）
    SRC.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✅ 完成：图片已下载，products.md + seed 片段已生成，JSON 已回写补图结果")


if __name__ == "__main__":
    main()
