interface Props {
  src: string
  onClose: () => void
}

/** 大图浮层：点任意处关闭 */
export function Lightbox({ src, onClose }: Props) {
  return (
    <div className="lightbox" style={{ display: 'flex' }} onClick={onClose}>
      <img src={src} alt="" />
    </div>
  )
}
