/**
 * Vite 配置。
 *
 * ★ 这里最值得解释的是 dev server 的 proxy —— 它同时解决了两个问题：
 *
 *   问题：开发时前端跑在 5173、API 跑在 8005，**不同源**。
 *         浏览器会把 fetch 当成跨域请求，需要后端配 CORS。
 *
 *   解法：不直接请求 8005，而是请求**自己的源**（5173）上的 /api/...，
 *         由 Vite 的开发服务器在**服务端**转发到 8005。
 *         对浏览器来说全程同源，CORS 完全不出现。
 *
 *   为什么这比"后端开 CORS"好：
 *     ① 生产环境是后端托管静态文件，本来就是同源，
 *        所以开发和生产**都不需要 CORS**——少一套只在某个环境
 *        才生效的配置，也就少一类"本地好好的、部署就跨域"的问题。
 *     ② 后端不用为了前端开放 allow_origins，
 *        减少一个将来必须记得收紧的安全面。
 *
 *   注意：这个 proxy **只在 dev server 里生效**。生产环境靠
 *        FastAPI 的 StaticFiles 挂载，不走 proxy。
 */
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],

  server: {
    // 监听所有网卡，这样从 Windows 也能访问（WSL2 的 localhost 转发
    // 只覆盖 127.0.0.1，绑 0.0.0.0 更省事）。
    host: '0.0.0.0',
    port: 5173,
    // 端口被占用时直接失败，不要自动换端口——
    // 换了端口而你没注意，就会对着一个空白的旧页面调半天。
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8005',
        changeOrigin: true,
      },
    },
  },

  build: {
    outDir: 'dist',
    // 不生成 sourcemap：这是局域网内的内部工具，
    // 没必要把源码结构暴露给浏览器，产物也小一截。
    sourcemap: false,
    // ECharts 按需引入后单个 chunk 仍会接近 500KB。
    // 默认警告阈值是 500KB，调高一点免得每次构建都刷一条
    // "有些 chunk 很大"的提示——那条提示在这里是预期的，不是问题。
    chunkSizeWarningLimit: 800,
  },
})
