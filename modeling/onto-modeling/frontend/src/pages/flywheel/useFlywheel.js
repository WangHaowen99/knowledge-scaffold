import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { RECONCILE_EVERY, nextPoll } from './flywheelModel.js';

// 发布页打开期间持续同步飞轮：每次读取都由后端用当前登录推进自动步骤。
// 弹窗打开或有执行中的事项时密集轮询，空闲时低频；进入页面时先对账一次。
export function useFlywheel(projectId, open) {
  const { api } = useApp();
  const [state, setState] = useState(null);
  const [error, setError] = useState('');
  const latest = useRef(null);
  const loading = useRef(false);

  const apply = useCallback((value) => {
    latest.current = value;
    setState(value);
  }, []);

  const load = useCallback(async (reconcile = false) => {
    if (!projectId || loading.current) return;
    loading.current = true;
    try {
      apply(await api.flywheel(projectId, { reconcile }));
      setError('');
    } catch (e) {
      setError(errorText(e));
    } finally {
      loading.current = false;
    }
  }, [api, projectId, apply]);

  useEffect(() => {
    latest.current = null;
    setState(null);
    setError('');
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return undefined;
    let alive = true;
    let timer = null;
    let count = 0;
    const tick = async (reconcile) => {
      await load(reconcile);
      if (!alive || latest.current?.available === false) return; // 未启用统一账号时不再轮询
      count += 1;
      timer = setTimeout(() => tick(count % RECONCILE_EVERY === 0), nextPoll(latest.current, open));
    };
    tick(true);
    return () => { alive = false; clearTimeout(timer); };
  }, [projectId, open, load]);

  return { state, error, apply, refresh: () => load(true) };
}
