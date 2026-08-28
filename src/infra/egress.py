# -*- coding: utf-8 -*-
"""出网限制 —— 后端地址白名单校验（Prompt Injection 架构级防御）

威胁模型：`BACKEND_URL` 从环境变量读且此前无任何校验。攻击者只要能改环境变量
（CI 变量被改、.env 泄露后被替换、容器编排配置被篡改），就能把 agent 的**全部**工具请求
导向自己的服务器 —— 订单、物流、退款请求连同其中的用户数据一起外泄，
而且返回的「数据」会被当作可信工具结果喂回 LLM（数据投毒 → 间接注入）。

这是典型的 SSRF 面：请求由我们的服务发出，目标却由外部输入决定。

⚠️ 先把话说清楚：**最有效的出网限制不是这个白名单，而是「压根不给 agent 通用 HTTP 工具」。**
TOOL_MAP 里没有 fetch_url / run_code 这类能访问任意地址的工具，agent 根本没有「上网」这个动作，
所有出网点都是我们写死的固定端点。能力最小化在前，白名单只是兜住配置被改的那一层。
"""

import sys
import os
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 允许的 host 白名单：本机 + compose 服务名。
# EGRESS_EXTRA_HOSTS 逗号分隔，给真实部署（如内网网关域名）显式追加——
# 追加是「显式动作」，和「默认放行任意 host」是两回事。
_DEFAULT_HOSTS = {"localhost", "127.0.0.1", "::1", "backend"}
_ALLOWED_SCHEMES = {"http", "https"}


def allowed_hosts() -> set:
    extra = os.environ.get("EGRESS_EXTRA_HOSTS", "")
    return _DEFAULT_HOSTS | {h.strip() for h in extra.split(",") if h.strip()}


def validate_backend_url(url: str) -> str:
    """校验后端地址合法，返回原 url；非法直接抛 ValueError（fail-closed，不静默回落默认值）。

    为什么抛而不是回落默认：静默回落会让「配置被投毒」变成一次无人察觉的降级，
    等于把安全事件吞掉。起不来 > 悄悄连到攻击者的服务器。
    """
    if not url or not isinstance(url, str):
        raise ValueError(f"BACKEND_URL 非法（空或非字符串）：{url!r}")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"BACKEND_URL 协议不允许：{parsed.scheme!r}（只允许 {sorted(_ALLOWED_SCHEMES)}）")
    host = parsed.hostname  # 用 hostname 不用 netloc：netloc 含端口和 user:pass，
    # 而 http://backend@evil.com/ 这种 userinfo 混淆正是绕过 host 校验的经典手法
    if host not in allowed_hosts():
        raise ValueError(f"BACKEND_URL 主机不在白名单：{host!r}（允许 {sorted(allowed_hosts())}）")
    return url
