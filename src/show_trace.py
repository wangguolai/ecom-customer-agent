# -*- coding: utf-8 -*-
"""本地查看 trace —— 替代 LangSmith 的「自己看数据」方式

用法：python src/show_trace.py "你的问题"
不加参数默认问「我的订单 20240818001 到哪了」
"""

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.agent import AgentSession


def main():
    question = sys.argv[1] if len(sys.argv) > 1 else "我的订单 20240818001 到哪了"

    session = AgentSession()
    answer = session.chat(question)
    trace = session.get_last_trace()

    print("=" * 64)
    print(f"问题：{question}")
    print(f"回答：{answer}")
    print("=" * 64)
    print("Trace 明细：")
    print()
    for _, elapsed, tokens, _prompt in trace.steps:
        print(f"  [LLM 决策] 耗时 {elapsed:.3f}s, token {tokens}")
    print()
    for name, elapsed, step, is_empty in trace.tool_calls:
        flag = "（空返回）" if is_empty else ""
        print(f"  [工具调用 第{step}步] {name} 耗时 {elapsed:.3f}s{flag}")
    for elapsed, p_tokens, c_tokens in trace.summaries:
        print(f"  [摘要压缩] 耗时 {elapsed:.3f}s, 输入 {p_tokens} token, 输出 {c_tokens} token")
    print()
    print(f"  结束原因：{trace.end_reason}")
    print("=" * 64)


if __name__ == "__main__":
    main()
