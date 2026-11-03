// 链路检索（契约 O-05）：按 trace id、项目、用户、时间、状态找一条链路，点一行在链路卡里打开。
// 设计稿没画入口，暂不挂载（见 gaps.md F4）。
// 挂载方法：在 OpsObsPage.jsx 的 Obs 里、<TraceCard … /> 上一行加
//   <TraceSearch range={range} labels={labels} onOpen={openTrace} />
// （import TraceSearch from './TraceSearch.jsx'；openTrace 已有，带 ?trace= 在链路卡里打开那一条）。
// 纯逻辑在 obsModel.js（parseTraceQuery、traceSearchQuery、filterTraces、traceRowView），mock 在 routes-ops-obs.js。
import { useCallback, useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { Button, Card, Chip, Inp, Note, Seg, Select } from '../../ui/index.jsx';
import { OBS_RANGES, TRACE_STATUS, filterTraces, parseTraceQuery, traceRowView, traceSearchQuery } from './obsModel.js';
import '../../styles/ops-obs.css';

const RANGE_OPTIONS = OBS_RANGES.map(([value, label]) => ({ value, label }));

export default function TraceSearch({ range: initialRange = '24h', labels = '', onOpen }) {
  const { api } = useApp();
  const [text, setText] = useState('');
  const [applied, setApplied] = useState('');
  const [status, setStatus] = useState('all');
  const [range, setRange] = useState(initialRange);
  const [res, setRes] = useState({ items: [], next: null, loading: true });
  const [error, setError] = useState(null);
  const parseError = parseTraceQuery(text).error;

  const run = useCallback(async (cursor = null) => {
    const { query, traceId } = traceSearchQuery({ text: applied, status, range, labels });
    setError(null);
    try {
      const r = await api.ops.traces({ ...query, cursor });
      const items = filterTraces(r.items, traceId);
      setRes((s) => ({ items: cursor ? [...s.items, ...items] : items, next: r.next_cursor || null, loading: false }));
    } catch (e) {
      setError(errorText(e));
      setRes({ items: [], next: null, loading: false });
    }
  }, [api, applied, status, range, labels]);
  useEffect(() => { run(); }, [run]);

  const submit = () => { if (!parseError) setApplied(text.trim()); };
  const now = Date.now();
  return (
    <Card
      className="mt16" title="链路检索" icon="search" sub="按 trace id、项目、用户、时间、状态找一条链路，点一行在链路卡里打开"
      extra={<Seg items={TRACE_STATUS} value={status} onChange={setStatus} />}
    >
      <div className="row mb8">
        <Inp
          icon="search" mono className="grow" value={text} error={!!parseError} title={parseError || '回车检索'}
          placeholder="trace id，或 project=retail-restock user=zhangsan"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
        />
        <Select value={range} options={RANGE_OPTIONS} onChange={setRange} w={120} />
        <Button size="sm" icon="search" disabled={!!parseError} onClick={submit}>检索</Button>
      </div>
      {parseError ? <div className="xs mb8" style={{ color: 'var(--red)' }}>{parseError}</div> : null}
      {error ? <Note tone="red">链路没取到：{error}</Note> : (
        <div className="tbl-wrap">
          <table className="tbl compact">
            <thead><tr><th>trace id</th><th>开始</th><th>入口</th><th>项目</th><th>用户</th><th>耗时</th><th>状态</th></tr></thead>
            <tbody>
              {res.items.map((t) => {
                const v = traceRowView(t, now);
                return (
                  <tr key={v.id} className="op-ts-row" onClick={() => onOpen?.(v.id)} title="在链路卡里打开">
                    <td><b className="mono" style={{ fontSize: 12 }}>{v.id}</b></td>
                    <td className="muted">{v.at}</td>
                    <td>{v.root}</td>
                    <td className="mono xs">{v.project}</td>
                    <td className="mono xs">{v.user}</td>
                    <td className="num">{v.total}</td>
                    <td><Chip tone={v.status[1]} sm>{v.status[0]}</Chip></td>
                  </tr>
                );
              })}
              {!res.items.length && !res.loading ? <tr><td colSpan={7} className="muted">没有符合条件的链路</td></tr> : null}
            </tbody>
          </table>
        </div>
      )}
      {res.next ? <div className="mt8"><a className="link sm" onClick={() => run(res.next)}>加载更多</a></div> : null}
    </Card>
  );
}
