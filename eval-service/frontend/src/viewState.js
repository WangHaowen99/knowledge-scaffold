import { useEffect, useRef, useState } from 'react';

export function readView(name, fallback) {
  const value = new URLSearchParams(globalThis.location?.hash.split('?')[1] || '').get(name);
  if (typeof fallback === 'number') return value != null && /^\d+$/.test(value) ? Math.min(100000, Number(value)) : fallback;
  return value == null ? fallback : value;
}
// replaceState keeps the current view in the history entry before navigation to another record.
export function useViewState(name, fallback) {
  const [value, update] = useState(() => readView(name, fallback));
  useEffect(() => {
    const restore = () => update(readView(name, fallback));
    window.addEventListener('hashchange', restore);
    window.addEventListener('popstate', restore);
    return () => { window.removeEventListener('hashchange', restore); window.removeEventListener('popstate', restore); };
  }, [name, fallback]);
  const latest = useRef(value);
  latest.current = value;
  const setValue = (next) => {
    const result = typeof next === 'function' ? next(latest.current) : next;
    latest.current = result;
    const [path, search = ''] = location.hash.split('?');
    const query = new URLSearchParams(search);
    if (result === fallback) query.delete(name); else query.set(name, String(result));
    history.replaceState(history.state, '', `${path || '#/'}${query.size ? `?${query}` : ''}`);
    update(result);
  };
  return [value, setValue];
}
