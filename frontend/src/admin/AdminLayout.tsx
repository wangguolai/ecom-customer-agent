import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { clearToken } from './api'

const NAV = [
  { to: '/admin/refunds', label: '退款工单' },
  { to: '/admin/orders', label: '订单 / 物流' },
  { to: '/admin/users', label: '用户画像' },
  // 排在「运行指标」之前：反馈是**可行动的数据**（点进去能看 trace 定位问题），
  // 指标是**纯观测**。需要动手的排在只能看的上面。
  { to: '/admin/feedback', label: '反馈评分' },
  { to: '/admin/metrics', label: '运行指标' },
]

export default function AdminLayout() {
  const navigate = useNavigate()

  function logout() {
    // ⚠️ 这只是**前端删 token**。后端是无状态 JWT，token 在 TTL（1 小时）内**仍然有效**，
    // 拿到它的人照样能调接口（curl 可复现）。这是已知缺口（architecture.md §7）。
    // 真正的登出需要服务端 token 黑名单 / 短 TTL + refresh，属二期。
    clearToken()
    navigate('/admin/login', { replace: true })
  }

  return (
    <div className="admin-shell">
      <aside className="admin-side">
        <div className="admin-brand">客服 Agent 后台</div>
        <nav>
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? 'admin-nav-active' : undefined)}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <a className="admin-side-back" href="/">← 客服对话</a>
      </aside>

      <div className="admin-main">
        <header className="admin-top">
          <span className="admin-top-title">管理后台</span>
          <button className="admin-logout" onClick={logout}>登出</button>
        </header>
        <main className="admin-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
