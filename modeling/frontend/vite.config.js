import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

const backend = 'http://127.0.0.1:5100';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      // 双入口：现有应用 index.html 与新工作台 workbench.html 并存（双跑期）
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        workbench: resolve(import.meta.dirname, 'workbench.html'),
      },
      output: {
        // react 单独成块，长期缓存；markdown 链路按需加载不进主包
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'react';
          if (/[\\/]node_modules[\\/](react-markdown|remark|rehype|mdast|micromark|unist|vfile|hast|property-information|space-separated|comma-separated|character-entities|decode-named|devlop|trim-lines|estree|bail|is-plain-obj|trough|unified|zwitch|longest-streak|ccount|escape-string|markdown-table)/.test(id)) return 'markdown';
          return undefined;
        },
      },
    },
    chunkSizeWarningLimit: 700,
    reportCompressedSize: true,
  },
  server: {
    port: 6700,
    host: true,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/auth': { target: backend, changeOrigin: true },
      '/health': { target: backend, changeOrigin: true },
    },
  },
});
