import { useCallback, useEffect, useRef, useState } from 'react'
import { ChatMessage } from './components/ChatMessage'
import { Lightbox } from './components/Lightbox'
import { Menu } from './components/Menu'
import { useChatStream } from './hooks/useChatStream'
import { DEFAULT_PLACEHOLDER } from './menuItems'
import type { MenuItem } from './types'

export default function App() {
  const { messages, streaming, send, reset } = useChatStream()
  const [draft, setDraft] = useState('')
  const [preview, setPreview] = useState<string | null>(null)
  const chatRef = useRef<HTMLDivElement>(null)

  // 新消息 / 流式追加都让 messages 变新引用，跟着滚到底
  useEffect(() => {
    const el = chatRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  const submit = useCallback(
    (text: string) => {
      setDraft('')
      void send(text)
    },
    [send],
  )

  // 菜单点击 = 直接发意图句。会话层落地后「缺参数」不再是问题——
  // agent 反问、用户在下一轮补上，上下文接得住，所以不用再让用户多点一次发送。
  const pickMenu = useCallback((item: MenuItem) => submit(item.text), [submit])

  return (
    <>
      <div className="header">
        🐾 宠物电商客服 Agent
        <small>真实商品库 · 162 款在售商品</small>
      </div>

      <div className="chat" ref={chatRef}>
        {messages.map((m) => (
          <ChatMessage key={m.id} message={m} onPreview={setPreview} />
        ))}
      </div>

      <Menu onPick={pickMenu} onReset={reset} disabled={streaming} />

      <div className="input-area">
        <input
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

      {preview && <Lightbox src={preview} onClose={() => setPreview(null)} />}
    </>
  )
}
