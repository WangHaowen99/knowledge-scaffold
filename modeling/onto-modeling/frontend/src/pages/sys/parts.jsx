// 系统设置各分区共用：取数 hook、读不出来时的提示、页头
import { useCallback, useEffect, useRef, useState } from 'react';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Button, Note } from '../../ui/index.jsx';

export const EYEBROW = '系统设置 · 管理员';

export function SysHead({ title, desc, actions }) {
  return <PageHead eyebrow={EYEBROW} title={title} desc={desc} actions={actions} />;
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

/** 接口读不出来：404 说明服务端还没有这组接口（以后阶段），其余给错误原因和重试 */
export function LoadNote({ error, what, code, onRetry }) {
  if (!error) return null;
  if (error.status === 404) {
    return <Note tone="gray" icon="info">这一版服务端还没有{what}接口{code ? `（${code}）` : ''}，这里暂时只能看说明，保存不了。</Note>;
  }
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
