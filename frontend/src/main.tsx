import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import App from './App'
import AdminApp from './admin/AdminApp'
import './styles.css'
import './admin/admin.css'

/**
 * 入口路由分流：
 *   /          客服对话（原有界面）
 *   /admin/*   管理后台
 *
 * ⚠️ 服务端对 `/admin/*` 一律返回 index.html（SPA fallback，见 backend/main.py）——
 * 服务端不知道前端有哪些路由，真正的匹配发生在这里。
 * 所以「刷新后台页面 404」不是后端 bug，而是 SPA 的必然：必须靠 fallback 兜住。
 *
 * ⚠️ 后端的接口前缀是 `/api/admin/*`，和前端路由 `/admin/*` **刻意分开**：
 * 二者同前缀的话，Vite 代理无法区分「浏览器要页面」和「前端要数据」。
 */
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<App />} />
        <Route path="/admin/*" element={<AdminApp />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
