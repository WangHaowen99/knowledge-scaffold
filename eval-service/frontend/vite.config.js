import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({ base: '/evaluation/', plugins: [react()], server: {
  proxy: { '/evaluation/api': process.env.EVAL_DEV_API_URL || 'http://127.0.0.1:8090' },
} });
