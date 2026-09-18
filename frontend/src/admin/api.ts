/**
 * 后台请求封装 —— token 注入 + 401/403/429 统一分流
 *
 * 为什么集中在这里：每个页面各写一遍 fetch + token + 错误处理，必然会漂移成
 * 「有的页面 401 会跳登录、有的不会」。状态处理只该有一处。
 *
 * ⚠️ 登录页**不走这里**（它用裸 fetch）——这是刻意的：
 * 如果登录请求也走 401 处理，登录失败会触发「清 token + 跳登录页」，
 * 而当前就在登录页 → 死循环。
 */

const TOKEN_KEY = 'admin_token'

/**
 * token 存 sessionStorage 而不是 localStorage。
 *
 * 诚实边界（面试被追问要承认）：
 *  - sessionStorage **防不住 XSS**：能读 DOM 就能读它。
 *  - 真正的解法是 httpOnly Cookie（JS 读不到），代价是要防 CSRF + 后端改造。
 *  - 选它的实际收益有两块：① 关闭标签页即失效，缩短暴露窗口；
 *    ② 用 Authorization 头传 token **天然免 CSRF**（浏览器不会自动带上自定义头）。
 */
export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY)
}

/** 统一的接口错误：调用方按 status 区分处理（403 是权限不足，不是没登录） */
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

/**
 * 401 跳转的并发去重。
 * 后台一进首页会并发发好几个请求，token 过期时它们会**同时** 401——
 * 不去重的话会触发多次跳转（React Router 报 warning，且可能把 returnUrl 冲掉）。
 */
let redirectingToLogin = false

function handleUnauthorized(): void {
  clearToken()
  if (redirectingToLogin) return
  redirectingToLogin = true
  // 带上当前路径，登录后跳回来（否则 token 过期重登一律落到落地页，回不到原页）。
  // ⚠️ 用 replace 不用 push：不往历史里塞一条「已失效的页面」，否则用户点后退会再 401。
  const from = encodeURIComponent(window.location.pathname + window.location.search)
  window.location.replace(`/admin/login?from=${from}`)
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let resp: Response
  try {
    resp = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, '网络错误，请检查后端是否在运行')
  }

  if (resp.status === 401) {
    // 未登录 / token 失效：清 token 回登录页
    handleUnauthorized()
    throw new ApiError(401, '登录已失效，请重新登录')
  }
  if (resp.status === 403) {
    // ⚠️ 403 与 401 是两回事：已登录但权限不足。
    // 绝不能清 token 跳登录——那会让用户以为「重新登录就能解决」，而实际上不能。
    throw new ApiError(403, '权限不足（需要 admin 角色）')
  }
  if (resp.status === 429) {
    // 限流：token 没问题，只是打太快。同样不跳转，让调用方提示重试即可。
    throw new ApiError(429, '请求过于频繁，请稍后重试')
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const data = await resp.json()
      if (data && typeof data.detail === 'string') detail = data.detail
    } catch {
      /* 响应体不是 JSON，用默认文案 */
    }
    throw new ApiError(resp.status, detail)
  }

  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>('GET', path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('POST', path, body)
}

export function apiDelete<T>(path: string): Promise<T> {
  return request<T>('DELETE', path)
}
