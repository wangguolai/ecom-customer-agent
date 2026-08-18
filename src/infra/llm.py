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


def chat(messages: list[dict], tools: Optional[list[dict]] = None, model: str = "deepseek-chat", temperature: float = 0.0):
    """单轮对话，返回完整 message 对象（含 content 和 tool_calls）"""
    params = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        params["tools"] = tools
    resp = get_client().chat.completions.create(**params)
    return resp.choices[0].message
