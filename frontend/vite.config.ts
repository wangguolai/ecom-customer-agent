import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 构建产物输出到 frontend/dist，由后端 FastAPI 托管（src/backend/main.py 的 `/` 路由）。
// 开发模式（npm run dev）把写接口代理到本地后端，避免跨域——生产是同源，不需要代理。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/chat': 'http://127.0.0.1:8000',
      '/product_images': 'http://127.0.0.1:8000',
      // 后台 API 刻意放在 /api 命名空间，与前端路由 /admin/* 分开：
      // 二者若同前缀（如都在 /admin/refunds），Vite 无法区分「这是前端路由」还是「这是 API 调用」，
      // 代理会把浏览器导航也转发给后端。分开之后规则无歧义。
      '/api': 'http://127.0.0.1:8000',
      '/auth': 'http://127.0.0.1:8000',
      '/memory': 'http://127.0.0.1:8000',
      // 审批/执行是既有接口，路径不在 /api 命名空间下（历史原因），单独代理
      '/refund': 'http://127.0.0.1:8000',
    },
  },
})
