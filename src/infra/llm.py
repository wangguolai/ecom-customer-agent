# -*- coding: utf-8 -*-
"""DeepSeek LLM 调用封装 —— OpenAI 兼容接口

模型：deepseek-chat
配置从 .env 读取：DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL
"""

import sys
import os
from pathlib import Path
from typing import Optional

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(Path(_project_root) / ".env", encoding="utf-8")

from openai import AsyncOpenAI

_client = None


def get_client():
    """懒加载单例 OpenAI 客户端（指向 DeepSeek）"""
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        base_url = os.getenv("DEEPSEEK_BASE_URL")
        if not api_key:
            raise RuntimeError("未找到 DEEPSEEK_API_KEY，请检查 .env")
        # timeout 必须设：openai SDK 默认 600s，LLM 服务端挂起（不返回、不报错）时会干等 10 分钟。
        # 挂起≠报错，会被 ReAct 循环放大（每步都可能挂 10 分钟 × MAX_STEPS）。设 60s 让
        # _react_loop 的 try/except 能捕获 TimeoutError → 返回「系统异常」，而非无限干等。
        _client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        )
    return _client


def reasoning_tokens_of(usage) -> int:
    """从 usage 取推理 token 数（思考模式的思维链消耗）。取不到返回 0。

    为什么必须单独取：**推理 token 计入 completion_tokens**（官方文档），所以
    `completion_tokens` 里混着「用户看到的答案」和「用户看不到的思维链」。2026-09-20
    实测极端例：completion=1929 中 reasoning=1908 —— **99% 的输出成本花在用户看不见的
    地方**，而 project 此前完全不读这个字段，于是「20 秒只产出两行字」这类现象查不出来。

    ⚠️ 不要把它加回 completion_tokens（会重复计数）——它已经是 completion 的子集。

    字段路径：`usage.completion_tokens_details.reasoning_tokens`。SDK 未定义时
    getattr 兜底 0（与 prompt_cache_hit_tokens 同规范：缓存/推理都是服务端扩展字段）。

    ⚠️ **必须做类型校验，不能直接 `int(getattr(...) or 0)`**——这是被测试抓出来的真 bug：
    单测里 usage 是 `mock.Mock(...)`，`getattr` 会**自动造出**一个 Mock 属性（既不抛错
    也不返回默认值），`int(Mock)` 直接 TypeError，把整个 ReAct 循环打崩。
    观测代码崩掉主链路是这个项目反复记录过的一类坑（可观测是增强不是依赖）：
    取不到数就返回 0，绝不让它成为新的失败点。
    """
    details = getattr(usage, "completion_tokens_details", None)
    if details is None:
        return 0
    value = getattr(details, "reasoning_tokens", 0)
    # 非 int 一律当「无数据」。刻意不用 try/except：那是掩盖异常，
    # 而 isinstance 明确表达「这个字段只接受整数，其余视为不存在」。
    return value if isinstance(value, int) else 0


def get_model(tier: str = "fast") -> str:
    """按档取模型名。`fast` = 打底（便宜快）；`deep` = 升级档（难问题才走）。

    分层路由的**分工**：模型名是部署相关的 → 放 `.env`（`DEEPSEEK_MODEL` /
    `DEEPSEEK_MODEL_DEEP`）；**升级策略**是算法参数 → 放 `settings.py`。
    和 `settings.py` 顶部「算法参数不放 .env」的约定一致。

    分层原则 = 项目既有原则「判断交规则、表达交模型」往上推一层：
    **决策用强模型、表达/机械用快模型**。实测（2026-09-20）：
    话术生成首字 flash 1063ms vs pro 2607ms；ReAct 决策 flash 736ms vs pro 2169ms
    （两者选出的工具相同）。
    """
    if tier == "deep":
        # 没配升级档就退回打底档——**降级不能变成报错**（分层是优化不是依赖）
        return os.getenv("DEEPSEEK_MODEL_DEEP") or get_model("fast")
    return os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


# 哨兵：表示「本次调用没指定推理强度，去读 settings.REASONING_EFFORT」。
# ⚠️ **必须是独立对象，不能是字符串**——被审核查出过的一个真坑：
#   若哨兵用字符串（如 `"default"`），它会被下面的归一化映射成 `None`，而 `None` 的含义是
#   「不传参数 = 服务端默认 = **思考开启**」。于是摘要/抽取要的 `"off"` 会被静默抹掉，
#   P0 修复直接失效且不报错——**正是本批要修的那类「设了不生效」的坑**。
#   用不可与取值域混淆的 `object()`，三态才互不污染。
_USE_SETTINGS = object()

