// 系统设置各分区共用：取数 hook、读不出来时的提示、页头
import { useCallback, useEffect, useRef, useState } from 'react';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Button, Note } from '../../ui/index.jsx';

export function SysHead({ title, actions }) {
  return <PageHead title={title} actions={actions} />;
}

/** 分区打开时取一次数据；data 为 undefined 表示还在读，error 是最近一次失败 */
export function useLoad(loader) {
  const [state, setState] = useState({ data: undefined, error: null });
  const fn = useRef(loader);
  fn.current = loader;
  const reload = useCallback(async () => {
    try {
      const data = await fn.current();
      setState({ data, error: null });
      return data;
    } catch (e) {
      setState((s) => ({ data: s.data ?? null, error: e }));
      return null;
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);
  const setData = useCallback((v) => setState((s) => ({ ...s, data: typeof v === 'function' ? v(s.data) : v })), []);
  return { data: state.data, error: state.error, loading: state.data === undefined && !state.error, reload, setData };
}

/** 接口读不出来：给错误原因和重试（服务端没有的分区在左栏就不出，R17） */
export function LoadNote({ error, what, onRetry }) {
  if (!error) return null;
  return (
    <Note tone="red">
      {what}没有读出来：{errorText(error)}
      {onRetry ? <Button size="xs" kind="ghost" style={{ marginLeft: 8 }} onClick={onRetry}>重试</Button> : null}
    </Note>
  );
}

export const Loading = () => <div className="card pad muted sm">正在读取…</div>;

/** 字段下的错误说明 */
export const FieldErr = ({ children }) => (children ? <span className="sy-err">{children}</span> : null);
