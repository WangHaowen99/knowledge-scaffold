import { useCallback, useEffect, useRef, useState } from 'react';
import { errorText } from '../../api/errors.js';
import { RECORD_PAGE, RECORD_PAGE_MAX, appendRecords } from './flywheelModel.js';

// 记录列表分批读取：从最新一条开始，每批 RECORD_PAGE 条，more() 接着取下一批。
// fetchPage 变化（换页签、换筛选）时从第一批重新开始；signature 变化（轮询发现记录有变）时
// 按已加载的条数重新读取，列表不会跳回第一批。读取过程中开始了新的读取，旧结果直接丢弃。
export function useRecordPages(fetchPage, keyOf, signature) {
  const [view, setView] = useState({ items: [], total: 0, done: false, loading: true, error: '' });
  const generation = useRef(0);
  const loaded = useRef(0);
  const next = useRef(null);
  const busy = useRef(false);
  const source = useRef(null);

  const reload = useCallback(async (count) => {
    const id = ++generation.current;
    busy.current = true;
    setView((value) => ({ ...value, loading: true }));
    try {
      let items = [];
      let offset = 0;
      let batch;
      do {
        batch = await fetchPage(offset, Math.min(RECORD_PAGE_MAX, count - items.length));
        items = appendRecords(items, batch.items, keyOf);
        offset = batch.next_offset;
      } while (offset != null && items.length < count);
      if (id !== generation.current) return;
      loaded.current = items.length;
      next.current = offset;
      setView({ items, total: batch.total, done: offset == null, loading: false, error: '' });
    } catch (e) {
      if (id === generation.current) setView((value) => ({ ...value, loading: false, error: errorText(e) }));
    } finally {
      if (id === generation.current) busy.current = false;
    }
  }, [fetchPage, keyOf]);

  useEffect(() => {
    const fresh = source.current !== fetchPage;
    source.current = fetchPage;
    if (fresh) setView({ items: [], total: 0, done: false, loading: true, error: '' });
    reload(fresh ? RECORD_PAGE : Math.max(loaded.current, RECORD_PAGE));
  }, [fetchPage, signature, reload]);

  const more = useCallback(async () => {
    if (busy.current || next.current == null) return;
    const id = generation.current;
    busy.current = true;
    setView((value) => ({ ...value, loading: true }));
    try {
      const batch = await fetchPage(next.current, RECORD_PAGE);
      if (id !== generation.current) return;
      next.current = batch.next_offset;
      setView((value) => {
        const items = appendRecords(value.items, batch.items, keyOf);
        loaded.current = items.length;
        return { items, total: batch.total, done: batch.next_offset == null, loading: false, error: '' };
      });
    } catch (e) {
      if (id === generation.current) setView((value) => ({ ...value, loading: false, error: errorText(e) }));
    } finally {
      if (id === generation.current) busy.current = false;
    }
  }, [fetchPage, keyOf]);

  const retry = () => (view.items.length ? more() : reload(RECORD_PAGE));
  return { ...view, more, retry };
}

// 滚动到列表底部附近时取下一批；每次加载完重新观察，取完一批后仍在可视区内会接着取
export function useLoadMore(more, active, count) {
  const ref = useRef(null);
  useEffect(() => {
    const node = ref.current;
    if (!node || !active || typeof IntersectionObserver === 'undefined') return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) more();
    }, { root: node.closest('.modal-b'), rootMargin: '0px 0px 160px 0px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [more, active, count]);
  return ref;
}
