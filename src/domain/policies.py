# -*- coding: utf-8 -*-
"""政策知识库解析 —— policies.md 的唯一解析入口（SSOT → Domain 实体）

所有需要读 policies.md 的消费方（索引）都从这里拿 Policy 实体，
不再各自写正则/切片解析。改 policies.md 格式只改这里一处。
"""

import sys
import os
import re
from dataclasses import dataclass

# 确保项目根目录在 Python 路径中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

POLICIES_PATH = os.path.join(_project_root, "data", "policies.md")


@dataclass
class Policy:
    """政策实体（policies.md 的 schema 化投影）"""
    title: str          # 政策标题（## 后文本），如「7天无理由退货」
    raw_chunk: str      # 原始块文本（含「## 标题」开头，供向量化）


def _chunk_markdown(text: str) -> list[str]:
    """按 ## 标题分块，过滤掉文件一级标题等非政策块"""
    sections = re.split(r"\n(?=## )", text)
    return [s for s in sections if s.startswith("##")]


def parse_policies() -> list[Policy]:
    """读 policies.md → 切块 → 提取标题 → Policy 实体列表（唯一解析入口）"""
    with open(POLICIES_PATH, encoding="utf-8") as f:
        content = f.read()

    policies = []
    for chunk in _chunk_markdown(content):
        m = re.match(r"## (.+)", chunk)
        title = m.group(1).strip() if m else ""
        policies.append(Policy(title=title, raw_chunk=chunk))
    return policies
