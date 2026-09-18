import { useRef, useState } from 'react'
import { apiGet } from '../api'
import { Empty, ErrorBox, Loading, PageHead, Pager, describeError, usePaged } from '../components'
import type { Order, OrderDetail } from '../types'

export default function OrdersPage() {
  const [qInput, setQInput] = useState('')
  const [q, setQ] = useState('')            // 已提交的查询词（与输入框分开，避免每敲一个字就请求）
  const { page, setPage, data, error, loading, reload } = usePaged<Order>('/api/admin/orders', { q })

  const [detail, setDetail] = useState<OrderDetail | null>(null)
  const [detailId, setDetailId] = useState('')
  const [detailError, setDetailError] = useState('')
  // 最近一次发起的详情请求是哪个订单 —— 用于丢弃「过期响应」
  const latestReqRef = useRef('')

  async function openDetail(orderId: string) {
    if (detailId === orderId) {                                              // 再点一次收起
      latestReqRef.current = ''
      setDetailId(''); setDetail(null); setDetailError('')
      return
    }
    latestReqRef.current = orderId
    setDetailId(orderId); setDetail(null); setDetailError('')
    try {
      const data = await apiGet<OrderDetail>(`/api/admin/orders/${encodeURIComponent(orderId)}`)
      // ⚠️ 竞态守卫：快速点 A 再点 B 时，A 的响应可能**后到**并覆盖 B 的结果，
      // 出现「面板标题是 A、B 那行的按钮却显示『收起』」的状态错乱。
      // 只认「当前仍是最近一次请求」的响应。
      if (latestReqRef.current !== orderId) return
      setDetail(data)
    } catch (e) {
      if (latestReqRef.current !== orderId) return
      setDetailError(describeError(e))
    }
  }

  return (
    <>
      <PageHead
        title="订单 / 物流"
        desc="输入纯数字按订单号精确匹配；输入文字按商品名模糊匹配"
        extra={
          <form
            className="admin-search"
            onSubmit={(e) => { e.preventDefault(); setQ(qInput.trim()); setPage(1) }}
          >
            <input placeholder="订单号或商品名" value={qInput} onChange={(e) => setQInput(e.target.value)} />
            <button type="submit">搜索</button>
            {q && <button type="button" onClick={() => { setQ(''); setQInput(''); setPage(1) }}>清除</button>}
          </form>
        }
      />

      {loading && <Loading />}
      {error && !loading && <ErrorBox message={error} onRetry={reload} />}

      {!loading && !error && data && (
        data.items.length === 0 ? (
          <Empty text={q ? `没有匹配「${q}」的订单` : '暂无订单'} />
        ) : (
          <table className="admin-table">
            <thead>
              <tr><th>订单号</th><th>商品</th><th>金额</th><th>状态</th><th>下单时间</th><th></th></tr>
            </thead>
            <tbody>
              {data.items.map((o) => (
                <tr key={o.order_id}>
                  <td className="admin-mono">{o.order_id}</td>
                  <td>{o.product}</td>
                  <td>{o.amount}</td>
                  <td><span className="admin-badge">{o.status}</span></td>
                  <td className="admin-muted">{o.created_at}</td>
                  <td>
                    <button onClick={() => openDetail(o.order_id)}>
                      {detailId === o.order_id ? '收起' : '详情'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}

      {detailError && <ErrorBox message={detailError} />}

      {detailId && !detail && !detailError && <Loading />}
      {detail && (
        <div className="admin-detail">
          <h3>物流轨迹 · {detail.order.order_id}</h3>
          {detail.traces.length === 0 ? (
            // 「订单存在但暂无物流」是**正常态**（种子数据里只有一单有轨迹）。
            // 后端刻意返回空数组而非 404，前端也就该渲染成「暂无」而不是报错。
            <Empty text="暂无物流信息" />
          ) : (
            <ul className="admin-traces">
              {detail.traces.map((t, i) => (
                <li key={i}>
                  <span className="admin-muted">{t.time}</span>
                  <span>{t.location}</span>
                  <span className="admin-badge">{t.status}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {data && data.total > 0 && (
        <Pager page={page} size={data.size} total={data.total} onChange={setPage} disabled={loading} />
      )}
    </>
  )
}
