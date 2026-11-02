import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

const SRC = fileURLToPath(new URL('./src', import.meta.url));
// 官方包自带的界面代码（ADR-0055）：assets/packs/<包>/ui/，随平台一起编译、按包懒加载
const PACKS = fileURLToPath(new URL('../assets/packs', import.meta.url));
const APP_ENTRY = `${SRC}/main.jsx`;

// 包目录不在前端工程里：包代码里的裸模块名（react、@xyflow/react……）按平台前端的依赖解析，
// 和 tests/pack-resolve-hooks.mjs（node 跑单测时）同一条规则
function packUi() {
  return {
    name: 'onto-pack-ui',
    enforce: 'pre',
    resolveId(source, importer, options) {
      if (!importer || !importer.startsWith(PACKS) || /^(\.|\/|@onto\/|@packs\/|\0|[a-z]+:)/.test(source)) return null;
      return this.resolve(source, APP_ENTRY, { ...options, skipSelf: true });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  // mock 只在开发 / 测试构建里生效：生产构建恒为 false，mock 模块被摇掉，不进产物
  const apiMock = mode !== 'production' && (env.VITE_API_MOCK || process.env.VITE_API_MOCK) === '1';
  return {
    plugins: [packUi(), react()],
    define: { __API_MOCK__: JSON.stringify(apiMock) },
    resolve: {
      // @onto/：包界面代码引用平台模块；@packs/：平台引用包界面代码
      alias: { '@onto': SRC, '@packs': PACKS },
    },
    server: {
      port: 5180,
      fs: { allow: [fileURLToPath(new URL('.', import.meta.url)), PACKS] },
      proxy: {
        '/api': { target: 'http://127.0.0.1:8100', changeOrigin: true },
        // drawio 编辑器 webapp 由后端静态挂载（vendor/drawio/webapp）
        '/drawio': { target: 'http://127.0.0.1:8100', changeOrigin: true },
      },
    },
  };
});
