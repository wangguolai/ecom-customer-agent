import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { apiGet } from '../api'
import { Empty, ErrorBox, Loading, PageHead, Pager, describeError, usePaged } from '../components'
import type { FeedbackDetail, FeedbackItem, TraceDetail } from '../types'

/** JSON 列可能是数组，也可能是「被截断的字符串」——如实展示，不假装解析成功 */
function JsonList({ value, empty }: { value: unknown[] | string; empty: string }) {
  if (typeof value === 'string') {
    return (
      <div className="admin-mono admin-muted">
        {value}
        <div className="admin-muted">（已截断——落盘时超长，只保留了前一段）</div>
      </div>
    )
  }
  if (value.length === 0) return <span className="admin-muted">{empty}</span>
  return (
    <div className="admin-tags">
      {value.map((v, i) => (
        <span key={i} className="admin-badge">
          {typeof v === 'object' && v !== null && 'name' in v
            ? String((v as { name: unknown }).name)
            : String(v)}
        </span>
      ))}
    </div>
  )
}

export default function FeedbackPage() {
  const { traceId } = useParams<{ traceId: string }>()
  return traceId ? <FeedbackDetailView traceId={traceId} /> : <FeedbackList />
}

/** 后端口径值 → 人话。未知值原样显示（宁可显示原始值，也不假装认识它） */
function sourceLabel(src?: string): string {
  if (src === 'web') return 'Web 对话链路（聊天页）'
  return src || 'Web 对话链路'
}

function RatingBadge({ rating }: { rating: 1 | -1 }) {
  return rating === 1
    ? <span className="admin-badge">👍 好评</span>
    : <span className="admin-badge admin-badge-bad">👎 差评</span>
}

