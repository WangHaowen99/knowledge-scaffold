// 按路由拆包的页面：lazy 组件带 preload（预取用同一个 import()，浏览器只下载一次）。
// 代码块已经到了（预取过、启动时预加载过）就直接渲染，不经 Suspense：React 19 揭示挂起内容有 300ms 节流，
// 首屏会白等这一拍。
// 重新部署后，开着的旧页面里的代码块地址失效（带哈希的文件换了名）：加载失败时整页刷新一次，拿到新的 index.html
import { createElement, lazy, useState } from 'react';

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
  let loaded = null;
  const preload = () => load().then((m) => { loaded = m; return m; });
  const Lazy = lazy(() => preload().catch((e) => reloadOnce(e)));
  function Page(props) {
    // 每次挂载只定一次走哪条路：中途换组件类型会把页面卸掉重建、丢状态
    const [mod] = useState(() => loaded);
    return createElement(mod ? mod.default : Lazy, props);
  }
  Page.preload = preload;
  return Page;
}
