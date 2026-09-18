import { useState } from 'react'
import { apiPost } from '../api'
import { Empty, ErrorBox, Loading, PageHead, Pager, describeError, usePaged } from '../components'
import type { Refund } from '../types'

/** 与后端 rules.py 的 REFUND_STATUS_* 一致（值是中文，直接透传） */
const STATUSES = ['待人工审批', '已批准', '已退款', '已拒绝'] as const

/**
 * 当前状态 → 可执行动作。
 * 与后端状态机（REFUND_TRANSITIONS）对应：
 *   待人工审批 --approve--> 已批准 --execute--> 已退款
 *   待人工审批 --reject--> 已拒绝
 * 终态（已退款/已拒绝）无动作。
 *
 * 前端据状态决定显示哪些按钮**只是体验**——真正的合法性由后端的条件 UPDATE
 * （`WHERE status=当前状态` 乐观锁）保证：并发审批或对终态操作会返回 409。
 */
const ACTIONS: Record<string, { action: string; label: string; primary?: boolean }[]> = {
  待人工审批: [
    { action: 'approve', label: '批准', primary: true },
    { action: 'reject', label: '拒绝' },
  ],
  已批准: [{ action: 'execute', label: '执行退款', primary: true }],
  已退款: [],
  已拒绝: [],
}

export default function RefundsPage() {
  const [status, setStatus] = useState('')
  const { page, setPage, data, error, loading, reload } = usePaged<Refund>('/api/admin/refunds', { status })
  const [busyId, setBusyId] = useState('')
  const [actionError, setActionError] = useState('')

  async function run(ticketId: string, action: string) {
    setBusyId(ticketId)
    setActionError('')
    try {
      if (action === 'execute') {
        await apiPost(`/refund/${ticketId}/execute`)
      } else {
        await apiPost(`/refund/${ticketId}/review`, { action })
      }
      reload()
    } catch (e) {
      // 409 = 状态已被别人改过（乐观锁拦住），429 = 限流，403 = 权限不足——分别提示
      setActionError(describeError(e))
    } finally {
      setBusyId('')
    }
  }

  return (
    <>
      <PageHead
        title="退款工单"
        desc="资金敏感操作：审批与执行都走状态机 + 乐观锁，并挂 admin 鉴权"
        extra={
          <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
            <option value="">全部状态</option>
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        }
      />

      {actionError && <ErrorBox message={actionError} />}
      {loading && <Loading />}
      {error && !loading && <ErrorBox message={error} onRetry={reload} />}

      {!loading && !error && data && (
        data.items.length === 0 ? (
          <Empty text="暂无工单（表在服务重启时会重建，属正常）" />
        ) : (
          <table className="admin-table">
            <thead>
              <tr>
                <th>工单号</th><th>订单号</th><th>商品</th><th>金额</th>
                <th>状态</th><th>申请时间</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.ticket_id}>
                  <td className="admin-mono">{r.ticket_id}</td>
                  <td className="admin-mono">{r.order_id}</td>
                  <td>{r.product ?? <span className="admin-muted">订单已不存在</span>}</td>
                  <td>¥{r.amount}</td>
                  <td><span className="admin-badge">{r.status}</span></td>
                  <td className="admin-muted">{r.created_at ?? '—'}</td>
                  <td>
                    {(ACTIONS[r.status] ?? []).length === 0
                      ? <span className="admin-muted">—</span>
                      : (ACTIONS[r.status] ?? []).map((a) => (
                          <button
                            key={a.action}
                            className={a.primary ? 'admin-btn-primary' : undefined}
                            disabled={busyId === r.ticket_id}
                            onClick={() => run(r.ticket_id, a.action)}
                          >
                            {a.label}
                          </button>
                        ))}
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
