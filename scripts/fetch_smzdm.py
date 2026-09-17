# -*- coding: utf-8 -*-
"""smzdm 多品牌宠物商品抓取（urllib 静态抓，避免 headless 验证码风控）

smzdm 分类页（fenlei/maoliang/ /fenlei/gouliang/）SSR 输出 15 个/类的好价，
标题格式「品牌 品类 规格」，如「网易严选 全价猫粮三文鱼鸡肉 T40 6kg」，
解析出品牌补足「多品牌」短板。分类页无分页，猫粮+狗粮各 15 个。

用法：python scripts/fetch_smzdm.py
"""
import sys
import re
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "smzdm_products.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

KNOWN_BRANDS = [
    "网易严选", "渴望", "Orijen", "爱肯拿", "ACANA", "冠能", "皇家", "蓝氏",
    "路斯", "乐斯尼", "味当家", "弗列加特", "伯纳天纯", "麦富迪", "疯狂小狗",
    "比瑞吉", "顽皮", "卫仕", "纽顿", "希尔斯", "耐威克", "海洋之星", "耐吉斯",
    "领先", "馋不腻", "华兴", "派得", "珍宝", "力狼", "麦德氏", "红狗", "福摩",
]

PREFIX_PAT = re.compile(r"^(?:88VIP|今日必买|淘金币可用|天猫|京东|拼多多|[、，,：:])+", re.I)


def fetch(url, timeout=20):
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_brand(title):
    t = PREFIX_PAT.sub("", title).strip()
    for b in KNOWN_BRANDS:
        if b.lower() in t.lower():
            return b
    return t.split(" ")[0][:12] if t else "未知"


def crawl():
    products = []
    for path, cat in (("/fenlei/maoliang/", "猫粮"), ("/fenlei/gouliang/", "狗粮")):
        url = f"https://www.smzdm.com{path}"
        try:
            html = fetch(url)
        except Exception as e:
            print(f"⚠️ {cat} 抓取失败: {e}")
            continue
        # 好价标题：<a href=".../p/数字/">标题</a>
        seen = set()
        got = 0
        for m in re.finditer(r'href="(https://www\.smzdm\.com/p/\d+/)"[^>]*>(.*?)</a>', html, re.S):
            href, inner = m.group(1), m.group(2)
            title = re.sub(r"<[^>]+>", "", inner)
            title = re.sub(r"\s+", " ", title).strip()
            if not title or len(title) < 5 or href in seen:
                continue
            seen.add(href)
            img = re.search(r'src="(//y\.zdmimg\.com/[^"]+)"', inner)
            brand = parse_brand(title)
            products.append({
                "brand": brand, "category": cat, "title": title,
                "intro": "", "image": ("https:" + img.group(1)) if img else "",
                "link": href,
            })
            got += 1
        print(f"📍 {cat}: {got} 个（累计 {len(products)}）")
        time.sleep(1)
    OUTPUT.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    from collections import Counter
    print(f"\n💾 共 {len(products)} 个 smzdm 商品 → {OUTPUT}")
    print(f"   品牌分布: {dict(Counter(p['brand'] for p in products))}")


if __name__ == "__main__":
    crawl()
