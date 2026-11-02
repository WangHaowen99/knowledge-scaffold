// 可观测（运维中心，设计稿 ops-obs）：公共标签过滤、指标卡（O-02）、按包 / 按节点归因（O-03）、链路瀑布（O-04）、
// 结构化日志与跟随（O-06、O-07）、告警规则（O-08～O-10）、导出器（O-11、O-12）。
// 地址参数：?trace=<trace_id> 直接打开那条链路（用户与审计跳过来），日志查询框同时填上 trace=<id>；
// ?section=rules 滚到告警规则卡（总览「全部规则」）。运维读写，管理员只读。
import { useCallback, useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Button, Card, Icon, Prog, Seg, Table } from '../../ui/index.jsx';
import { Spark } from './charts.jsx';
import { ExportersCard, ExportersModal } from './obsExporters.jsx';
import { LogsCard } from './obsLogs.jsx';
import {
  OBS_RANGES, addLabel, attributionRow, labelName, labelsQuery, metricView, parseLabelInput,
} from './obsModel.js';
import { RulesCard } from './obsRules.jsx';
import { TraceCard } from './obsTrace.jsx';
import { OpsShell } from './parts.jsx';
import '../../styles/ops-obs.css';

function LabelFilter({ tags, onChange }) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState('');
  const [error, setError] = useState(null);
  const commit = () => {
    if (!text.trim()) { setAdding(false); setError(null); return; }
    const r = parseLabelInput(text);
    if (r.error) { setError(r.error); return; }
    onChange(addLabel(tags, { key: r.key, value: r.value }));
    setText('');
    setError(null);
    setAdding(false);
  };
  return (
    <div className="card pad">
      <div className="row mb8">
        <span className="b"><Icon name="filter" size={14} /> 公共标签过滤</span>
        <span className="muted sm">所有图表和日志按这组标签联动</span>
        <span className="sp" />
        <Button size="xs" kind="ghost" disabled={!tags.length} onClick={() => onChange([])}>清空</Button>
      </div>
      <div className="op-tags">
        {tags.map((t) => (
          <span key={t.key} className="op-tg rm" title="点一下移除" onClick={() => onChange(tags.filter((x) => x.key !== t.key))}>
            <span>{labelName(t.key)}</span><b>{t.value}</b>
          </span>
        ))}
        {adding ? (
          <span className={`op-tg op-tg-in${error ? ' bad' : ''}`} title={error || '回车添加，Esc 取消'}>
            <span>+</span>
            <input
              autoFocus value={text} placeholder="pack=core@1.4.2" onChange={(e) => { setText(e.target.value); setError(null); }}
              onKeyDown={(e) => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') { setAdding(false); setText(''); setError(null); } }}
              onBlur={() => { if (!text.trim()) setAdding(false); }}
            />
          </span>
        ) : (
          <span className="op-tg" style={{ borderStyle: 'dashed', cursor: 'pointer' }} onClick={() => setAdding(true)}>
            <span>+</span><b style={{ fontFamily: 'var(--font)' }}>添加标签</b>
          </span>
        )}
        {error ? <span className="xs" style={{ color: 'var(--red)' }}>{error}</span> : null}
      </div>
    </div>
  );
}

function MetricCards({ range, labels }) {
  const { api } = useApp();
  const [cards, setCards] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    api.ops.metrics({ range, labels })
      .then((r) => { if (alive) { setCards(r.cards || []); setError(null); } })
      .catch((e) => alive && setError(errorText(e)));
    return () => { alive = false; };
  }, [api, range, labels]);
  if (error) return <div className="card pad mt16 muted sm">指标没取到：{error}</div>;
  return (
    <div className="op-k5 mt16">
      {(cards || []).map((c) => {
        const v = metricView(c);
        return (
          <div className="op-mc" key={v.id}>
            <div className="lb"><Icon name={v.icon} size={12} />{v.label}</div>
            <div className="v" style={v.tone ? { color: v.tone } : undefined}>{v.big}{v.small ? <small>{v.small}</small> : null}</div>
            <Spark points={v.series} w={180} h={30} color={v.color} fill={v.fill} />
            <div className="xs muted mt4">{v.note || ' '}</div>
          </div>
        );
      })}
    </div>
  );
}

