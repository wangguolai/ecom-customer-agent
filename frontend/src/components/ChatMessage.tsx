import { Feedback } from './Feedback'
import type { Message, ProductImage } from '../types'

interface Props {
  message: Message
  onPreview: (src: string) => void
  /** 仅 assistant 消息需要：提交评分 */
  onRate: (messageId: number, traceId: string, rating: 1 | -1, reason: string, comment: string) => Promise<void>
  /** 30 秒无输入 → 给最后一条提亮评分卡片 */
  nudge?: boolean
  /** 是否属于当前轮（最后一条）。false = 用户已经发了新的，收掉没提交的评分面板 */
  active: boolean
}

/** 商品图网格：点击放大。intro 是「一句话介绍」，和 title 一起放图注里 */
function ImageGallery({ images, onPreview }: { images: ProductImage[]; onPreview: (s: string) => void }) {
  return (
    <div className="images">
      {images.map((it, i) => (
        <figure key={`${it.image}-${i}`}>
          <img src={`/${it.image}`} alt={it.title ?? ''} onClick={() => onPreview(`/${it.image}`)} />
          <figcaption>
            {it.title ?? ''}
            {it.intro && <span className="product-intro">{it.intro}</span>}
          </figcaption>
        </figure>
      ))}
    </div>
  )
}

export function ChatMessage({ message, onPreview, onRate, nudge, active }: Props) {
  const isUser = message.role === 'user'
  const avatar = <div className="avatar">{isUser ? '🙂' : '🐾'}</div>
  const bubble = (
    <div className="bubble">
      {message.text}
      {message.images.length > 0 && <ImageGallery images={message.images} onPreview={onPreview} />}
    </div>
  )
  // 用户消息靠右（气泡在前），助手消息靠左（头像在前）——与原版 DOM 顺序保持一致
  if (isUser) {
    return (
      <div className="msg user">
        {bubble}
        {avatar}
      </div>
    )
  }
  // 助手侧要多包一层：气泡下方挂评分控件。
  // 直接把评分控件塞进 .msg（flex row）会变成头像的一个横向兄弟节点，
  // 跑到头像右边去；且气泡的 max-width:78% 是相对 .msg 算的，加兄弟节点会挤压它。
  return (
    <div className="msg assistant">
      {avatar}
      <div className="msg-col">
        {bubble}
        <Feedback
          message={message}
          nudge={nudge}
          active={active}
          onSubmit={(rating, reason, comment) => onRate(message.id, message.traceId ?? '', rating, reason, comment)}
        />
      </div>
    </div>
  )
}
