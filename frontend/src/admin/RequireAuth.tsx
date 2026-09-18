import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { getToken } from './api'

/**
 * 路由守卫：无 token 跳登录页。
 *
 * ⚠️ **这不是安全边界，只是体验层**。
 * 前端守卫可以被完全绕过——改 JS、删掉这个组件、或者直接 curl 接口。
 * 真正拦住越权的是后端的 `Depends(auth.require_role("admin"))`。
 *
 * 验证方式（必须手动验一次）：
 *   curl -i http://127.0.0.1:8000/api/admin/orders      # 期望 401
 * 如果这个返回 200，说明守卫形同虚设、后端有洞——前端跳转再漂亮都没用。
 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation()
  if (!getToken()) {
    // 记住来路，登录后可以跳回去（state 在 SPA 内传递，不经过服务端）
    return <Navigate to="/admin/login" replace state={{ from: location.pathname }} />
  }
  return <>{children}</>
}
