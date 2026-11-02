// 可观测 · 结构化日志（O-06）与跟随（O-07 日志流，事件 {type: 'log', seq, data}）。
// 级别分段、查询框（key=value OR key=value）、公共标签、时间范围都作为过滤条件；跟随时条件变了就重开流。
// 日志行里的 trace= 悬停可点，在链路卡里打开那一条。
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { Button, Card, Inp, Seg } from '../../ui/index.jsx';
import { LOG_LEVELS, appendLog, levelCls, levelParam, logFields, logTime, sortLogs } from './obsModel.js';

const LEVEL_TEXT = { I: 'INFO', W: 'WARN', E: 'ERROR' };

function LogLine({ e, onOpenTrace }) {
  const cls = levelCls(e.level);
  return (
    <div>
      <span className="t">{logTime(e.ts)}</span>{'  '}
      <span className={`L ${cls}`}>{LEVEL_TEXT[cls] || String(e.level || '').toUpperCase()}</span>{' '}
      <b>{e.event}</b>{'  '}
      <span className="g">
        {logFields(e.fields).map((f, i) => (
          <span key={i}>
            {i ? ' ' : ''}{f.k}=
            {f.trace && f.v ? <span className="tr" title="在链路卡里打开" onClick={() => onOpenTrace(f.v)}>{f.v}</span> : f.v}
          </span>
        ))}
      </span>
    </div>
  );
}

export function LogsCard({ range, labels, initialQuery, onOpenTrace }) {
  const { api, services } = useApp();
  const [level, setLevel] = useState('all');
  const [input, setInput] = useState(initialQuery || '');
  const [q, setQ] = useState(initialQuery || '');
  const [lines, setLines] = useState([]);
  const [error, setError] = useState(null);
  const [follow, setFollow] = useState(false);
  const box = useRef(null);

  useEffect(() => {
    let alive = true;
    api.ops.logs({ level: levelParam(level), q, labels, range })
      .then((r) => { if (alive) { setLines(sortLogs(r.items)); setError(null); } })
      .catch((e) => alive && setError(errorText(e)));
    return () => { alive = false; };
  }, [api, level, q, labels, range]);

  useEffect(() => {
    if (!follow) return undefined;
    const query = { level: levelParam(level), q, labels };
    return services.openStream({
      kind: 'user',
      url: (resume) => api.ops.logStreamUrl({ ...query, ...resume }),
      onEvent: (env) => { if (env.type === 'log' && env.data) setLines((l) => appendLog(l, env.data)); },
    });
  }, [follow, api, services, level, q, labels]);

  // 跟随时停在最下面
  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, follow]);

  const apply = () => { if (input.trim() !== q) setQ(input.trim()); };
  return (
    <Card className="mt16" title="结构化日志" icon="terminal" extra={<Seg items={LOG_LEVELS} value={level} onChange={setLevel} />}>
      <div className="row mb8">
        <Inp
          icon="search" mono className="grow" value={input} placeholder="trace=4f9c2e1d OR pack=industry-retail@1.0.3"
          title="回车筛选；多个条件用 OR 连接，key=value 比字段，其余在整行里找"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') apply(); if (e.key === 'Escape') { setInput(''); setQ(''); } }}
          onBlur={apply}
        />
        <Button size="sm" icon={follow ? 'pause' : 'play'} kind={follow ? 'primary' : undefined} onClick={() => setFollow((v) => !v)}>
          {follow ? '停止跟随' : '跟随'}
        </Button>
      </div>
      <div className="op-log" ref={box}>
        {lines.map((e, i) => <LogLine key={`${e.ts}-${i}`} e={e} onOpenTrace={onOpenTrace} />)}
        {!lines.length ? <div className="t">{error ? `日志没取到：${error}` : follow ? '等待新的日志…' : '没有符合条件的日志'}</div> : null}
      </div>
    </Card>
  );
}
