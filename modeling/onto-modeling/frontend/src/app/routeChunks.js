// 按路由拆包的页面：lazy 组件带 preload（预取用同一个 import()，浏览器只下载一次）。
// 重新部署后，开着的旧页面里的代码块地址失效（带哈希的文件换了名）：加载失败时整页刷新一次，拿到新的 index.html
import { lazy } from 'react';

const RELOAD_KEY = 'onto.chunk_reload_at';
const RELOAD_GAP = 30000;

/** 刷新一次并一直挂起（刷新前页面保持原样）；30 秒内刷新过还失败就抛出，交给错误边界 */
export function reloadOnce(err, { storage = globalThis.sessionStorage, now = Date.now(), reload = () => window.location.reload() } = {}) {
  let last = 0;
  try { last = Number(storage?.getItem(RELOAD_KEY)) || 0; } catch { /* 存储不可用时按没刷过 */ }
  if (now - last < RELOAD_GAP) throw err;
  try { storage?.setItem(RELOAD_KEY, String(now)); } catch { /* 存储不可用时照样刷新 */ }
  reload();
  return new Promise(() => {});
}

export function routePage(load) {
  const Page = lazy(() => load().catch((e) => reloadOnce(e)));
  Page.preload = load;
  return Page;
}
