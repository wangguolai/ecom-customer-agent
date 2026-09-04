# -*- coding: utf-8 -*-
"""Redis 缓存层回归测试 —— 缓存命中 / qty=0 真值 / 404 空标记 / 参数校验 / 前缀隔离 / 降级

用法：
  python tests/test_redis_cache.py              # 常规：缓存命中 / qty=0 真值 / 404 空标记 / 参数校验
  REDIS_DOWN=1 python tests/test_redis_cache.py  # 降级：Redis 已停，三端点仍 200 + 参数校验仍 400

前置：backend 已启动（uvicorn src.backend.main:app --port 8000）+ Redis + MySQL 在跑。
"""
import sys
import os
from urllib.parse import quote

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 确保项目根目录在 Python 路径中（直跑 python tests/xxx.py 时 sys.path[0]=tests/）
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import httpx
from src.backend import cache

BASE = "http://localhost:8000"
DOWN = os.environ.get("REDIS_DOWN") == "1"


def get(path: str):
    """带中文编码的 GET"""
    return httpx.get(BASE + path)


def main():
    if DOWN:
        print("📍 降级验证（Redis 已停）")
        # 三端点应降级查库仍 200
        for path, name in [
            ("/orders/20240818001", "订单"),
            ("/logistics/20240818001", "物流"),
            ("/products/P029", "商品"),
        ]:
            r = get(path)
            ok = r.status_code == 200
            print(f"  {'✅' if ok else '❌'} {name} {path} → {r.status_code}（期望 200）")
            assert ok, f"降级失败 {path}: {r.status_code}"
        # 参数校验在降级下仍应拦截（Redis 挂了不能放开校验）
        r = get("/orders/abc")
        ok = r.status_code == 400
        print(f"  {'✅' if ok else '❌'} 参数校验（非法 order_id）→ {r.status_code}（期望 400）")
        assert ok, f"降级时参数校验失效: {r.status_code}"
        print("✅ 降级验证通过：Redis 停后三端点 200（降级查库）+ 参数校验仍 400")
        return

    # 场景 1：缓存命中 + TTL
    print("📍 场景 1：缓存命中")
    r1 = get("/orders/20240818001")
    assert r1.status_code == 200, f"首次查询失败: {r1.status_code}"
    hit, data = cache.get_json("ecom:order:20240818001")
    assert hit and data and data.get("status") == "已发货", f"缓存未写入: hit={hit} data={data}"
    ttl = cache._redis.ttl("ecom:order:20240818001")
    assert ttl is not None and 0 < ttl <= 65, f"TTL 异常: {ttl}"  # 60 + ±5 雪崩抖动
    print(f"  ✅ 首次 miss 查 MySQL 后写缓存，TTL={ttl}s")
    r2 = get("/orders/20240818001")
    assert r2.status_code == 200 and r2.json() == r1.json(), "二次请求缓存返回不一致"
    print("  ✅ 二次请求命中缓存返回一致")

    # 场景 2：qty=0 是真实缺货，缓存真值不是空标记
    print("📍 场景 2：qty=0 真值（豆腐猫砂缺货）")
    r = get("/products/P029")
    assert r.status_code == 200 and r.json().get("qty") == 0, f"豆腐猫砂返回异常: {r.status_code} {r.text}"
    hit, data = cache.get_json("ecom:product:P029")
    assert hit and data and data.get("qty") == 0, f"qty=0 被当空标记或未缓存: hit={hit} data={data}"
    print(f"  ✅ 缓存 {data}（qty=0 是真实数据，不是 __EMPTY__）")

    # 场景 3：404 空标记 + 二次请求命中空标记
    print("📍 场景 3：404 空标记")
    miss_id = "20249999999"
    r1 = get(f"/orders/{miss_id}")
    assert r1.status_code == 404, f"不存在订单未 404: {r1.status_code}"
    hit, data = cache.get_json(f"ecom:order:{miss_id}")
    assert hit and data is None, f"空标记未写入: hit={hit} data={data}"
    print("  ✅ 404 后写入 __EMPTY__ 空标记")
    r2 = get(f"/orders/{miss_id}")
    assert r2.status_code == 404, f"二次请求未命中空标记: {r2.status_code}"
    print("  ✅ 二次请求命中空标记仍 404（不查库）")

    # 场景 5：参数校验拦截非法输入，不查库不写空标记
    print("📍 场景 5：参数校验")
    r = get("/orders/abc")
    assert r.status_code == 400, f"非法 order_id 未拦截: {r.status_code}"
    hit, _ = cache.get_json("ecom:order:abc")
    assert not hit, f"非法参数写入了缓存: hit={hit}"
    print("  ✅ 非法 order_id → 400，未写缓存")
    r = get("/products/" + quote("P" + "0" * 30))
    assert r.status_code == 400, f"超长 product_id 未拦截: {r.status_code}"
    print("  ✅ 超长 product_id（31 字）→ 400")

    # 场景 6（附带）：lifespan 的 flush 只清 ecom:* —— 验证非 ecom 前缀不被误删
    print("📍 场景 6：flush 只清 ecom:* 前缀")
    cache._redis.set("other:key", "别家数据")
    cache.flush()
    left = cache._redis.get("other:key")
    assert left == "别家数据", f"flush 误删了非 ecom 前缀: {left}"
    print("  ✅ flush 后 other:key 仍在（前缀隔离正确）")

    print("\n✅ 常规回归全部通过")


if __name__ == "__main__":
    main()
