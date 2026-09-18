import type { Message, ProductImage } from '../types'

interface Props {
  message: Message
  onPreview: (src: string) => void
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

export function ChatMessage({ message, onPreview }: Props) {
  const isUser = message.role === 'user'
  const avatar = <div className="avatar">{isUser ? '🙂' : '🐾'}</div>
  const bubble = (
    <div className="bubble">
      {message.text}
      {message.images.length > 0 && <ImageGallery images={message.images} onPreview={onPreview} />}
    </div>
  )
  // 用户消息靠右（气泡在前），助手消息靠左（头像在前）——与原版 DOM 顺序保持一致
  return (
    <div className={`msg ${message.role}`}>
      {isUser ? (
        <>
          {bubble}
          {avatar}
        </>
      ) : (
        <>
          {avatar}
          {bubble}
        </>
      )}
    </div>
  )
}
