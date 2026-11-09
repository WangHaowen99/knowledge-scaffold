import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Two plugin pages share this project: KB_APP=kb (index.html, kb-manager)
// and KB_APP=da (da.html, da-analyst). Swarm serves each page under
// /api/application-plugins/<id>/assets/, so asset URLs must be relative.
const app = process.env.KB_APP === 'da' ? 'da' : 'kb';

export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: app === 'da' ? '../plugin/da-analyst/frontend/dist' : '../plugin/kb-manager/frontend/dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: { input: resolve(__dirname, app === 'da' ? 'da.html' : 'index.html') },
  },
});