_EFFORT_DISABLED = "off"           # 关闭思考模式
_EFFORT_STRENGTHS = ("low", "high", "max")   # ⚠️ 不含 medium/xhigh：服务端把它们都映射成 high


def _normalize_effort(value):
    """把外部值归一成三态之一：`None`（不传参数）/ `"off"`（关思考）/ `"low"|"high"|"max"`。

    非法值 **raise**——宁可第一次调用就炸，也不静默发出一个语义不同的参数。

    为什么把 `""` / `"none"` / `"default"` 一律归一成 `None`：OpenAI SDK 的
    `reasoning_effort` 取值域里 **`'none'` 是合法值、语义是「不做推理」**，与我们要表达的
    「不传参数、用服务端默认(=high)」**正好相反**。拼写相近、后果相反、且不报错——
    必须在发出去之前拦死，这是本批 §4 要修的一条静默失效。
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "none", "default"):
            return None
        if s == _EFFORT_DISABLED:
            return _EFFORT_DISABLED
        if s in _EFFORT_STRENGTHS:
            return s
    raise ValueError(
        f"非法 reasoning_effort={value!r}：只接受 None / 'none' / 'default'（=不传参数，服务端默认）"
        f" / 'off'（关闭思考）/ 'low' / 'high' / 'max'。"
        f"注意 'medium' / 'xhigh' 会被服务端映射成 high，写了等于没写。"
    )


def _resolve_effort(explicit):
    """哨兵 → 读 settings；否则用显式值。

    ⚠️ 读 settings 用的是**函数内 import**，不是模块顶层——刻意的：评测脚本要做 A/B 对比
    （同一份代码跑 high / low 两轮），必须能在运行时改 `settings.REASONING_EFFORT` 后
    **立即生效**。写成模块顶层 `from ... import REASONING_EFFORT` 会把值冻在 import 那一刻。
    （代价：与 settings.py 顶部「这是 Python 模块不是运行时配置」的声明读起来有张力。
     那里针对的是「不要外置成 .env/YAML」，不是「禁止测试进程内覆盖」；生产没人运行时改它。）
    """
    if explicit is not _USE_SETTINGS:
        return _normalize_effort(explicit)
    from src.config.settings import REASONING_EFFORT
    effort = _normalize_effort(REASONING_EFFORT)
    if effort == _EFFORT_DISABLED:
        # `"off"` 允许按调用点显式传（机械任务），但**不允许落到全局默认**：
        # 全局关思考会让主循环也失去推理，实测长句答案从 358 字符塌到 62 字符（只答了一个
        # 子问题）。两处风险差一个量级，不能共用一个旋钮——这里拦死，避免哪天有人
        # 「顺手把全局也设成 off 省点钱」。
        raise ValueError(
            "settings.REASONING_EFFORT 不允许设成 'off'：全局关思考会让主循环失去推理"
            "（实测长句答案 358→62 字符）。机械任务请用 settings.REASONING_EFFORT_MECHANICAL，"
            "它按调用点生效。"
        )
    return effort


def _reasoning_params(effort) -> dict:
    """把**已归一化**的 effort 转成请求参数。`None` → `{}`（不传该参数）。"""
    if effort is None:
        return {}
    if effort == _EFFORT_DISABLED:
        # 关思考的官方形式：OpenAI 兼容端点通过 extra_body 传 thinking。
        # 实测（2026-09-20）：该参数生效，reasoning_tokens 归 0、content 正常产出。
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"reasoning_effort": effort}


async def chat_with_usage(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None, reasoning_effort=_USE_SETTINGS):
    """单轮对话，返回 (message, usage)。usage 含 prompt_tokens/completion_tokens/total_tokens，供可观测记录 token 消耗

    `reasoning_effort` **追加在签名末尾**（向后兼容，位置参数不受影响）。取值：
    省略哨兵=读 `settings.REASONING_EFFORT`；`None`=不传该参数（服务端默认=思考开启）；
    `"off"`=关闭思考；`"low"/"high"/"max"`=强度。

    ⚠️ **`max_tokens` 封顶的是 `completion_tokens`，而推理 token 计入 `completion_tokens`**
    （DeepSeek 官方文档）。所以小预算 + 思考开启 = 预算被推理吃光、`content` 返回空。
    实测：`max_tokens=300` + 默认思考 → `completion=300, reasoning=300, content=''`。
    需要"保证有正文产出"的机械任务（摘要/抽取）必须传 `"off"`。
    """
    if model is None:
        model = get_model()
    params = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    params.update(_reasoning_params(_resolve_effort(reasoning_effort)))
    if tools:
        params["tools"] = tools
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    resp = await get_client().chat.completions.create(**params)
    return resp.choices[0].message, resp.usage


async def chat(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0, reasoning_effort=_USE_SETTINGS):
    """单轮对话，返回完整 message 对象（含 content 和 tool_calls）。兼容旧调用，不需要 usage 时用这个

    ⚠️ 它原先是**位置参数直通**（`chat_with_usage(messages, tools, model, temperature)`），
    新增的 `reasoning_effort` 排在 `max_tokens` 之后，不显式转发就会被**静默丢弃**。
    这里补上——调用方（`rag_pipeline` / `demo_injection`）都是「无 tools 生成且输出直达用户」
    的路径，正是最需要能控制推理强度的地方。
    """
    message, _ = await chat_with_usage(messages, tools, model, temperature, reasoning_effort=reasoning_effort)
    return message


async def stream_events(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None, reasoning_effort=_USE_SETTINGS):
    """流式对话，产出事件序列。

    **出边界的 kind 有三种（契约，调用方必须显式列举，不能用 `else` 兜）**：
      ("text",  str)   —— 逐 token 文本，边收边产出
      ("tool_calls", list) —— 完整累积的工具调用（流式结束一次性产出，**总是在 usage 之前**）
      ("usage", obj)   —— token/cache 统计（需 include_usage；挂在最后一个 chunk 上）

    ⚠️ 第三种是后加的：早期只有 text / tool_calls，调用方普遍写成 `if text: ... else: tool_calls`。
    新增 usage 后这种写法会**静默把 usage 对象当成 tool_calls**——决策轮覆盖列表、
    答案轮（唯一的非 text 事件就是 usage）直接走错分支。本项目已把调用点改成显式三分支。

    为什么要同时产出 text 和 tool_calls：agent 的 ReAct 循环无法预知某一轮是「决策」（返回
    tool_calls）还是「最终答案」（返回 content），流式下必须边收 delta 边判断。text 逐 token
    透出（答案轮），tool_calls 在流式结束后一次性产出完整累积（决策轮）。

    流式 tool_calls 是增量分片（OpenAI 兼容）：delta.tool_calls[].function.arguments 是 JSON
    分片，必须 += 拼接；function.name 只在首个 chunk 出现（后续为 None）。按 index 累积，
    结束按 index 排序产出，返回 [{"id", "name", "arguments"}, ...] 简化结构。

    注意：流式默认不返回 usage（token 计数），调用方按估算或记 0 处理。
    """
    if model is None:
        model = get_model()
    params = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        # 让流式也拿得到 token/cache 统计（此前流式路径的 token 只能记 0）。
        # **实测确认（2026-09-18）**：DeepSeek 支持该参数，usage 挂在**最后一个 chunk** 上
        # （`choices` 长度 = 1，**不是** OpenAI 标准的空数组）——两篇资料对此说法互相矛盾，
        # 以实测为准。含 prompt_cache_hit_tokens / prompt_cache_miss_tokens。
        "stream_options": {"include_usage": True},
    }
    params.update(_reasoning_params(_resolve_effort(reasoning_effort)))
    if tools:
        params["tools"] = tools
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    stream = await get_client().chat.completions.create(**params)
    tool_calls = {}  # index -> {"id", "name", "arguments"}
    final_usage = None
    async for chunk in stream:
        # ⚠️ 必须在 `if not chunk.choices: continue` **之前**捕获 usage：
        # DeepSeek 当前把它挂在最后一个**内容块**上（choices 非空），但若某天供应商改成
        # OpenAI 标准的「空 choices 块」，先 continue 就会把统计整条漏掉。
        if getattr(chunk, "usage", None):
            final_usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            yield ("text", delta.content)
        for tc in (delta.tool_calls or []):
            acc = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                acc["id"] = tc.id
            if tc.function and tc.function.name:
                acc["name"] = tc.function.name
            if tc.function and tc.function.arguments:
                acc["arguments"] += tc.function.arguments
    if tool_calls:
        ordered = [tool_calls[i] for i in sorted(tool_calls)]
        yield ("tool_calls", ordered)
    # usage 刻意排在 tool_calls **之后**：调用方靠「有没有 tool_calls」区分决策轮/答案轮，
    # 顺序颠倒会让它把 usage 对象当成 tool_calls 列表（曾经会直接 TypeError）。
    if final_usage is not None:
        yield ("usage", final_usage)