function FeedbackList() {
  const navigate = useNavigate()
  // 默认只看差评：好评是「确认没坏」，差评才是要去看 trace 的那批。
  // 后台的第一屏应该是最需要行动的那一屏。
  const [rating, setRating] = useState<-1 | 0 | 1>(-1)
  const { page, setPage, data, error, loading, reload } = usePaged<FeedbackItem>(
    '/api/admin/feedback',
    { rating },
  )

  return (
    <>
      <PageHead
        title="反馈评分"
        desc="用户对每条回答的评价。点「查看」回溯该轮完整 trace（prompt 版本 / 检索命中 / 工具序列）"
      />

      {/* 口径标注（R9）：只有聊天页会写 feedback，CLI 与评测脚本不经过 /chat/stream。
          不标的话，看的人会以为「就这么几条评价」= 全量。
          ⚠️ 来源**取自后端返回的 sample_source**，不写死文案——口径是后端定的，
          写死的话后端一改（比如将来接了评测链路），这里的说明就变成谎言且没人会发现。
          （JSX 文本里写 Markdown 的 `**` 不会加粗，会原样显示星号——要强调用 <b>。） */}
      <div className="admin-note">
        仅统计 <b>{sourceLabel(data?.sample_source)}</b> 的评分，评测脚本与命令行不写入。
      </div>

      <div className="admin-filters">
        {([[-1, '只看差评'], [1, '只看好评'], [0, '全部']] as const).map(([v, label]) => (
          <button
            key={v}
            className={rating === v ? 'admin-filter-on' : undefined}
            onClick={() => { setRating(v); setPage(1) }}
          >
            {label}
          </button>
        ))}
      </div>

      {loading && <Loading />}
      {error && !loading && <ErrorBox message={error} onRetry={reload} />}

      {!loading && !error && data && (
        data.items.length === 0 ? (
          <Empty text={rating === -1 ? '还没有差评（好消息）' : '还没有评分记录'} />
        ) : (
          <table className="admin-table">
            <thead>
              <tr><th>时间</th><th>评分</th><th>原因</th><th>评的是哪个问题</th><th>备注</th><th></th></tr>
            </thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={f.feedback_id}>
                  <td className="admin-muted">{f.created_at}</td>
                  <td><RatingBadge rating={f.rating} /></td>
                  <td>{f.reason || <span className="admin-muted">—</span>}</td>
                  <td>
                    {/* trace 缺失是正常态：反馈收了，但回溯不到现场 */}
                    {f.has_trace
                      ? <span className="admin-mono">{f.query}</span>
                      : <span className="admin-muted">该轮记录未保存</span>}
                  </td>
                  <td className="admin-muted">{f.comment || '—'}</td>
                  <td>
                    <button onClick={() => navigate(`/admin/feedback/${f.trace_id}`)}>查看</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}

      {data && data.total > 0 && (
        <Pager page={page} size={data.size} total={data.total} onChange={setPage} disabled={loading} />
      )}
    </>
  )
}

function TraceBlock({ trace }: { trace: TraceDetail }) {
  return (
    <div className="admin-detail">
      <h3>该轮完整记录</h3>
      <dl className="admin-kv">
        <dt>用户问题</dt><dd>{trace.query}</dd>
        <dt>Agent 回答</dt><dd className="admin-prewrap">{trace.answer || '（无）'}</dd>
        <dt>路由来源</dt><dd>{trace.route_source ?? '—'}</dd>
        <dt>结束原因</dt>
        <dd>
          {trace.end_reason ?? '—'}
          {/* 带「+截断」= 工具序列/检索命中超长被截，看到的不是全量 */}
          {trace.end_reason?.includes('截断') && (
            <span className="admin-muted"> （下方工具与检索命中非全量）</span>
          )}
        </dd>
        <dt>耗时 / token</dt>
        <dd>
          {trace.total_sec ?? '—'} 秒 · {trace.total_tokens ?? 0} token
          （缓存命中 {trace.cache_hit ?? 0} / 未命中 {trace.cache_miss ?? 0}）
          {/* 推理 token 是「这轮为什么慢」的直接答案：它是 total_tokens 的子集，
              但模型把它花在了用户看不见的思维链上。迁移前的旧数据为 null，显示「—」。 */}
          {trace.reasoning_tokens != null && (
            <span className="admin-muted"> · 其中推理 {trace.reasoning_tokens}</span>
          )}
        </dd>
        <dt>LLM 步数</dt><dd>{trace.llm_steps ?? '—'}</dd>
        <dt>工具调用</dt><dd><JsonList value={trace.tool_calls} empty="未调用工具" /></dd>
        <dt>检索命中</dt><dd><JsonList value={trace.retrieved_ids} empty="未命中检索" /></dd>
        {/* prompt / 知识库版本是「能不能复现」的关键：同样的问题，prompt 改了、
            商品数据换了，答案就不一样——不记版本号，事后对着现在的代码怎么都对不上 */}
        <dt>版本</dt>
        <dd className="admin-mono">
          prompt {trace.prompt_version ?? '—'} · 知识库 {trace.kb_version ?? '—'}
        </dd>
      </dl>
    </div>
  )
}

function FeedbackDetailView({ traceId }: { traceId: string }) {
  const navigate = useNavigate()
  const [detail, setDetail] = useState<FeedbackDetail | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    let alive = true
    setDetail(null)
    setError('')
    apiGet<FeedbackDetail>(`/api/admin/feedback/${encodeURIComponent(traceId)}`)
      .then((d) => { if (alive) setDetail(d) })
      .catch((e) => { if (alive) setError(describeError(e)) })
    // 竞态守卫：快速点 A 再看 B，旧请求可能后到并覆盖新结果
    return () => { alive = false }
  }, [traceId])

  useEffect(load, [load])

  return (
    <>
      <PageHead
        title="反馈详情"
        desc={traceId}
        extra={<button onClick={() => navigate('/admin/feedback')}>← 返回列表</button>}
      />

      {error && <ErrorBox message={error} onRetry={load} />}
      {detail === null && !error && <Loading />}

      {detail && (
        <>
          <div className="admin-detail">
            <dl className="admin-kv">
              <dt>评分</dt><dd><RatingBadge rating={detail.feedback.rating} /></dd>
              <dt>原因</dt><dd>{detail.feedback.reason || '—'}</dd>
              <dt>备注</dt><dd>{detail.feedback.comment || '—'}</dd>
              <dt>首次评分</dt><dd className="admin-muted">{detail.feedback.created_at}</dd>
              <dt>最后修改</dt>
              <dd className="admin-muted">
                {detail.feedback.updated_at === detail.feedback.created_at
                  ? '未修改过'
                  : `${detail.feedback.updated_at}（用户改过主意）`}
              </dd>
              <dt>会话</dt><dd className="admin-mono">{detail.feedback.session_id || '—'}</dd>
            </dl>
          </div>

          {detail.trace
            ? <TraceBlock trace={detail.trace} />
            : (
              // ⚠️ 这不是错误：trace 落盘失败（MySQL 抖动）或请求在生成前就结束，
              // 反馈照样收得到。渲染成提示而不是报错——否则读的人会去查一个不存在的问题。
              <div className="admin-note">
                该轮 trace 没有保存下来（落盘失败或请求提前结束），无法回溯当时的检索与工具调用。
              </div>
            )}
        </>
      )}
    </>
  )
}
