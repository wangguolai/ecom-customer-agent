import { useCallback, useRef, useState } from 'react'
import type { Message, ProductImage, StreamEvent } from '../types'

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
  const inFlight = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const sidRef = useRef<string>(getOrCreateSessionId())

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
            if (obj.delta) appendText(obj.delta)
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
      setStreaming(false)
    }
  }, [])

  /** 开新会话：换掉 session_id（后端那条历史就此断联，30 分钟后自动过期）+ 清空界面 */
  const reset = useCallback(() => {
    if (inFlight.current) return
    localStorage.removeItem(SESSION_KEY)
    sidRef.current = getOrCreateSessionId()
    setMessages([])
  }, [])

  return { messages, streaming, send, reset }
}
