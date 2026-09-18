import { useEffect, useState } from 'react'
import type { Message } from '../types'

/**
 * 差评原因标签。
 *
 * ⚠️ 与后端 `src/config/rules.py` 的 `FEEDBACK_REASONS` **必须一致**：
 * 这里负责渲染，那边负责白名单校验（服务端才是权威边界，前端列表只是 UI）。
 * 漂移的表现是「标签点得出来、提交被 400」——是可发现的漂移，不是静默失效。
 *
 * 为什么客服场景**必须**有这个标签：差评可能跟答案质量完全无关
 * （物流慢、催单、情绪发泄）。只记「差评」，回流时这批样本全是噪声，
 * 分不出「agent 答错」和「快递慢」——那差评就白收了。
 */
const REASONS = ['答非所问', '信息错误', '没解决问题', '语气不好', '其他']

interface Props {
  message: Message
  /** 30 秒无输入时把卡片提亮 + 加一句引导（不是弹窗——调研明确「不 modal、不强制」） */
  nudge?: boolean
  /**
   * 这条消息是否属于**当前轮**（即它是最后一条）。
   *
   * 用途只有一个：用户发了新消息时，把**上一轮那张已展开、还没提交**的差评面板收掉。
   * 不收的话多轮之后页面上会同时挂着好几张卡片，用户不知道点哪个（R19 那条要防的正是这个）。
   *
   * ⚠️ 与「面板不因外部 re-render 收起」不冲突，两者管的是不同事件：
   *   · `active` 由 true→false = **用户显式动作**（发了新一轮）→ 该收；
   *   · 随便什么 re-render（计时器、滚动、父组件刷新）→ **不该收**，会吞掉用户已输入的内容。
   *   用 useEffect 只在 `active` 变化时触发，就精确落在这条线上。
   */
  active: boolean
  onSubmit: (rating: 1 | -1, reason: string, comment: string) => Promise<void>
}

/**
 * 评分控件：常驻在每条 assistant 消息下方（轻量图标），点击展开。
 *
 * 形态选择（用户拍板 + 调研修正）：**好评/差评 + 原因标签**，不做几颗星。
 * 星级偏差大（5 星堆在 4–5）；而客服场景真正要的是「好/坏 + 为什么」。
 */
export function Feedback({ message, nudge, active, onSubmit }: Props) {
  const [picking, setPicking] = useState(false) // 差评原因面板展开中
  const [reason, setReason] = useState('')
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [revising, setRevising] = useState(false) // 已评分后又点了「改一下」

  // 用户发了新一轮 → 收掉这张没提交的卡片（连已输入的 reason/comment 一起清，
  // 留着下次再展开时是「上次的半截输入」，比空着更让人困惑）。
  useEffect(() => {
    if (!active) {
      setPicking(false)
      setRevising(false)
      setReason('')
      setComment('')
      setErr('')
    }
  }, [active])

  // 没有 traceId 就不能评：后端按 trace_id 存反馈，发一个假 id 是往库里写脏数据，
  // 发空 id 直接被 400。**禁用而不是隐藏**——隐藏会让用户以为这功能时有时无。
  const noTrace = !message.traceId

  async function submit(rating: 1 | -1) {
    if (!message.traceId || busy) return
    setBusy(true)
    setErr('')
    try {
      await onSubmit(rating, rating === -1 ? reason : '', rating === -1 ? comment : '')
      setPicking(false)
      setRevising(false)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '提交失败')
    } finally {
      setBusy(false)
    }
  }

  // 已评分：显示终态 + 「改一下」。
  // ⚠️ 为什么必须有这个入口：后端 `upsert` 幂等设计的就是「改主意是更新不是新增」
  // （created_at 保留首评时间、updated_at 记录改动），如果 UI 只给一次机会，
  // 那条「更新」分支在生产链路上永远走不到——**误点一次👎就永久留一条脏样本**，
  // 而且后台那个「用户改过主意」的渲染分支成了永远不亮的功能。
  if (message.rated && !revising) {
    return (
      <div className="feedback feedback-done">
        <span>{message.rated === 1 ? '👍 谢谢反馈' : '👎 已记录，我们会去看这一轮发生了什么'}</span>
        <button className="feedback-revise" disabled={busy} onClick={() => setRevising(true)}>
          改一下
        </button>
      </div>
    )
  }

  return (
    <div className={`feedback${nudge ? ' feedback-nudge' : ''}`}>
      <div className="feedback-row">
        <span className="feedback-label">
          {nudge ? '这次回答有帮助吗？' : '有帮助吗'}
        </span>
        <button
          className="feedback-btn"
          disabled={noTrace || busy}
          title={noTrace ? '该轮记录未保存，无法评分' : '有帮助'}
          onClick={() => void submit(1)}
        >
          👍
        </button>
        <button
          className="feedback-btn"
          disabled={noTrace || busy}
          title={noTrace ? '该轮记录未保存，无法评分' : '没帮助'}
          // 差评**不直接提交**，先展开原因标签：差评是唯一会被拿去归因的一侧，
          // 一个没有原因的差评在飞轮里没有用处（且无法与「物流慢」区分）。
          onClick={() => setPicking((v) => !v)}
        >
          👎
        </button>
      </div>

      {/* ⚠️ 展开后**不因外部状态变化而关闭**（R19）：用户点开差评面板正选原因时，
          30 秒计时器到点或因别处 re-render 把面板收掉，等于把已输入的内容吞了。
          只有「提交成功」和「自己点取消」两条关闭路径。 */}
      {picking && (
        <div className="feedback-panel">
          <div className="feedback-reasons">
            {REASONS.map((r) => (
              <button
                key={r}
                className={`feedback-chip${reason === r ? ' feedback-chip-on' : ''}`}
                onClick={() => setReason(r)}
                disabled={busy}
              >
                {r}
              </button>
            ))}
          </div>
          <input
            className="feedback-comment"
            value={comment}
            maxLength={512}
            placeholder="补充说明（选填）"
            onChange={(e) => setComment(e.target.value)}
            disabled={busy}
          />
          <div className="feedback-actions">
            <button className="feedback-submit" disabled={!reason || busy} onClick={() => void submit(-1)}>
              {busy ? '提交中…' : '提交'}
            </button>
            <button className="feedback-cancel" disabled={busy} onClick={() => setPicking(false)}>
              取消
            </button>
          </div>
        </div>
      )}

      {err && <div className="feedback-err">{err}</div>}
    </div>
  )
}
