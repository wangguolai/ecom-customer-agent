import { useCallback, useEffect, useRef, useState } from 'react'
import { ChatMessage } from './components/ChatMessage'
import { Lightbox } from './components/Lightbox'
import { Menu } from './components/Menu'
import { useChatStream } from './hooks/useChatStream'
import { DEFAULT_PLACEHOLDER } from './menuItems'
import { TEST_ORDERS } from './orderIds'
import type { MenuItem } from './types'

/**
 * 无输入多少秒后提示评分。
 *
 * **单一真源就是这里**：计时完全发生在浏览器（后端既不参与也不感知）。
 * 刻意**不**在后端 `config/settings.py` 再放一个同名常量——那会造出「改了没反应」的
 * 排查黑洞（改 Python 那边不会影响这里），死配置比没有配置更坑。
 */
const FEEDBACK_IDLE_SECONDS = 30

export default function App() {
  const { messages, streaming, steps, send, reset, rate } = useChatStream()
  const [draft, setDraft] = useState('')
  const [preview, setPreview] = useState<string | null>(null)
  const [idle, setIdle] = useState(false)
  // 只用来**触发计时器 effect 重跑**的计数器。切回前台时 +1，让 30 秒从头开始计。
  const [visibleTick, setVisibleTick] = useState(0)
  const chatRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const last = messages.length > 0 ? messages[messages.length - 1] : null

  // 新消息 / 流式追加 / 步骤变化都让 messages 或 steps 变新引用，跟着滚到底
  useEffect(() => {
    const el = chatRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, steps])

  /**
   * 30 秒无输入 → 提亮最后一条的评分卡片（**内联，不是弹窗**——调研明确「不 modal、不强制」）。
   *
   * 触发边界的每一条都在守卫里，不是「能跑就行」：
   *  · streaming 中不计时——还在生成，不存在「等太久」；
   *  · 最后一条不是 assistant（刚发出去还没回）不计时；
   *  · 没有 traceId（该轮没落盘）或**已评分**不计时——评完还催是骚扰；
   *  · `draft` 进依赖 = **打字算活动**，每敲一个字重置计时。
   *    这条是「什么算活动」的答案：用户还在输入，说明他还在参与，此时弹卡片是打断。
   */
  useEffect(() => {
    if (streaming || !last || last.role !== 'assistant' || !last.traceId || last.rated) {
      setIdle(false)
      return
    }
    setIdle(false)
    const t = window.setTimeout(() => setIdle(true), FEEDBACK_IDLE_SECONDS * 1000)
    return () => window.clearTimeout(t)
    // visibleTick 进依赖是**必须的**，不是多余：
    // 切回前台时如果只 setIdle(false) 而不重挂计时器，那就永久关掉了这条消息的提示
    // （依赖没变 → effect 不重跑 → 再也不会弹）。加上它，语义才是「切回来 = 重新计 30 秒」。
  }, [last, streaming, draft, visibleTick])

  // 后台标签页里定时器会被浏览器**节流**（Chrome 后台最小 ~1s 且不保证），
  // 「30 秒」在后台根本不可信。切回前台时**重置并重新计时**，避免「人刚切回来就弹卡片」——
  // 那不是「用户等了 30 秒」，是用户压根没在等。
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        setIdle(false)
        setVisibleTick((t) => t + 1) // 让上面的 effect 重跑，重新开始计时
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [])

  const submit = useCallback(
    (text: string, menuIntent?: string) => {
      setDraft('')
      void send(text, menuIntent)
    },
    [send],
  )

  // 菜单点击 = 直接发意图句，**并带上菜单 id**。
  //
  // 带 id 的原因（2026-09-20 实测）：不带的话服务端只能把「查订单」当普通消息，
  // 规则层要求「订单号 + 关键词」双命中，裸意图词拦不住 → 掉 LLM 决策，
  // 实测一次 6.5 秒、工具一次没调（就为了反问一句「你要查哪个订单」）。
  // 带上 id 后服务端走确定性路由：有参数直通工具 / 没参数**固定反问（零 LLM）**。
  //
  // ⚠️ 只传**菜单 id**，不传工具名——`menu_intent` 是客户端可控输入，
  // 传工具名等于给任意工具调用开入口。白名单在 `src/config/rules.py`。
  const pickMenu = useCallback((item: MenuItem) => submit(item.text, item.intent), [submit])

  // 侧栏点订单号 = **只填入输入框，不发送**。
  // 与伪菜单刻意不同：伪菜单给的是完整意图（「查订单」），用户不需要改；
  // 侧栏给的是**参数**，用户通常还要补一句（想查物流就把「查订单」改成「查物流」）。
  // ⚠️ 填入的是**规范意图句**而不是裸订单号：`intent_router` 要求「订单号 + 订单/物流词」
  // 同时命中才走规则路由，裸数字会掉进 LLM ReAct（慢且不确定）。
  const pickOrder = useCallback((orderId: string) => {
    setDraft(`查订单 ${orderId}`)
    // 填完把焦点交给输入框。不交的话焦点留在侧栏按钮上，
    // 用户按回车触发的是**按钮的默认 click**（再填一次 draft），而不是发送——
    // 等于还要再点一下输入框才走得通，正是「伪菜单还要多点一次」那类多余的摩擦。
    inputRef.current?.focus()
  }, [])

  return (
    <>
      <aside className="sidebar">
        {/* 执行步骤区：实时显示「这一轮 agent 在做什么」。
            数据来自 SSE 的 step 事件（后端 agent.py），每轮从空开始。 */}
        <div className="sidebar-title">执行步骤</div>
        {steps.length === 0 ? (
          <div className="sidebar-hint">发一条消息，这里显示处理过程</div>
        ) : (
          <ol className="sidebar-steps">
            {steps.map((s, i) => (
              <li key={i} className={s.done ? 'step-done' : 'step-running'}>
                {s.text}
              </li>
            ))}
          </ol>
        )}

        <div className="sidebar-title sidebar-title-gap">测试订单</div>
        <div className="sidebar-hint">点一下填入输入框</div>
        {TEST_ORDERS.map((o) => (
          <button
            key={o.id}
            className="sidebar-order"
            onClick={() => pickOrder(o.id)}
            disabled={streaming}
            title={o.hint}
          >
            <span className="sidebar-order-id">{o.id}</span>
            <span className="sidebar-order-status">{o.status} · {o.hint}</span>
          </button>
        ))}
      </aside>

      {/* 主区：加侧栏后，header/chat/menu/input 必须包进一个 column 容器，
          否则它们会变成 #root（row flex）的横向兄弟节点。 */}
      <div className="app-main">
        <div className="header">
          🐾 宠物电商客服 Agent
          <small>真实商品库 · 162 款在售商品</small>
        </div>

        <div className="chat" ref={chatRef}>
          {messages.map((m) => (
            <ChatMessage
              key={m.id}
              message={m}
              onPreview={setPreview}
              onRate={rate}
              nudge={idle && m.id === last?.id}
              // 「新轮开始即移除旧的未提交卡片」（R19）：`active` 由 true→false 时
              // Feedback 内部会收掉已展开的差评面板。**光靠 nudge 不够**——nudge 只管
              // 「提不提亮」，管不到「已经点开、reason 都填了一半」的那张卡片的开关状态。
              active={m.id === last?.id}
            />
          ))}
        </div>

        <Menu onPick={pickMenu} onReset={reset} disabled={streaming} />

        <div className="input-area">
          <input
            ref={inputRef}
            value={draft}
            placeholder={DEFAULT_PLACEHOLDER}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit(draft)
            }}
          />
          <button disabled={streaming} onClick={() => submit(draft)}>
            发送
          </button>
        </div>
      </div>

      {preview && <Lightbox src={preview} onClose={() => setPreview(null)} />}
    </>
  )
}
