/** 后台数据类型 —— 与后端 /api/admin/* 的返回结构一一对应 */

export interface Paged<T> {
  total: number
  page: number
  size: number
  items: T[]
  /**
   * 口径标注：这份数据是从哪条链路采出来的（后端返回，不是前端写死的）。
   * `/api/admin/metrics` 与 `/api/admin/feedback` 都带；其余列表接口没有。
   * ⚠️ **必须消费它而不是硬编码文案**——后端改了采集口径，硬编码的说明就成了谎言。
   */
  sample_source?: string
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

export interface FeedbackItem {
  feedback_id: string
  trace_id: string
  session_id: string | null
  /** 1 = 好评，-1 = 差评 */
  rating: 1 | -1
  reason: string | null
  comment: string | null
  created_at: string
  /** 与 created_at 不同 = 用户改过主意（幂等是更新不是新增） */
  updated_at: string
  /** false = 该轮 trace 未落盘。**正常态，不是错误**——渲染成「未保存」而非报错 */
  has_trace?: boolean
  /** 列表接口 LEFT JOIN traces 带出的 query，一眼看出评的是哪个问题 */
  query?: string | null
  end_reason?: string | null
}

/** 单轮 trace 的完整记录（复现一次失败所需的全部现场） */
export interface TraceDetail {
  trace_id: string
  session_id: string | null
  user_id: string | null
  query: string
  answer: string | null
  route_source: string | null
  /** 带「+截断」后缀表示 tool_calls / retrieved_ids 不是全量 */
  end_reason: string | null
  total_sec: number | null
  total_tokens: number | null
  cache_hit: number | null
  cache_miss: number | null
  llm_steps: number | null
  /** ⚠️ 被截断时是**字符串**（截断后的 JSON 不是合法 JSON），见后端 _maybe_json_list */
  tool_calls: unknown[] | string
  retrieved_ids: unknown[] | string
  prompt_version: string | null
  kb_version: string | null
  created_at: string
}

export interface FeedbackDetail {
  feedback: FeedbackItem
  /** null = 该轮 trace 未保存（正常态，不是错误） */
  trace: TraceDetail | null
  sample_source: string
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
