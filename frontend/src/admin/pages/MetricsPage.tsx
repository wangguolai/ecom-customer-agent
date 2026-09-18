import { useCallback, useEffect, useState } from 'react'
import { apiGet } from '../api'
import { Empty, ErrorBox, Loading, PageHead, describeError } from '../components'
import type { Metrics } from '../types'

/** 无数据显示 `—` 而不是 0：「没有数据」和「值是零」是两回事，显示 0 会读成「命中率 0%」 */
function fmt(v: number | null | undefined, suffix = '', digits = 3): string {
  if (v === null || v === undefined) return '—'
  return `${typeof v === 'number' ? v.toFixed(digits) : v}${suffix}`
}

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="admin-tile">
      <div className="admin-tile-label">{label}</div>
      <div className="admin-tile-value">{value}</div>
      {hint && <div className="admin-tile-hint">{hint}</div>}
    </div>
  )
}

export default function MetricsPage() {
  const [m, setM] = useState<Metrics | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    setLoading(true)
    setError('')
    apiGet<Metrics>('/api/admin/metrics')
      .then(setM)
      .catch((e) => setError(describeError(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(load, [load])

  return (
    <>
      <PageHead
        title="运行指标"
        desc="来自进程内 MetricsStore 的聚合快照"
        extra={<button onClick={load} disabled={loading}>刷新</button>}
      />

      {loading && <Loading />}
      {error && !loading && <ErrorBox message={error} onRetry={load} />}

      {m && !loading && !error && (
        <>
          {/* 口径必须显式展示，否则会被误读成「全量统计」 */}
          <div className="admin-note">
            口径：<b>{m.scope === 'single-process' ? '单进程' : m.scope}</b> ·
            只统计 <b>{m.sample_source === 'web' ? 'Web 对话链路' : m.sample_source}</b>
            （CLI / 评测脚本跑的对话不计入）·
            统计自 {m.sample_since ?? '—'}（<b>服务重启即清零</b>）
          </div>

          {m.samples === 0 ? (
            <Empty text="本次启动还没有样本——去对话页聊几句再回来看" />
          ) : (
            <>
              <div className="admin-tiles">
                <Tile label="样本数" value={String(m.samples)} />
                <Tile label="技术成功率" value={pct(m.tech_success_rate)}
                      hint="结束原因=正常（不含答案对错）" />
                <Tile label="规则路由占比" value={pct(m.rule_route_rate)}
                      hint="规则命中省下的 LLM 决策调用" />
                <Tile label="平均 token" value={fmt(m.tokens_avg, '', 1)} />
                <Tile label="缓存命中率" value={pct(m.cache_hit_rate)}
                      hint="前缀缓存，省钱核心指标" />
              </div>

              <h3 className="admin-sub">延迟分布</h3>
              <div className="admin-tiles">
                <Tile label="平均" value={fmt(m.latency_avg, 's')} />
                <Tile label="P50" value={fmt(m.latency_p50, 's')} hint="中位数" />
                <Tile label="P95" value={fmt(m.latency_p95, 's')} />
                <Tile label="P99" value={fmt(m.latency_p99, 's')} hint="长尾，最该盯" />
              </div>
            </>
          )}
        </>
      )}
    </>
  )
}
