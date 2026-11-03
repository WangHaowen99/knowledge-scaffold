// 首屏之后预取各路由的代码块（F2：第一次切换页面白屏几秒）：一次只取一个，浏览器空闲时才取下一个，不和当前页面抢网络；
// 省流量模式不取。loaders 是各页面 lazy 用的同一个 import()，取过之后切过去不用再下载
const idleOf = (fn) => {
  if (typeof requestIdleCallback === 'function') requestIdleCallback(fn, { timeout: 3000 });
  else setTimeout(fn, 200);
};
const saveDataOn = () => (typeof navigator !== 'undefined' && !!navigator.connection?.saveData);

/** 返回取消函数（换用户、卸载时调） */
export function prefetchWhenIdle(loaders, { delay = 1500, idle = idleOf, saveData = saveDataOn() } = {}) {
  if (saveData || !loaders.length) return () => {};
  let stopped = false;
  let i = 0;
  const next = () => {
    if (stopped || i >= loaders.length) return;
    idle(() => {
      if (stopped) return;
      const load = loaders[i];
      i += 1;
      new Promise((resolve) => { resolve(load()); }).catch(() => { /* 取不到就算了，真切过去时再取 */ }).then(next);
    });
  };
  const timer = setTimeout(next, delay);
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}
