// 首屏提前取数：启动时按地址里的项目把首屏要用的请求先发出去，和 C-01、A-07、页面代码块下载并行。
// 页面挂载时按同一个键取走（只用一次，10 秒内有效）；没有提前发过或已过期就照常自己请求。
const pending = new Map();
const TTL_MS = 10000;

export function prime(key, start, now = Date.now()) {
  if (pending.has(key)) return;
  const promise = start();
  promise.catch(() => {});
  pending.set(key, { promise, at: now });
}

export function take(key, start, now = Date.now()) {
  const hit = pending.get(key);
  pending.delete(key);
  return hit && now - hit.at < TTL_MS ? hit.promise : start();
}

export function primeProject(api, pid) {
  prime(`project:${pid}`, () => api.project(pid));
  prime(`extensions:${pid}`, () => api.extensions(pid));
  prime(`inbox:${pid}`, () => api.inbox(pid, { limit: 1 }));
  prime(`flow:${pid}`, () => api.flow(pid));
  prime(`scope:${pid}`, () => api.scope(pid));
  prime(`conversations:${pid}`, () => api.conversations(pid));
}
