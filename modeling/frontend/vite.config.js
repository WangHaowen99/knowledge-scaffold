import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const backend = 'http://127.0.0.1:5100';

export default defineConfig({
  plugins: [react()],
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
