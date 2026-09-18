import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { apiGet, ApiError } from './api'
import type { Paged } from './types'

/** 分页控件。页码边界由后端 Query(ge=1) 兜底，这里只做「不让用户点到越界」的体验防护。 */
export function Pager({
  page, size, total, onChange, disabled,
}: {
  page: number
  size: number
  total: number
  onChange: (p: number) => void
  disabled?: boolean
}) {
  const pages = Math.max(1, Math.ceil(total / size))
  return (
    <div className="admin-pager">
      <span>共 {total} 条 · 第 {page}/{pages} 页</span>
      <button disabled={disabled || page <= 1} onClick={() => onChange(page - 1)}>上一页</button>
      <button disabled={disabled || page >= pages} onClick={() => onChange(page + 1)}>下一页</button>
    </div>
  )
}

export function Loading() {
  return <div className="admin-empty">加载中…</div>
}

export function Empty({ text = '暂无数据' }: { text?: string }) {
  // 空态必须是**显式的空态**，不是白屏——白屏会让人以为「坏了」
  return <div className="admin-empty">{text}</div>
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="admin-error">
      {message}
      {onRetry && <button onClick={onRetry}>重试</button>}
    </div>
  )
}

/** 页面标题 + 可选右侧操作区 */
export function PageHead({ title, desc, extra }: { title: string; desc?: string; extra?: ReactNode }) {
  return (
    <div className="admin-pagehead">
      <div>
        <h2>{title}</h2>
        {desc && <p className="admin-desc">{desc}</p>}
      </div>
      {extra && <div className="admin-head-extra">{extra}</div>}
    </div>
  )
}

/** 把错误转成给用户看的话（区分 403/429/网络错误，而不是统一「加载失败」） */
export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return '权限不足：当前账号不是 admin 角色'
    if (e.status === 429) return '请求过于频繁，请稍后重试'
    return e.message
  }
  return e instanceof Error ? e.message : '未知错误'
}

/**
 * 分页数据加载 hook —— 把「加载中 / 出错 / 空 / 有数据」四种状态收敛到一处。
 *
 * 为什么抽出来：4 个列表页各写一遍加载逻辑，必然会漂移成「有的页面出错不提示、
 * 有的页面加载中显示空表」。状态处理只该有一处。
 */
export function usePaged<T>(basePath: string, query: Record<string, string | number> = {}) {
  const [page, setPage] = useState(1)
  const [data, setData] = useState<Paged<T> | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [reloadKey, setReloadKey] = useState(0)

  const qs = new URLSearchParams()
  Object.entries(query).forEach(([k, v]) => {
    if (v !== '' && v !== undefined && v !== null) qs.set(k, String(v))
  })

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')
    const url = `${basePath}?${qs.toString()}&page=${page}`
    apiGet<Paged<T>>(url)
      .then((d) => { if (alive) setData(d) })
      .catch((e) => { if (alive) setError(describeError(e)) })
      .finally(() => { if (alive) setLoading(false) })
    // 竞态防护：快速切页/改筛选时，旧请求可能后返回并覆盖新结果
    return () => { alive = false }
  }, [basePath, qs.toString(), page, reloadKey])

  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  return { page, setPage, data, error, loading, reload }
}
