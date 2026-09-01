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


async def chat_with_usage(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None):
    """单轮对话，返回 (message, usage)。usage 含 prompt_tokens/completion_tokens/total_tokens，供可观测记录 token 消耗"""
    if model is None:
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    params = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        params["tools"] = tools
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    resp = await get_client().chat.completions.create(**params)
    return resp.choices[0].message, resp.usage


async def chat(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0):
    """单轮对话，返回完整 message 对象（含 content 和 tool_calls）。兼容旧调用，不需要 usage 时用这个"""
    message, _ = await chat_with_usage(messages, tools, model, temperature)
    return message


async def stream_events(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None):
    """流式对话，产出事件序列：("text", delta) 逐 token 文本 + ("tool_calls", 完整累积列表)。

    为什么要同时产出 text 和 tool_calls：agent 的 ReAct 循环无法预知某一轮是「决策」（返回
    tool_calls）还是「最终答案」（返回 content），流式下必须边收 delta 边判断。text 逐 token
    透出（答案轮），tool_calls 在流式结束后一次性产出完整累积（决策轮）。

    流式 tool_calls 是增量分片（OpenAI 兼容）：delta.tool_calls[].function.arguments 是 JSON
    分片，必须 += 拼接；function.name 只在首个 chunk 出现（后续为 None）。按 index 累积，
    结束按 index 排序产出，返回 [{"id", "name", "arguments"}, ...] 简化结构。

    注意：流式默认不返回 usage（token 计数），调用方按估算或记 0 处理。
    """
    if model is None:
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    params = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        params["tools"] = tools
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    stream = await get_client().chat.completions.create(**params)
    tool_calls = {}  # index -> {"id", "name", "arguments"}
    async for chunk in stream:
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
