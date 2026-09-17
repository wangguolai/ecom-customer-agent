# -*- coding: utf-8 -*-
"""官网宠物食品批量抓取 —— 抓品牌官网产品列表页（标题/图/卖点/系列/价格）

数据源（官网列表页，无反爬）：
  1. 皇家 royalcanin.com.cn —— /dogs/products /cats/products，page=N 翻页（101 个）
  2. 伯纳天纯 en-purenatural.com —— /gouliang/ 静态列表页（卖点 intro，30 个）
  3. 疯狂小狗 crazydoggy.cn —— /main-food-staple?product_category=N（建站宝模板，含价格）
  4. 麦富迪 gambolpet.com —— /product/（系列 + 卖点）

输出：scripts/official_products.json
"""
import sys
import re
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urljoin

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "official_products.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def fetch(url, timeout=25):
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip(s):
    return re.sub(r"\s+", " ", s).strip()


# ── 皇家 ──────────────────────────────────────────────────

def crawl_royal():
    products = []
    for path, cat in (("/dogs/products", "狗粮"), ("/cats/products", "猫粮")):
        page = 0
        while True:
            url = f"https://www.royalcanin.com.cn{path}" + (f"?page={page}" if page else "")
            html = fetch(url)
            cards = re.findall(r"<article class=\"rc-card.*?</article>", html, re.S)
            if not cards:
                break
            for c in cards:
                t = re.search(r"views-field-title[^>]*>.*?<a[^>]+>([^<]+)</a>", c, re.S)
                img = re.search(r"<img[^>]+src=\"([^\"]+)\"", c)
                link = re.search(r"<a href=\"([^\"]+)\"", c)
                if not t:
                    continue
                products.append({
                    "brand": "皇家", "category": cat,
                    "title": strip(t.group(1)),
                    "intro": "",
                    "image": urljoin("https://www.royalcanin.com.cn", img.group(1)) if img else "",
                    "link": urljoin("https://www.royalcanin.com.cn", link.group(1)) if link else "",
                })
            page += 1
            time.sleep(0.3)
    return products


# ── 伯纳天纯 ──────────────────────────────────────────────

def crawl_purenatural():
    products = []
    for path, cat in (("/gouliang/list_68_1.html", "狗粮"), ("/gouliang/list_68_2.html", "狗粮")):
        url = f"https://www.en-purenatural.com{path}"
        try:
            html = fetch(url)
        except Exception as e:
            print(f"⚠️ 伯纳天纯 {path}: {e}")
            continue
        items = re.findall(r"<li>.*?</li>", html, re.S)
        for li in items:
            title = re.search(r'class="title">([^<]+)<', li)
            intro = re.search(r'class="intro">([^<]+)<', li)
            img = re.search(r"<img[^>]+src=\"(/uploads/[^\"]+)\"", li)
            if not title:
                continue
            products.append({
                "brand": "伯纳天纯", "category": cat,
                "title": strip(title.group(1)),
                "intro": strip(intro.group(1)) if intro else "",
                "image": urljoin("https://www.en-purenatural.com", img.group(1)) if img else "",
                "link": "",
            })
        time.sleep(0.3)
    return products


# ── 疯狂小狗 ──────────────────────────────────────────────

def crawl_crazydoggy():
    products = []
    for cat_id in (54, 55, 56, 57):
        url = f"https://www.crazydoggy.cn/main-food-staple?product_category={cat_id}"
        try:
            html = fetch(url)
        except Exception as e:
            print(f"⚠️ 疯狂小狗 cat={cat_id}: {e}")
            continue
        # 商品卡片：a.productlistid 里 img data-original + p.title + p.category
        cards = re.findall(r'<a[^>]+productlistid="(\d+)"[^>]*>(.*?)</a>', html, re.S)
        got = set()
        for pid, inner in cards:
            if pid in got:
                continue
            img = re.search(r'data-original="([^"]+)"', inner)
            if not img:  # 跳过没有图占位的（标题 a 里可能无图）
                # 标题单独找
                continue
            got.add(pid)
            # 标题：找含该 product_id 的 title 链接
            m = re.search(r'class="title"[^>]*>\s*<a[^>]*productlistid="' + pid + r'"[^>]*>([^<]+)</a>', html)
            title = strip(m.group(1)) if m else f"商品{pid}"
            # 系列：category
            mc = re.search(r'class="category"[^>]*>\s*<a[^>]*>([^<]+)</a>', html)
            products.append({
                "brand": "疯狂小狗", "category": strip(mc.group(1)) if mc else "主粮",
                "title": title,
                "intro": "",
                "image": img.group(1),
                "link": f"https://www.crazydoggy.cn/page23?product_id={pid}",
            })
        time.sleep(0.3)
    return products


# ── 麦富迪 ────────────────────────────────────────────────

def crawl_gambol():
    products = []
    url = "https://www.gambolpet.com/product/"
    try:
        html = fetch(url)
    except Exception as e:
        print(f"⚠️ 麦富迪: {e}")
        return products
    # 商品：<a href="/product/{id}">系列名\n卖点</a>
    for m in re.finditer(r'<a[^>]+href="(/product/\d+)"[^>]*>(.*?)</a>', html, re.S):
        link, inner = m.group(1), m.group(2)
        text = re.sub(r"<[^>]+>", "\n", inner)
        lines = [strip(l) for l in text.split("\n") if strip(l)]
        if len(lines) < 1 or "研发中心" in lines[0] or "检测中心" in lines[0]:
            continue
        title = lines[0]
        intro = lines[1] if len(lines) > 1 else ""
        products.append({
            "brand": "麦富迪", "category": "宠物粮",
            "title": title,
            "intro": intro,
            "image": "",
            "link": urljoin("https://www.gambolpet.com", link),
        })
    # 补图：从页面 img 顺序对齐（简化：单独抓每个详情页太费，先空着）
    return products


def main():
    all_products = []
    print("📍 [1/4] 皇家")
    all_products += crawl_royal()
    print(f"   累计 {len(all_products)}")
    print("📍 [2/4] 伯纳天纯")
    all_products += crawl_purenatural()
    print(f"   累计 {len(all_products)}")
    print("📍 [3/4] 疯狂小狗")
    all_products += crawl_crazydoggy()
    print(f"   累计 {len(all_products)}")
    print("📍 [4/4] 麦富迪")
    all_products += crawl_gambol()
    print(f"   累计 {len(all_products)}")

    seen = set()
    uniq = []
    for p in all_products:
        k = (p["brand"], p["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(p)

    OUTPUT.write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
    from collections import Counter
    print(f"\n💾 共 {len(uniq)} 个商品（去重后）→ {OUTPUT}")
    print(f"   品牌: {dict(Counter(p['brand'] for p in uniq))}")
    print(f"   类别: {dict(Counter(p['category'] for p in uniq))}")
    noimg = sum(1 for p in uniq if not p["image"])
    print(f"   无图: {noimg}/{len(uniq)}")


if __name__ == "__main__":
    main()
