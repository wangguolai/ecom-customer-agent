import { useCallback, useMemo, useRef, useState } from 'react'
import type { Message, ProductImage, Step, StreamEvent } from '../types'

let seq = 0
const nextId = () => ++seq

/**
 * 会话 id：localStorage 持久。
 * 刷新页面后前端消息列表是空的，但后端 Redis 里那 30 分钟的历史还在——
 * 继续问「它多少钱」仍然接得上上文。要彻底重开就点「🔄 新会话」。
 * 用 crypto.randomUUID()：浏览器原生，无需引第三方 uuid 依赖。
 */
const SESSION_KEY = 'ecom_session_id'
function getOrCreateSessionId(): string {
  let sid = localStorage.getItem(SESSION_KEY)
  if (!sid) {
    sid = crypto.randomUUID()
    localStorage.setItem(SESSION_KEY, sid)
  }
  return sid
}

/**
 * 「生成回答中」这条步骤是**前端补的**，后端不发。
 *
 * 为什么需要它：ReAct 路径在收到第一个 delta 之前，这一轮是决策轮还是答案轮
 * **不可知**（只有拿到内容才知道）。而一旦开始吐 token，侧栏还停在「思考中（第 N 步）」
 * 就是在说谎——用户已经在读答案了，侧栏却说还在思考。
 * 首个 delta 到达即追加这条（信号前端本来就有，零额外成本）。
 */
const GENERATING_TEXT = '生成回答中'

/**
 * 对话 + SSE 流式接收。
 *
 * 流式解析用「buffer 累积 + 按空行分帧」而不是 EventSource：
 * 1) 请求是 POST 带 JSON body，EventSource 只支持 GET，用不了；
 * 2) 分帧必须自己切——一个 chunk 可能含半条事件，也可能含多条，按 `\n\n` 切开后
 *    最后一段是残缺的，要留在 buffer 里等下一个 chunk 拼（`frames.pop()` 那步）。
 *    这是手写 SSE 最容易错的地方：少留这一手，长回答会在 chunk 边界处丢字。
 */
export function useChatStream() {
  const [messages, setMessages] = useState<Message[]>([])
  const [streaming, setStreaming] = useState(false)
  // 步骤只保留**当前轮**的：它是「这轮正在发生什么」的实时视图，不是历史。
  // 每轮从空开始（send/reset 里清），否则上一轮的步骤会挂在新一轮下面。
  const [stepTexts, setStepTexts] = useState<string[]>([])
  const [generating, setGenerating] = useState(false)
  const inFlight = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const sidRef = useRef<string>(getOrCreateSessionId())

  /**
   * 步骤的**终态推导**放这里，不存进 state。
   *
   * 存 state 的话要维护「哪条在跑、什么时候结束」两处真相，必然会漂移
   * （比如断开时忘了置完成，最后一步永久转圈）。推导规则只有两条：
   *   ① 只有最后一条是进行中；② `streaming` 结束 → 全部完成。
   */
  const steps: Step[] = useMemo(() => {
    const list = generating ? [...stepTexts, GENERATING_TEXT] : stepTexts
    return list.map((text, i) => ({
      text,
      done: i < list.length - 1 || !streaming,
    }))
  }, [stepTexts, generating, streaming])

  const send = useCallback(async (raw: string) => {
    const text = raw.trim()
    if (!text || inFlight.current) return
    inFlight.current = true
    setStreaming(true)

    const asstId = nextId()
    setMessages((prev) => [
      ...prev,
      { id: nextId(), role: 'user', text, images: [] },
      { id: asstId, role: 'assistant', text: '', images: [] },
    ])
    // 新轮开始：上一轮的步骤必须立即清掉，否则新旧步骤混在一起看不懂
    setStepTexts([])
    setGenerating(false)

    const patchAsst = (fn: (m: Message) => Message) =>
      setMessages((prev) => prev.map((m) => (m.id === asstId ? fn(m) : m)))
    const appendText = (d: string) => patchAsst((m) => ({ ...m, text: m.text + d }))
    const appendImages = (imgs: ProductImage[]) =>
      patchAsst((m) => ({ ...m, images: [...m.images, ...imgs] }))

    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const resp = await fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // session_id 让后端把本轮接进该会话的上下文（Redis 存历史，30 分钟滑动过期）
        body: JSON.stringify({ message: text, session_id: sidRef.current }),
        signal: ctrl.signal,
      })
      if (!resp.ok) {
        appendText(`请求失败（HTTP ${resp.status}），请确认后端已启动。`)
        return
      }
      const reader = resp.body?.getReader()
      if (!reader) {
        appendText('当前浏览器不支持流式响应。')
        return
      }

      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? '' // 末段可能是半条事件，留到下个 chunk
        for (const frame of frames) {
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6)
            if (data === '[DONE]') continue
            let obj: StreamEvent
            try {
              obj = JSON.parse(data) as StreamEvent
            } catch {
              continue
            }
            // trace_id 挂在**这条消息**上（不是组件 state）——见 types.ts 的 Message.traceId。
            // patchAsst 闭包锁的就是本轮 asstId，所以多轮之间不会串。
            if (obj.trace_id) patchAsst((m) => ({ ...m, traceId: obj.trace_id }))
            if (obj.step) setStepTexts((prev) => [...prev, obj.step!.text])
            if (obj.delta !== undefined) {
              // setState 同值时 React 直接 bail out，不会因为每个 token 调一次而多渲染
              setGenerating(true)
              appendText(obj.delta)
            }
            if (obj.images) appendImages(obj.images)
          }
        }
      }
    } catch (e) {
      // 组件卸载 / 主动中断导致的 abort 不算错误，静默收尾
      if ((e as Error).name === 'AbortError') return
      appendText(`连接异常：${(e as Error).message}`)
    } finally {
      inFlight.current = false
      abortRef.current = null
      setStreaming(false) // steps 的终态由它推导：false → 全部 done
    }
  }, [])

  /**
   * 提交评分。
   *
   * 带 `session_id` 是为了后台能看出「同一个会话里评了几次」——
   * 权威来源其实在 traces 表，但 trace 落盘失败时那行是空的（正常态），
   * 冗余一份能让后台在 trace 缺失时至少还知道是哪个会话。
   */
  const rate = useCallback(
    async (messageId: number, traceId: string, rating: 1 | -1, reason: string, comment: string) => {
      const resp = await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          trace_id: traceId,
          session_id: sidRef.current,
          rating,
          reason,
          comment,
        }),
      })
      if (!resp.ok) {
        // 与 trace 落盘相反：评分失败**必须抛出去**让用户看见。
        // 静默失败的表现是「点了没反应、刷新后也没有」——最难排查的一类现象。
        let detail = `提交失败（HTTP ${resp.status}）`
        try {
          const d = await resp.json()
          if (d && typeof d.detail === 'string') detail = d.detail
        } catch {
          /* 响应体不是 JSON，用默认文案 */
        }
        throw new Error(detail)
      }
      setMessages((prev) => prev.map((m) => (m.id === messageId ? { ...m, rated: rating } : m)))
    },
    [],
  )

  /** 开新会话：换掉 session_id（后端那条历史就此断联，30 分钟后自动过期）+ 清空界面 */
  const reset = useCallback(() => {
    if (inFlight.current) return
    localStorage.removeItem(SESSION_KEY)
    sidRef.current = getOrCreateSessionId()
    setMessages([])
    setStepTexts([])
    setGenerating(false)
  }, [])

  return { messages, streaming, steps, send, reset, rate }
}
