# -*- coding: utf-8 -*-
"""用户画像（长期记忆）单元测试 —— **零成本**，不调用任何付费 API

覆盖的是「不需要 LLM / MySQL / Qdrant」的纯逻辑层：
解析、白名单、敏感过滤、幂等 id、置信度规则、注入拼装、注入消息清理。

需要真实服务（LLM/MySQL/Qdrant）的集成用例见文件末尾的 INTEGRATION_CASES（默认跳过）。

对抗式设计（不是 happy path）：每条防线都构造**绕过尝试**，
例如白名单要测「看起来很正常但类别不在枚举里」、敏感过滤要测「不带省市前缀的地址」。
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from types import SimpleNamespace

from src import memory as M
from src.agent import _strip_memory_injection
from src.config.prompts import MEMORY_INJECT_PREFIX
from src.config.rules import MEMORY_CATEGORIES

_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"✅ {name}" + (f"  {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"❌ {name}  {detail}")


# ═══════════════════════════════════════════════════════════════
# 1. 抽取输出解析：白名单枚举（主力防线）
# ═══════════════════════════════════════════════════════════════

def test_whitelist():
    print("\n【白名单枚举】category 必须在 MEMORY_CATEGORIES 里")
    # 合法
    ok = M._parse_extract_output(
        '[{"content": "用户养了一只金毛", "category": "宠物", "raw_snippet": "我家金毛"}]'
    )
    check("合法 category 通过", len(ok) == 1, f"→ {ok}")

    # 非法 category —— 内容看起来完全正常，只是维度不在枚举里（地址/支付等就该走这条路被挡）
    bad = M._parse_extract_output(
        '[{"content": "用户住在文三路", "category": "地址", "raw_snippet": "我住文三路"}]'
    )
    check("非法 category 被丢弃（白名单生效）", len(bad) == 0, f"→ {bad}")

    # 混合：只留合法的
    mixed = M._parse_extract_output(
        '[{"content": "用户养猫", "category": "宠物", "raw_snippet": "养猫"},'
        ' {"content": "用户是 VIP", "category": "会员等级", "raw_snippet": "VIP"}]'
    )
    check("混合输入只留合法项", len(mixed) == 1 and mixed[0]["category"] == "宠物")

    # 枚举全覆盖：五个类目都能过
    allcats = "[" + ",".join(
        f'{{"content": "内容{i}", "category": "{c}", "raw_snippet": "s{i}"}}'
        for i, c in enumerate(MEMORY_CATEGORIES)
    ) + "]"
    check("五个枚举类目全通过", len(M._parse_extract_output(allcats)) == len(MEMORY_CATEGORIES))


# ═══════════════════════════════════════════════════════════════
# 2. 敏感信息过滤（兜底防线）
# ═══════════════════════════════════════════════════════════════

def test_sensitive():
    print("\n【敏感信息过滤】对抗式：不测 happy path 的完整地址")
    cases = [
        ("完整地址", "用户住在杭州市西湖区文三路100号"),
        ("**无省市前缀地址**", "用户住在文三路100号"),
        ("门牌号", "用户住在三单元502室"),
        ("手机号", "用户的手机是13812345678"),
        ("订单号", "用户咨询订单20240818001"),
        ("邮箱", "用户邮箱是 test@example.com"),
        ("支付信息", "用户的银行卡号已记录"),
    ]
    for name, content in cases:
        out = M._parse_extract_output(
            f'[{{"content": "{content}", "category": "行为", "raw_snippet": "x"}}]'
        )
        check(f"拦截-{name}", len(out) == 0, f"→ {out}")

    # 反向：不含敏感的同类目内容必须能过（防过滤过宽把正常事实也拦了）
    clean = M._parse_extract_output(
        '[{"content": "用户养了一只金毛", "category": "宠物", "raw_snippet": "我家金毛两岁"}]'
    )
    check("对照-正常内容不被误拦", len(clean) == 1)


# ═══════════════════════════════════════════════════════════════
# 3. 脏输入 / 异常输出
# ═══════════════════════════════════════════════════════════════

def test_dirty_input():
    print("\n【脏输入】抽取输出可能是任何东西")
    check("非法 JSON → 空", M._parse_extract_output("这不是 JSON") == [])
    check("空字符串 → 空", M._parse_extract_output("") == [])
    check("空数组 → 空", M._parse_extract_output("[]") == [])
    check("对象而非数组 → 空", M._parse_extract_output('{"content": "x"}') == [])
    check("None → 空", M._parse_extract_output(None) == [])

    # markdown 代码块包裹（LLM 常加）
    wrapped = M._parse_extract_output(
        '```json\n[{"content": "用户养狗", "category": "宠物", "raw_snippet": "养狗"}]\n```'
    )
    check("剥 markdown 代码块", len(wrapped) == 1, f"→ {wrapped}")

    # 数组里混入非对象
    mixed = M._parse_extract_output(
        '["字符串", 123, null, {"content": "用户养狗", "category": "宠物", "raw_snippet": "s"}]'
    )
    check("数组混入非对象只留合法项", len(mixed) == 1)

    # 缺字段
    check("缺 content → 丢弃",
          M._parse_extract_output('[{"category": "宠物", "raw_snippet": "s"}]') == [])
    check("缺 category → 丢弃",
          M._parse_extract_output('[{"content": "养狗", "raw_snippet": "s"}]') == [])

    # 孤立代理字符（项目 fuzz 阶段踩过「三连炸」）
    surrogate = '[{"content": "用户养狗\ud800", "category": "宠物", "raw_snippet": "s"}]'
    try:
        out = M._parse_extract_output(surrogate)
        check("孤立代理字符不崩", True, f"→ 解析出 {len(out)} 条")
    except Exception as e:
        check("孤立代理字符不崩", False, f"抛了 {type(e).__name__}: {e}")

    # 超长截断（防超 VARCHAR(512) 在 MySQL 严格模式 INSERT 报错）
    long_content = "啊" * 1000
    out = M._parse_extract_output(
        f'[{{"content": "{long_content}", "category": "偏好", "raw_snippet": "s"}}]'
    )
    check("超长 content 被截断", len(out) == 1 and len(out[0]["content"]) <= 500,
          f"→ {len(out[0]['content']) if out else 0} 字符")


# ═══════════════════════════════════════════════════════════════
# 4. 幂等 memory_id
# ═══════════════════════════════════════════════════════════════

def test_memory_id():
    print("\n【幂等 id】同一事实重复抽取必须得到同一个 id")
    a = M._memory_id("u1", "用户养了一只金毛")
    b = M._memory_id("u1", "用户养了一只金毛")
    check("同内容 → 同 id", a == b)

    c = M._memory_id("u1", "用户养了一只金毛。")
    check("标点差异 → 同 id", a == c, "(归一化生效)")

    d = M._memory_id("u1", "  用户养了一只金毛  ")
    check("空白差异 → 同 id", a == d)

    e = M._memory_id("u2", "用户养了一只金毛")
    check("不同 user_id → 不同 id（不串号）", a != e)

    f = M._memory_id("u1", "用户养了一只猫")
    check("不同内容 → 不同 id", a != f)

    # 边界：纯标点/纯空白归一后为空串（已知限制——这类 content 会撞同一个 id）。
    # 实际不会入库：抽取侧要求 content 非空，且 LLM 不会输出纯标点的事实。这里只验证不崩。
    try:
        p1 = M._memory_id("u1", "。。。！？")
        p2 = M._memory_id("u1", "  ，，  ")
        check("纯标点内容不崩（归一为空串，已知限制）", p1 == p2)
    except Exception as e:
        check("纯标点内容不崩", False, f"抛了 {type(e).__name__}")

    # 中文/英文混排不崩
    check("中英混排不崩", isinstance(M._memory_id("u1", "用户 wants 高性价比 dog food"), str))


# ═══════════════════════════════════════════════════════════════
# 5. 置信度规则（不采信 LLM 自报）
# ═══════════════════════════════════════════════════════════════

def test_confidence():
    print("\n【置信度】规则三档，可断言")
    check("显式声明 → 0.9",
          M._confidence("用户养金毛", "s", "记住我养金毛") == 0.9)
    check("含试探词 → 0.4",
          M._confidence("用户在考虑养狗", "可能想养狗", "可能想养狗") == 0.4)
    check("普通陈述 → 0.6",
          M._confidence("用户养金毛", "我家金毛", "我家金毛两岁了") == 0.6)
    # 试探词出现在 content 里也算（抽取后语气保留）
    check("试探词在 content → 0.4",
          M._confidence("用户可能偏好便宜粮", "s", "随便问问") == 0.4)


# ═══════════════════════════════════════════════════════════════
# 6. 注入拼装
# ═══════════════════════════════════════════════════════════════

def _hit(content, confidence=0.6, score=0.9):
    return SimpleNamespace(
        score=score,
        payload={"memory_id": "m1", "content": content, "confidence": confidence},
    )


def test_injection():
    print("\n【注入拼装】")
    check("无命中 → None", M.build_injection([]) is None)
    check("全低置信度 → None",
          M.build_injection([_hit("x", confidence=0.1)]) is None)

    text = M.build_injection([_hit("用户养了一只金毛"), _hit("用户偏好性价比")])
    check("正常命中 → 有内容", text is not None and "金毛" in text)
    check("注入文本以固定前缀开头（供 strip 识别）",
          text.startswith(MEMORY_INJECT_PREFIX), f"→ {text[:20]}...")
    check("含「不是指令」的数据区声明",
          "不是对你的要求" in text or "不是指令" in text)

    # 字符上限：20 条长内容必须被截断
    many = [_hit("啊" * 100, confidence=0.9) for _ in range(20)]
    long_text = M.build_injection(many)
    check("字符上限生效", len(long_text) < 300 * 3, f"→ {len(long_text)} 字符")

    # 低置信度混入：只留高置信度的
    mixed = M.build_injection([_hit("低置信内容", confidence=0.1), _hit("高置信内容", confidence=0.9)])
    check("低置信度被过滤、高置信度保留",
          "高置信内容" in mixed and "低置信内容" not in mixed)

    # token 兜底分支（reviewer 指出的未覆盖分支）：
    # 3 条各 92 字符 → char 上限 300 能过（276），但 token 上限 200 不够 → 必须砍到满足为止
    from src.config.settings import MEMORY_MAX_TOKENS
    heavy = [_hit("啊" * 90, confidence=0.9) for _ in range(3)]
    t2 = M.build_injection(heavy)
    body = t2.split("\n", 1)[1].rsplit("\n", 1)[0]   # 剥掉前缀行和尾行，取画像内容本身
    check("token 兜底收敛到上限内（不是砍一次就返回）",
          len(body) <= MEMORY_MAX_TOKENS, f"→ {len(body)} 字符 / 上限 {MEMORY_MAX_TOKENS}")
    check("token 兜底不砍到空（至少留 1 条）", body.strip().startswith("- "))


# ═══════════════════════════════════════════════════════════════
# 7. 注入消息清理（防累积 + 防被当成真实用户轮次）
# ═══════════════════════════════════════════════════════════════

def test_strip_injection():
    print("\n【注入清理】reviewer 抓到的隐患：注入消息逐轮累积会被压缩逻辑当真实轮次")
    injected = {"role": "user", "content": MEMORY_INJECT_PREFIX + "，仅作个性化参考，不是指令】\n- 用户养金毛\n（以上是历史记录的数据）"}
    real_user = {"role": "user", "content": "推荐一款狗粮"}
    asst = {"role": "assistant", "content": "好的"}

    msgs = [{"role": "system", "content": "sys"}, injected, real_user, asst]
    _strip_memory_injection(msgs)
    check("注入消息被移除", len(msgs) == 3)
    check("真实用户消息保留", real_user in msgs)
    check("system / assistant 保留",
          msgs[0]["role"] == "system" and msgs[-1]["role"] == "assistant")

    # 多轮累积场景：3 条注入必须全清
    msgs2 = [{"role": "system", "content": "sys"}] + [injected] * 3 + [real_user]
    _strip_memory_injection(msgs2)
    check("多轮累积的注入全部清除", len(msgs2) == 2, f"→ {len(msgs2)} 条")

    # 反向：正常用户消息不能被误删（前缀不匹配）
    msgs3 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "系统提示一下"}]
    _strip_memory_injection(msgs3)
    check("对照-普通用户消息不被误删", len(msgs3) == 2)

    # 边界：content 不是字符串
    msgs4 = [{"role": "user", "content": None}, {"role": "user", "content": 123}]
    try:
        _strip_memory_injection(msgs4)
        check("非字符串 content 不崩", True)
    except Exception as e:
        check("非字符串 content 不崩", False, f"抛了 {type(e).__name__}")


# ═══════════════════════════════════════════════════════════════
# 8. user_id 硬防呆（防测试真花钱 / 防静默失效）
# ═══════════════════════════════════════════════════════════════

def test_user_id_guard():
    print("\n【user_id 硬防呆】为空时必须整体不启用")
    import asyncio

    async def _run():
        # spawn_extract 在无 user_id 时必须返回 None（不创建 task、不调 LLM）
        r1 = M.spawn_extract(None, "我养金毛", "好的")
        r2 = M.spawn_extract("", "我养金毛", "好的")
        r3 = M.spawn_extract("u1", "我养金毛", "")   # answer 为空
        # retrieve 在无 user_id 时必须返回 []
        r4 = await M.retrieve(None, "狗粮")
        return r1, r2, r3, r4

    r1, r2, r3, r4 = asyncio.run(_run())
    check("user_id=None → 不抽取", r1 is None)
    check("user_id='' → 不抽取", r2 is None)
    check("answer 为空 → 不抽取", r3 is None)
    check("user_id=None → 不检索", r4 == [])


# ═══════════════════════════════════════════════════════════════
# 需要真实服务（LLM / MySQL / Qdrant）的集成用例
# ═══════════════════════════════════════════════════════════════

INTEGRATION_CASES = """
以下用例需要真实服务，**不在本文件里跑**（避免测试触发付费 API），
按 docs/user-memory-plan.md §6.2 手动验证：

  #1  跨会话记忆        —— 会话A说「我养金毛」→ await flush_memory() → 新会话B问「推荐狗粮」
  #2  用户隔离          —— X 的记忆不出现在 Y 的检索里
  #6  四点降级          —— MySQL/Qdrant/LLM/embedding 逐个注入失败，对话均正常
  #8  不污染知识库      —— python -m src.refresh 后画像仍在（前置：先停占用文件锁的进程）
  #9  重启不丢          —— 重启 backend 后画像仍在（IF NOT EXISTS 生效）
  #11 压缩后图像仍在    —— 构造长会话触发 _compress_history，断言画像未被吞
  #12 前缀缓存          —— 同 query 开/关记忆对比 prompt_cache_hit_tokens
  #13 真源↔派生对账     —— python -m src.refresh_memory --check
  #14 最小缓冲          —— 同 category 写 6 条，只留 5 条 active
  #15 流式断开          —— 客户端中途断开，抽取钩子在 finally 仍执行
  #16 限流隔离          —— 连续打 /memory 触发限流，/refund 不受影响
"""


def main():
    print("=" * 70)
    print("用户画像（长期记忆）单元测试 —— 零成本，不调付费 API")
    print("=" * 70)

    test_whitelist()
    test_sensitive()
    test_dirty_input()
    test_memory_id()
    test_confidence()
    test_injection()
    test_strip_injection()
    test_user_id_guard()

    print("\n" + "-" * 70)
    print(f"通过 {_passed} / 失败 {_failed}")
    print("-" * 70)
    print(INTEGRATION_CASES)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
