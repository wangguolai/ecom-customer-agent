# -*- coding: utf-8 -*-
"""会话层：Web 链路的跨轮上下文（无状态应用 + 会话外置）

问题（2026-09-18 用户实测暴露）：
  `/chat/stream` 每次请求都 `new AgentSession()`，前端也不传历史 → 每轮都是全新会话。两个后果：
  ① 多轮指代接不住：「它多少钱」不知道「它」指上一轮哪款商品；
  ② **反问补参接不住**：agent 问「请提供订单号」，用户答「20240818001」——新一轮里 agent
     不知道自己在问什么，只能当普通输入重新走一遍完整 LLM 决策（用户视角：白白多烧一次调用）。

方案：session_id 由前端生成并持久（localStorage），后端按 id 把**干净对话历史**存 Redis。
  无状态应用（后端进程不持有会话）+ 会话外置（存 Redis，多实例可共享）——
  这也是「会话外置」考点的真实落地，不再只是口述。

只存什么（关键设计）：**只存 user/assistant 的文本对，不存 ReAct 中间态**。
  `messages` 在 ReAct 循环里会混入 tool_calls / tool 结果 / 规则路由的引导消息（见 agent.py
  的多处 messages.append）。跨轮保留这些中间态**无价值且有害**：工具结果是时点数据
  （库存/物流会变），跨轮复述等于把过期数据喂回上下文。中间态是「单轮内部过程」，
  跨轮只该保留「这轮聊了什么」——这个取舍也让会话历史天然瘦身。

降级：Redis 不可用 → 读写都退回空历史（当新会话）+ 不报错。
  会话是**增强**不是**依赖**：丢了上下文用户重说一遍即可，但服务不能因此挂掉。

并发：同一 session 的并发请求会互相覆盖（后写赢）。当前由前端保证串行
  （streaming 期间输入框和菜单都禁用），后端不加锁——加锁的代价对一个 demo 不值。
"""

# 会话参数集中在 src/config/settings.py（SESSION_TTL / SESSION_PREFIX / SESSION_MAX_TURNS）
from src.config.settings import SESSION_TTL, SESSION_PREFIX, SESSION_MAX_TURNS as MAX_TURNS


def _key(session_id: str) -> str:
    return f"{SESSION_PREFIX}{session_id}"


def load(session_id: str | None) -> list[dict]:
    """读会话历史。无 id / Redis 挂 / 数据坏 → 返回空列表（当新会话处理，不报错）。

    外部存储不可信：逐条校验结构，只放行 {role: user|assistant, content: str}，
    脏数据直接丢弃（防手改 Redis 或旧版本数据把非法结构喂进 messages）。
    """
    if not session_id:
        return []
    from src.backend import cache

    hit, data = cache.get_json(_key(session_id))
    if not hit or not isinstance(data, list):
        return []

    out = []
    for m in data:
        if (
            isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ):
            out.append({"role": m["role"], "content": m["content"]})
    return out[-MAX_TURNS * 2:]


def _cap(history: list[dict]) -> list[dict]:
    """按条数上限裁剪，但**摘要消息必须留下**。

    ⚠️ 为什么不能直接写 `history[-N:]`：摘要是 role=user 且位于最前，`[-N:]` 会让它
    **先于真实轮次被裁掉**；而 `_compress_history` 有「≤2 个真实 user 轮就不压」的早退
    （`agent.py`），摘要丢了就**再也回不来**——静默的上下文丢失，且不打任何警告。
    这条在压缩结果落盘之前无所谓（丢了下一轮重算），落盘之后就是不可逆的。
    """
    limit = MAX_TURNS * 2
    if len(history) <= limit:
        return history
    from src.config.prompts import SUMMARY_PREFIX

    head = history[:1] if str(history[0].get("content", "")).startswith(SUMMARY_PREFIX) else []
    return head + history[-(limit - len(head)):]


def save(session_id: str | None, history: list[dict], answer: str) -> None:
    """把**已经组装好的干净历史**写回。Redis 挂 → cache 层内部兜底（打日志不抛）。

    ⚠️ **契约变更（2026-09-20）**：调用方现在传 `AgentSession.session_history()`
    ——「摘要消息 + 已登记的轮次」，**已包含本轮问答**；不再是「原始 history 再拼本轮」。

    为什么必须变：旧写法存的是**请求开始时读到的原始 history**，而压缩只发生在
    `AgentSession.messages` 这个内存副本里 → 压缩结果每轮被丢弃 → 历史单调增长
    （2,4,6…20 条），一旦超过 `MAX_HISTORY_TOKENS` 就**每轮重新压一遍**。
    实测代价：20 条 / 2772 token，每一轮多付一次 LLM 往返（摘要的钱花两遍、收益一遍没拿）。

    `answer` 只用于**「空回复不落」的守卫**（异常/中断时不把半截回复写进上下文），
    不再参与内容组装。
    """
    if not session_id or not answer:
        return  # 没 id 就是无状态模式
    from src.backend import cache

    cache.set_json(_key(session_id), _cap(history), SESSION_TTL)
