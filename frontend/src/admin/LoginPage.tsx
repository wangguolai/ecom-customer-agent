import { useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { setToken } from './api'

/**
 * 后台登录页。
 *
 * ⚠️ 这里**刻意用裸 fetch 而不是 api.ts 的封装**：
 * 封装里 401 会触发「清 token + 跳登录页」，而登录失败恰好返回 401 →
 * 如果在登录页用封装，一次密码输错就会触发自我跳转（死循环 + 状态被清）。
 * 登录接口的 401 是「凭据错」，其他接口的 401 是「会话失效」——语义不同，处理必须分开。
 */
export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  // 登录后的跳转目标，两个来源：
  //   ?from=...       —— token 过期被 api.ts 踢回来时带的原路径
  //   location.state  —— 未登录首次进入时 RequireAuth 带的原路径
  //
  // ⚠️ `from` 来自 URL 参数 = **用户可控**，必须校验是站内后台路径。
  // 不校验就是**开放重定向**：`/admin/login?from=https://evil.com` 登录后被送去钓鱼站，
  // 而钓鱼站长得和登录页一模一样——用户刚输过密码，正是最容易上钩的时刻。
  const rawFrom = new URLSearchParams(location.search).get('from')
    ?? (location.state as { from?: string } | null)?.from
    ?? ''
  const from = rawFrom.startsWith('/admin/') ? rawFrom : '/admin/refunds'

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const resp = await fetch('/auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      if (resp.status === 429) {
        // 登录接口有独立限流桶（5 次/60s）——开发和演示时很容易把自己锁住，
        // 所以这里给出明确的文案，而不是笼统的「失败」。
        setError('登录尝试过于频繁（限 5 次/60 秒），请稍后再试')
        return
      }
      if (!resp.ok) {
        // 后端对「用户不存在」和「密码错」返回同一个话术，前端原样转述即可（防用户枚举）
        setError('用户名或密码错误')
        return
      }
      const data = (await resp.json()) as { access_token?: string }
      // ⚠️ 字段名是 access_token，不是 token —— 取错了会拿到 undefined 且不报错
      if (!data.access_token) {
        setError('登录响应缺少 access_token 字段')
        return
      }
      setToken(data.access_token)
      navigate(from, { replace: true })
    } catch {
      setError('网络错误，请确认后端已启动')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin-login-wrap">
      <form className="admin-login-card" onSubmit={onSubmit}>
        <h1>客服 Agent 管理后台</h1>
        <p className="admin-login-hint">需要 admin 账号（见 .env 的 ADMIN_USER / ADMIN_PASSWORD）</p>

        <label>
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>

        {error && <div className="admin-error">{error}</div>}

        <button type="submit" disabled={busy || !username || !password}>
          {busy ? '登录中…' : '登录'}
        </button>

        <a className="admin-login-back" href="/">← 返回客服对话</a>
      </form>
    </div>
  )
}
