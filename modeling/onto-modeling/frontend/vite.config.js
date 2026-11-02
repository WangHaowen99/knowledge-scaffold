import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  // mock 只在开发 / 测试构建里生效：生产构建恒为 false，mock 模块被摇掉，不进产物
  const apiMock = mode !== 'production' && (env.VITE_API_MOCK || process.env.VITE_API_MOCK) === '1';
  return {
    plugins: [react()],
    define: { __API_MOCK__: JSON.stringify(apiMock) },
    server: {
      port: 5180,
      proxy: {
        '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true },
        // drawio 编辑器 webapp 由后端静态挂载（vendor/drawio/webapp）
        '/drawio': { target: 'http://127.0.0.1:8100', changeOrigin: true },
      },
    },
  };
});
