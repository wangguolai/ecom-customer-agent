# -*- coding: utf-8 -*-
"""配置层 —— 提示词 / 业务规则 / 参数阈值三块集中地

分三个模块，各自职责单一、互不依赖：
    prompts.py   提示词与文案（改提示词只动这里）
    rules.py     词表 / 正则 / 策略映射 / 安全白名单（加规则只动这里）
    settings.py  阈值 / 上限 / 超时 / TTL（调参只动这里）

业务代码从这里 import，不再就地定义：
    from src.config.prompts import SYSTEM_PROMPT
    from src.config.rules import HUMAN_WORDS
    from src.config.settings import MAX_STEPS

约定：本包**不 import 任何业务模块**（保持零依赖，谁都能安全引用它）。
派生数据（如类别关键词表，从 category_synonyms.md 生成）不属于这里——那是 src/derived/ 的职责。
"""
