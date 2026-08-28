# -*- coding: utf-8 -*-
"""规则层脏输入路由测试 —— 「订　　单绕过」+「订单号碎片」的修复前基线载体

这是「先基线后修」的基线：当前 route_by_rule 对脏输入会 miss（返回 None），
导致「订　　单」「2024 0818001」「202￥408$$$180/01」这种被空白/符号打断的输入
绕过规则层、掉进 LLM 裸奔（LLM 得靠脑补/反问去猜订单号，不可靠）。

修复目标（两件事，落地后本测试应从 FAIL 转 PASS）：
  1. 去空格副本：`re.sub(r"\s+", "", user_msg)` 删所有空白（含全角 　），
     关键词匹配 + 订单号提取都在这份副本上做；原始 user_msg 原样流转给 LLM/RAG/trace。
     依据：中文的空格不是词边界、是纯噪声（和英文相反），去空格安全甚至有益。
  2. 数字碎片清洗：`re.findall(r"\d+", ...)` 捞碎片 → 拼接 → fullmatch 验证格式，
     处理「202￥408$$$180/01」这种符号污染的订单号。

用法：python tests/test_router_dirty_input.py
⚠️ 当前（修复前）预期全部 FAIL —— 这是基线数据，不是代码意外坏了。
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.intent_router import route_by_rule


# (name, query, 期望 tool_name, 期望 order_id)
# ⚠️ 测「订　　单绕过」的 case 必须**只含「订　　单」这个词、不带「状态/发货/到哪」**——
# 因为 _ORDER_WORDS = ("状态","订单","发货")，「订　　单」被空格打断时，「状态」等词会救场，
# 路由照样走到 search_orders，测不出「订　　单」这个词本身的绕过。真实绕过只发生在
# 「用户只用『订　　单』+订单号、无其它意图词」时。
CASES = [
    ("订　　单绕过-全角空格", "我的订　　单20240818001", "search_orders", "20240818001"),
    ("订 单绕过-半角空格", "我的订 单 20240818001", "search_orders", "20240818001"),
    ("订单号-中间空格", "订单 2024 0818001 什么状态", "search_orders", "20240818001"),
    ("订单号-符号污染", "订单 202￥408$$$180/01 什么状态", "search_orders", "20240818001"),
    # 对照组：干净输入应该本来就路由成功（证明测试框架本身没问题）
    ("对照-干净输入", "订单 20240818001 什么状态", "search_orders", "20240818001"),
]


def main():
    print("=" * 74)
    print("规则层脏输入路由基线测试（修复前预期：脏输入 FAIL，对照组 PASS）")
    print("-" * 74)

    passed = failed = 0
    for name, q, want_tool, want_oid in CASES:
        r = route_by_rule(q)
        tool = r[1] if r else None
        oid = r[2].get("order_id") if (r and r[0] == "tool") else None
        ok = (tool == want_tool and oid == want_oid)
        if ok:
            passed += 1
        else:
            failed += 1
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}: route={tool} order_id={oid}  (期望 {want_tool}/{want_oid})")

    print("-" * 74)
    print(f"通过 {passed} / 失败 {failed}")
    if failed:
        print("⚠️ 脏输入 case 失败 = 修复前基线（订　　单绕过 / 订单号碎片 未修复）。")
        print("   修复「去空格副本 + 数字碎片清洗」后应全部转 PASS。")
    print("=" * 74)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
