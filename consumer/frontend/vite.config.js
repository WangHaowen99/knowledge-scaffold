import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const DEFAULT_API_PROXY_TARGET = 'http://127.0.0.1:5055';

const createProxyConfig = (target, options = {}) => ({
  target,
  changeOrigin: true,
  secure: false,
  ws: true,
  ...options,
});

// https://vite.dev/config/
export default defineConfig(() => {
  return {
    plugins: [react()],
    server: {
      port: 6600,
      host: true,
      proxy: {
        '/api': createProxyConfig(DEFAULT_API_PROXY_TARGET),
        '/agentic': createProxyConfig(DEFAULT_API_PROXY_TARGET),
        // Preserve the browser-facing authority for backend origin checks.
        '/auth': createProxyConfig(DEFAULT_API_PROXY_TARGET, { xfwd: true }),
        '/health': createProxyConfig(DEFAULT_API_PROXY_TARGET),
      },
    },
  };
});
