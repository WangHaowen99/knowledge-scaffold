import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      '/evaluation': { target: process.env.EVAL_DEV_API_URL || 'http://127.0.0.1:8090', changeOrigin: true },
      '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true },
      // drawio 编辑器 webapp 由后端静态挂载（vendor/drawio/webapp）
      '/drawio': { target: 'http://127.0.0.1:8100', changeOrigin: true },
    },
  },
});
