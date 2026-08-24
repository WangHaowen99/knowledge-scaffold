/* 极小状态容器：useSyncExternalStore + 不可变更新。
   不引状态库——整个工作台的共享状态就是一个对象，Redux/Zustand 的收益覆盖不了体积。 */
import { useSyncExternalStore, useCallback } from 'react';

export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  const get = () => state;
  const set = (patch) => {
    const next = typeof patch === 'function' ? patch(state) : patch;
    if (next === state) return;
    state = { ...state, ...next };
    subs.forEach((f) => f());
  };
  const subscribe = (f) => { subs.add(f); return () => subs.delete(f); };
  return { get, set, subscribe };
}

/** 选择器订阅：只有选出来的值变了才重渲染，避免一处改动刷新整个工作台。
 *  约束：selector 必须返回稳定引用或原始值（`s => s.tree` 可以，`s => ({...})` 会死循环）。
 *  需要组合多个字段时在组件里分别 useStore，不要在 selector 里现造对象。 */
export function useStore(store, selector = (s) => s) {
  const sel = useCallback(() => selector(store.get()), [store, selector]);
  return useSyncExternalStore(store.subscribe, sel, sel);
}
