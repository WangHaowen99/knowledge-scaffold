// 可观测 · 链路卡（O-04 瀑布）：带 ?trace=<trace_id> 时打开那一条（用户与审计的审计、错误会这样跳过来），
// 没带时取 O-05 第一页里开始最晚的一条（带当前时间范围与公共标签）。链路里只有路径、key、ID，不含项目内容。
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { errorText } from '../../api/errors.js';
import { Card, Empty, Icon, Note } from '../../ui/index.jsx';
import { labelName, latestTrace, traceTags, traceTotalText, waterfallRows } from './obsModel.js';

// 滚到某张卡：上面的卡还在加载、会把它往下推，所以过一会儿再对齐一次（用户自己滚过就不再动）
export function settleScroll(getEl) {
  const body = document.querySelector('.pg-body');
  let touched = false;
  const stop = () => { touched = true; };
  body?.addEventListener('wheel', stop, { once: true, passive: true });
  [0, 300, 800].forEach((ms) => setTimeout(() => {
    if (!touched) getEl()?.scrollIntoView({ block: 'start', behavior: ms ? 'auto' : 'smooth' });
  }, ms));
  setTimeout(() => body?.removeEventListener('wheel', stop), 900);
}

export function TraceCard({ traceId, range, labels, onTag, scrollOnLoad }) {
  const { api } = useApp();
  const [state, setState] = useState({ trace: null, error: null, missing: null, loading: true });
  const ref = useRef(null);
  // 指定了链路时不随时间范围、标签变化重取
  const listKey = traceId ? '' : `${range}|${labels}`;

  useEffect(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true }));
    (async () => {
      try {
        let id = traceId;
        if (!id) {
          const list = await api.ops.traces({ range, labels, limit: 20 });
          id = latestTrace(list.items)?.trace_id;
          if (!id) { if (alive) setState({ trace: null, error: null, missing: null, loading: false }); return; }
        }
        const trace = await api.ops.trace(id);
        if (!alive) return;
        setState({ trace, error: null, missing: null, loading: false });
        if (scrollOnLoad) settleScroll(() => ref.current);
      } catch (e) {
        if (!alive) return;
        if (e?.code === 'trace_not_found') setState({ trace: null, error: null, missing: traceId, loading: false });
        else setState({ trace: null, error: errorText(e), missing: null, loading: false });
        if (scrollOnLoad) settleScroll(() => ref.current);
      }
    })();
    return () => { alive = false; };
  }, [api, traceId, listKey]);

  const { trace, error, missing, loading } = state;
  const rows = waterfallRows(trace);
  return (
    <div ref={ref} id="op-trace" style={{ scrollMarginTop: 16 }}>
      <Card
        className="mt16" title="链路" icon="activity"
        extra={<span className="mono xs muted">{traceTotalText(trace)}</span>}
      >
        {trace ? (
          <>
            <div className="op-tags mb12">
              {traceTags(trace.labels).map((t) => (
                <span key={t.key} className="op-tg rm-add" title="加入公共标签过滤" onClick={() => onTag?.(t)}>
                  <span>{labelName(t.key)}</span><b>{t.value}</b>
                </span>
              ))}
            </div>
            <div className="op-wf">
              {rows.map((r) => (
                <div className="r" key={r.id}>
                  <div className="nm"><Icon name={r.icon} size={14} /><span className="ell" title={r.name}>{r.name}</span></div>
                  <div className="bar"><i className={r.cls || undefined} style={{ left: `${r.left}%`, width: `${r.width}%` }} /></div>
                  <div className="ms">{r.ms}</div>
                </div>
              ))}
            </div>
          </>
        ) : null}
        {missing ? (
          <Note tone="amber">
            链路 <span className="mono">{missing}</span> 不存在或已过期
            <a className="link" style={{ marginLeft: 6 }} onClick={() => navigate('/ops-obs')}>看最近一条</a>
          </Note>
        ) : null}
        {error ? <div className="muted sm">链路没取到：{error}</div> : null}
        {!trace && !missing && !error && !loading ? (
          <Empty icon="activity" title="这段时间还没有链路" />
        ) : null}
      </Card>
    </div>
  );
}
