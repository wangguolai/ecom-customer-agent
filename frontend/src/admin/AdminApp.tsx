import { Navigate, Route, Routes } from 'react-router-dom'
import AdminLayout from './AdminLayout'
import LoginPage from './LoginPage'
import RequireAuth from './RequireAuth'
import RefundsPage from './pages/RefundsPage'
import OrdersPage from './pages/OrdersPage'
import UsersPage from './pages/UsersPage'
import FeedbackPage from './pages/FeedbackPage'
import MetricsPage from './pages/MetricsPage'

/**
 * 后台路由表。
 *
 * 路径在这里是**相对**的（`refunds` 而非 `/admin/refunds`）——
 * 因为 main.tsx 已经用 `<Route path="/admin/*">` 把这一整棵子树挂在了 /admin 下。
 *
 * 路由结构：
 *   /admin/login    登录页（在守卫之外——否则没登录时会被守卫拦到登录页，再被拦……死循环）
 *   /admin/*        其余全部需要登录
 *     （index）      → 重定向到 refunds（唯一带写操作的页，演示价值最高）
 *     *              未知路径 → 前端自己的 404（服务端 SPA fallback 会返回 index.html）
 */
export default function AdminApp() {
  return (
    <Routes>
      <Route path="login" element={<LoginPage />} />

      <Route
        element={
          <RequireAuth>
            <AdminLayout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="refunds" replace />} />
        <Route path="refunds" element={<RefundsPage />} />
        <Route path="orders" element={<OrdersPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="users/:uid" element={<UsersPage />} />
        <Route path="feedback" element={<FeedbackPage />} />
        {/* 详情用 trace_id 作参数：反馈的主键就是它（feedback_id == trace_id），
            且「看某条反馈」永远等价于「看那一轮发生了什么」 */}
        <Route path="feedback/:traceId" element={<FeedbackPage />} />
        <Route path="metrics" element={<MetricsPage />} />
        {/* 前端自己的 404：服务端对 /admin/* 一律返回 index.html（SPA fallback），
            真正区分「页面不存在」只能靠前端路由表 */}
        <Route path="*" element={<div className="admin-empty">页面不存在</div>} />
      </Route>
    </Routes>
  )
}
