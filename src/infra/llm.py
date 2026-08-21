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

from openai import OpenAI

_client = None


def get_client():
    """懒加载单例 OpenAI 客户端（指向 DeepSeek）"""
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        base_url = os.getenv("DEEPSEEK_BASE_URL")
        if not api_key:
            raise RuntimeError("未找到 DEEPSEEK_API_KEY，请检查 .env")
        _client = OpenAI(api_key=api_key, base_url=base_url)
    return _client


def chat_with_usage(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None):
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
    resp = get_client().chat.completions.create(**params)
    return resp.choices[0].message, resp.usage


def chat(messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, temperature: float = 0.0):
    """单轮对话，返回完整 message 对象（含 content 和 tool_calls）。兼容旧调用，不需要 usage 时用这个"""
    message, _ = chat_with_usage(messages, tools, model, temperature)
    return message
