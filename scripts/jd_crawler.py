# -*- coding: utf-8 -*-
"""京东宠物食品爬虫（登录 / 探测 / 爬取）

三步走：
  1. python scripts/jd_crawler.py login    —— 扫码登录，保存 cookie（存 scripts/jd_cookies.json）
  2. python scripts/jd_crawler.py probe    —— 探测：搜「狗粮」拿第 1 个商品，dump 详情页结构
  3. python scripts/jd_crawler.py crawl    —— 批量爬 N 个（默认 30）

输出：scripts/jd_products.json —— 标题/价格/图片/类目/规格参数。
"""
import sys
import json
import time
import argparse
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
COOKIE_FILE = BASE / "jd_cookies.json"
OUTPUT_FILE = BASE / "jd_products.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def launch(p, headless=False):
    """开 browser + context，带 stealth 与已存 cookie。"""
    browser = p.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx_kwargs = dict(
        user_agent=UA, viewport={"width": 1920, "height": 1080}, locale="zh-CN",
    )
    if COOKIE_FILE.exists():
        ctx_kwargs["storage_state"] = str(COOKIE_FILE)
    context = browser.new_context(**ctx_kwargs)
    context.add_init_script(STEALTH)
    return browser, context


# ── 1. 登录 ────────────────────────────────────────────────

def do_login():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=UA, viewport={"width": 1280, "height": 800}, locale="zh-CN",
        )
        context.add_init_script(STEALTH)
        page = context.new_page()
        page.goto("https://passport.jd.com/new/login.aspx", wait_until="domcontentloaded")
        print("📍 请用京东 App 扫码登录（最多 180s）...")
        ok = False
        for _ in range(180):
            if any(c["name"] == "pt_key" for c in context.cookies()):
                print("✅ 检测到登录态 pt_key")
                ok = True
                break
            time.sleep(1)
        if not ok:
            print("⚠️ 180s 未检测到 pt_key，仍保存当前 cookie（可能登录页没加载出二维码）")
        context.storage_state(path=str(COOKIE_FILE))
        print(f"💾 cookie 已保存: {COOKIE_FILE}")
        browser.close()


# ── 2. 探测 ────────────────────────────────────────────────

def search_first_sku(context, keyword="狗粮"):
    """搜索，返回第一个商品的 sku（失败返回 None）。"""
    page = context.new_page()
    url = f"https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8"
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(3)
    lis = page.query_selector_all("li[data-sku]")
    if not lis:
        # 新版可能换选择器，尝试常见的几类
        for sel in ("li.gl-item", "div[data-sku]", ".gl-item"):
            lis = page.query_selector_all(sel)
            if lis:
                break
    sku = None
    for li in lis:
        sku = li.get_attribute("data-sku")
        if sku:
            break
    page.close()
    return sku


def do_probe():
    with sync_playwright() as p:
        browser, context = launch(p, headless=False)
        sku = search_first_sku(context)
        if not sku:
            print("❌ 搜索页没拿到 sku，可能触发风控/滑块，先确认登录态")
            browser.close()
            return
        print(f"📍 探测商品 sku = {sku}")
        page = context.new_page()
        page.goto(f"https://item.jd.com/{sku}.html", wait_until="domcontentloaded")
        time.sleep(3)

        def txt(sel):
            el = page.query_selector(sel)
            return el.inner_text().strip() if el else None

        print("\n=== 标题 ===")
        print(page.title())
        print("\n=== 候选价格选择器 ===")
        for sel in (".p-price", ".summary-price", ".price", ".p-price .price"):
            v = txt(sel)
            if v:
                print(f"  {sel}: {v}")
        print("\n=== 主图 ===")
        for sel in ("#spec-img", "#spec-n1 img", ".main-img img"):
            el = page.query_selector(sel)
            if el:
                print(f"  {sel} src={el.get_attribute('src') or el.get_attribute('data-origin')}")
        print("\n=== 面包屑类目 ===")
        for sel in (".crumb", "#crumb-wrap .item", ".breadcrumb"):
            v = txt(sel)
            if v:
                print(f"  {sel}: {v}")
        print("\n=== 规格参数（.Ptable / 参数表）===")
        for sel in (".Ptable", "#detail .Ptable", ".parameter2", ".p-parameter"):
            el = page.query_selector(sel)
            if el:
                print(f"  [{sel}] 前 800 字:\n{el.inner_text()[:800]}")
        page.close()
        browser.close()


# ── 3. 批量爬取 ────────────────────────────────────────────

def do_crawl(count=30):
    with sync_playwright() as p:
        browser, context = launch(p, headless=False)
        page = context.new_page()
        url = f"https://search.jd.com/Search?keyword={quote('狗粮')}&enc=utf-8"
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)
        lis = page.query_selector_all("li[data-sku]") or page.query_selector_all("li.gl-item")
        skus = []
        for li in lis:
            s = li.get_attribute("data-sku")
            if s and s not in skus:
                skus.append(s)
            if len(skus) >= count:
                break
        print(f"📍 搜索拿到 {len(skus)} 个 sku")
        page.close()

        products = []
        for i, sku in enumerate(skus, 1):
            print(f"📍 [{i}/{len(skus)}] 爬 sku={sku} ...")
            try:
                products.append(crawl_detail(context, sku))
            except Exception as e:
                print(f"  ⚠️ sku={sku} 失败: {e}")
            time.sleep(2)  # 控制频率
        OUTPUT_FILE.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"💾 已保存 {len(products)} 条 → {OUTPUT_FILE}")
        browser.close()


def crawl_detail(context, sku):
    page = context.new_page()
    page.goto(f"https://item.jd.com/{sku}.html", wait_until="domcontentloaded")
    time.sleep(2)

    def txt(sel):
        el = page.query_selector(sel)
        return el.inner_text().strip() if el else ""

    title = page.title()
    price = txt(".p-price") or txt(".summary-price")
    img_el = page.query_selector("#spec-img")
    img = (img_el.get_attribute("src") or img_el.get_attribute("data-origin")) if img_el else ""
    category = txt(".crumb")
    spec = txt(".Ptable") or txt("#detail .Ptable")
    page.close()
    return {"sku": sku, "title": title, "price": price, "image": img,
            "category": category, "spec": spec}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["login", "probe", "crawl"])
    ap.add_argument("-n", "--count", type=int, default=30)
    args = ap.parse_args()
    if args.cmd == "login":
        do_login()
    elif args.cmd == "probe":
        do_probe()
    else:
        do_crawl(args.count)


if __name__ == "__main__":
    main()