function AttributionCard({ range, labels }) {
  const { api } = useApp();
  const [by, setBy] = useState('pack');
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    api.ops.attribution({ by, range, labels })
      .then((r) => { if (alive) { setItems(r.items || []); setError(null); } })
      .catch((e) => alive && setError(errorText(e)));
    return () => { alive = false; };
  }, [api, by, range, labels]);
  const node = by === 'node';
  const rows = (items || []).map(attributionRow);
  let body;
  if (error) body = <div className="muted sm">归因没取到：{error}</div>;
  else if (items && !items.length) body = <div className="muted sm">这段时间没有带包、节点标签的样本（插件运行时接入后才有数据）。</div>;
  else {
    body = (
      <Table
        compact
        cols={[node ? '节点' : '包', node ? '来自' : '版本', '调用', '失败率', '平均耗时', '守卫拒绝', '重跑', '首次接受率']}
        rows={rows.map((r) => [
          <b key="n" className={node ? 'mono' : undefined} style={node ? { fontSize: 12 } : undefined}>{r.name}</b>,
          <span key="v" className="tag">{r.version}</span>,
          <span key="c" className="num">{r.calls}</span>,
          r.failHigh ? <span key="f" className="chip red sm">{r.fail}</span> : <span key="f" className="num">{r.fail}</span>,
          <span key="d" className="num">{r.duration}</span>,
          <span key="g" className="num">{r.guards}</span>,
          <span key="r" className="num">{r.reruns}</span>,
          r.accept == null ? <span key="a" className="faint">—</span>
            : <div key="a" className="row"><Prog p={r.accept} w="54px" thin /><span className="num xs">{r.accept}%</span></div>,
        ])}
      />
    );
  }
  return (
    <Card
      className="mt16" title="按包 / 按节点归因" icon="pie" sub="调用、失败、耗时、守卫拒绝、重跑、首次被接受的比例"
      extra={<Seg items={[['pack', '按包'], ['node', '按节点']]} value={by} onChange={setBy} />}
    >
      {body}
    </Card>
  );
}

function Obs({ route, perm }) {
  const { api } = useApp();
  const traceId = route.query?.trace || null;
  const [range, setRange] = useState('24h');
  const [tags, setTags] = useState([]);
  const labels = labelsQuery(tags);
  const [exp, setExp] = useState({ items: null, error: null });
  const [expModal, setExpModal] = useState(null); // {change} 打开弹窗

  const loadExporters = useCallback(() => api.ops.exporters()
    .then((r) => setExp({ items: r.items || [], error: null }))
    .catch((e) => setExp({ items: null, error: errorText(e) })), [api]);
  useEffect(() => { loadExporters(); }, [loadExporters]);

  const openTrace = (id) => navigate('/ops-obs', { trace: id });
  const openExporters = (change = null) => { if (exp.items) setExpModal({ change }); };

  return (
    <>
      <PageHead
        eyebrow="运维中心"
        title="可观测"
        desc="统一信号：结构化日志、指标、链路、事件。每条信号都带公共标签，可以按包、按节点归因。"
        actions={(
          <>
            <Seg items={OBS_RANGES} value={range} onChange={setRange} />
            <Button icon="sliders" disabled={!exp.items} onClick={() => openExporters()}>导出器设置</Button>
          </>
        )}
      />
      <LabelFilter tags={tags} onChange={setTags} />
      <MetricCards range={range} labels={labels} />
      <AttributionCard range={range} labels={labels} />
      <TraceCard traceId={traceId} range={range} labels={labels} scrollOnLoad={!!traceId} onTag={(t) => setTags((x) => addLabel(x, t))} />
      <LogsCard range={range} labels={labels} initialQuery={traceId ? `trace=${traceId}` : ''} onOpenTrace={openTrace} />
      <div className="op-g21 mt16">
        <RulesCard perm={perm} focus={route.query?.section === 'rules'} />
        <ExportersCard data={exp.items} error={exp.error} perm={perm} onOpen={openExporters} />
      </div>
      {expModal && exp.items ? (
        <ExportersModal
          items={exp.items} change={expModal.change} perm={perm}
          onClose={() => setExpModal(null)}
          onSaved={(items) => { setExp({ items, error: null }); setExpModal(null); }}
        />
      ) : null}
    </>
  );
}

export default function Page({ route }) {
  return (
    <OpsShell active="ops-obs">
      {(perm) => <Obs route={route} perm={perm} />}
    </OpsShell>
  );
}
