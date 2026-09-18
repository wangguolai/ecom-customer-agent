/** 后台数据类型 —— 与后端 /api/admin/* 的返回结构一一对应 */

export interface Paged<T> {
  total: number
  page: number
  size: number
  items: T[]
}

export interface Order {
  order_id: string
  status: string
  created_at: string
  product: string
  amount: string
}

export interface LogisticsTrace {
  time: string
  location: string
  status: string
}

export interface OrderDetail {
  order: Order
  /** 可能为空数组：「订单存在但暂无物流」是正常态，不是错误 */
  traces: LogisticsTrace[]
}

export interface Refund {
  ticket_id: string
  order_id: string
  /** 后端可能是 null（LEFT JOIN 不到订单时 product 为 null） */
  amount: number
  status: string
  created_at: string | null
  product: string | null
}

export interface MemoryUser {
  user_id: string
  active_count: number
  /** total > active 说明该用户有被删除/被缓冲挤掉的记忆 */
  total_count: number
  last_update: string
}

export interface MemoryItem {
  memory_id: string
  content: string
  category: string
  confidence: number
  raw_snippet: string | null
  created_at: string
}

export interface Metrics {
  samples: number
  /** single-process：进程内单例，多 worker 不聚合 */
  scope: string
  /** web：只有 /chat/stream 路径写入，CLI/评测不计入 */
  sample_source: string
  sample_since: string | null
  tech_success_rate?: number
  rule_route_rate?: number
  latency_avg?: number
  latency_p50?: number
  latency_p95?: number
  latency_p99?: number
  tokens_avg?: number
  /** null = 无缓存数据（与「命中率 0%」语义不同，UI 要显示 — 而不是 0%） */
  cache_hit_rate?: number | null
}
