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
    },
  },
})
