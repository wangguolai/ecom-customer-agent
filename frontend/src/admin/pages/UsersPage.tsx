import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { apiDelete, apiGet } from '../api'
import { Empty, ErrorBox, Loading, PageHead, Pager, describeError, usePaged } from '../components'
import type { MemoryItem, MemoryUser } from '../types'

/** 用户标识展示：完整 UUID 太长，截断显示；hover 看全量 */
function UserId({ id }: { id: string }) {
  return <span className="admin-mono" title={id}>{id.length > 20 ? `${id.slice(0, 18)}…` : id}</span>
}

export default function UsersPage() {
  const { uid } = useParams<{ uid: string }>()
  return uid ? <UserDetail uid={uid} /> : <UserList />
}

function UserList() {
  const navigate = useNavigate()
  const { page, setPage, data, error, loading, reload } = usePaged<MemoryUser>('/api/admin/users')

  return (
    <>
      <PageHead
        title="用户画像"
        desc="长期记忆按 user_id 分组。标识可能是登录 uid，也可能是未登录时的会话 id（见下）"
      />

      {loading && <Loading />}
      {error && !loading && <ErrorBox message={error} onRetry={reload} />}

      {!loading && !error && data && (
        data.items.length === 0 ? (
          <Empty text="还没有沉淀任何画像（去对话页聊几句试试）" />
        ) : (
          <table className="admin-table">
            <thead>
              <tr><th>用户标识</th><th>生效中</th><th>累计</th><th>最后更新</th><th></th></tr>
            </thead>
            <tbody>
              {data.items.map((u) => (
                <tr key={u.user_id}>
                  <td><UserId id={u.user_id} /></td>
                  <td>{u.active_count}</td>
                  <td>
                    {u.total_count}
                    {/* total > active = 有被删除或被缓冲挤掉的记忆。刻意不隐藏这类用户：
                        只显示 active 的话，「删光记忆的用户」会整行消失，反而看不出删除生效了 */}
                    {u.total_count > u.active_count && (
                      <span className="admin-muted"> （{u.total_count - u.active_count} 条已删除/被挤出）</span>
                    )}
                  </td>
                  <td className="admin-muted">{u.last_update}</td>
                  <td><button onClick={() => navigate(`/admin/users/${encodeURIComponent(u.user_id)}`)}>查看</button></td>
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

function UserDetail({ uid }: { uid: string }) {
  const navigate = useNavigate()
  const [items, setItems] = useState<MemoryItem[] | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busyId, setBusyId] = useState('')

  const load = useCallback(() => {
    // 竞态守卫：uid 变化（或重复点击）时，旧请求可能后到并覆盖新 uid 的结果。
    // 返回清理函数让 useEffect 在依赖变化/卸载时把它标记为失效。
    let alive = true
    setItems(null)
    setError('')
    apiGet<{ user_id: string; items: MemoryItem[] }>(`/memory?user_id=${encodeURIComponent(uid)}`)
      .then((d) => { if (alive) setItems(d.items) })
      .catch((e) => { if (alive) setError(describeError(e)) })
    return () => { alive = false }
  }, [uid])

  useEffect(load, [load])

  async function remove(memoryId: string) {
    setBusyId(memoryId)
    setNotice('')
    try {
      const r = await apiDelete<{ derived_synced: boolean }>(`/memory/${memoryId}`)
      if (!r.derived_synced) {
        // 真源删了但 Qdrant 索引没同步 → 该条**仍可能被检索命中并注入对话**。
        // 必须显式告知，而不是假装成功（这正是这个功能最初静默失效的原因）
        setNotice('已从数据库删除，但检索索引同步失败——请在后端执行：python -m src.refresh_memory')
      }
      load()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setBusyId('')
    }
  }

  return (
    <>
      <PageHead
        title="画像详情"
        desc={uid}
        extra={<button onClick={() => navigate('/admin/users')}>← 返回列表</button>}
      />

      {/* ⚠️ 这里是 JSX 文本，不是 Markdown——写 `**加粗**` 会原样显示星号。
          （既有问题，与 FeedbackPage 同一处写法一起改；要强调请用 <b>。） */}
      <div className="admin-note">
        这个标识可能是登录用户的 uid，也可能是<b>未登录时的会话 id</b>（聊天页没有登录，
        user_id 会回落到 session_id，30 分钟过期或点「开新会话」后就会换一个）。
        多个相似标识通常意味着同一个人的多次会话。
      </div>

      {notice && <div className="admin-warn">{notice}</div>}
      {error && <ErrorBox message={error} onRetry={load} />}
      {items === null && !error && <Loading />}

      {items && (
        items.length === 0 ? (
          <Empty text="该用户当前没有生效中的画像" />
        ) : (
          <table className="admin-table">
            <thead>
              <tr><th>内容</th><th>类别</th><th>置信度</th><th>原文依据</th><th>时间</th><th></th></tr>
            </thead>
            <tbody>
              {items.map((m) => (
                <tr key={m.memory_id}>
                  <td>{m.content}</td>
                  <td><span className="admin-badge">{m.category}</span></td>
                  <td>{m.confidence.toFixed(2)}</td>
                  <td className="admin-muted">{m.raw_snippet ?? '—'}</td>
                  <td className="admin-muted">{m.created_at}</td>
                  <td>
                    <button disabled={busyId === m.memory_id} onClick={() => remove(m.memory_id)}>
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}
    </>
  )
}
