// Studio 图谱（设计稿 studio）：顶部工具条与统计（ST-01）、关系图（ST-02，画布复用原版 WebGL 图谱）、
// 左上图例、底部操作流程面板（ST-05）、右侧选中详情：属性 / 关系 / 证据（ST-04、ST-06、EV-26），锁标记（CS-06）
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import LegacyStudioGraphHost from '../../components/studio/LegacyStudioGraphHost.jsx';
import { ShellP } from '../../shell/Shell.jsx';
import { Button, Chip, Icon, Inp, Seg, St, Tabs, Toggle } from '../../ui/index.jsx';
import { inlineTokens } from '../workbench/md.js';
import '../../styles/studio.css';
import { KindDot, LockIcon, LockMark, PlugMark, askAssistant, copyText, gotoEntry, useLockCopy } from './parts.jsx';
import {
  CONFIDENCE_CHIP, KIND_META, backlinkView, evidenceShortLabel, kindDotStyle, kindLabel, relationText,
} from './studioModel.js';

const LEGEND_POP = '图例：● 对象　■ 操作　◆ 规则　● 指标。实线：关系声明（一级标题「关联对象」下）；虚线：正文里只是提及，不算关系';
const SUM_KINDS = [['object', 'objects'], ['operation', 'operations'], ['rule', 'rules'], ['metric', 'metrics']];
const STATE_KEY = { confirmed: 'confirmed', pending: 'pending', stale: 'stale', draft: 'draft' };

function useDebounced(value, ms) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function StatsBar({ stats, pid }) {
  if (!stats) return <div className="sd-sum" />;
  return (
    <div className="sd-sum">
      {SUM_KINDS.map(([k, f]) => (
        <span key={k}><span className="k" style={kindDotStyle(k, 8)} /><b>{stats[f]}</b>{kindLabel(k)}</span>
      ))}
      {stats.flowcharts != null ? <span><b>{stats.flowcharts}</b>流程图<PlugMark label="图示" /></span> : null}
      <span><b>{stats.relations}</b>关系</span>
      {stats.scenarios ? (
        <span style={{ cursor: 'pointer' }} onClick={() => navigate(pagePath('scenarios', pid))}>
          <b>{stats.scenarios.total}</b>场景 · 推演通过 {stats.scenarios.pass_rate_pct}%<PlugMark label="场景走查" />
        </span>
      ) : null}
    </div>
  );
}

function Legend({ stats }) {
  return (
    <div className="sd-legend">
      <div className="b" style={{ fontSize: 12, marginBottom: 4 }}>图例</div>
      {SUM_KINDS.map(([k, f]) => (
        <div className="r" key={k}><span style={kindDotStyle(k, 10)} />{kindLabel(k)} {stats ? stats[f] : ''}</div>
      ))}
      <div className="r" style={{ marginTop: 6 }}><svg width="22" height="4"><line x1="0" y1="2" x2="22" y2="2" stroke="#9DB0CA" strokeWidth="1.6" /></svg>关系声明</div>
      <div className="r"><svg width="22" height="4"><line x1="0" y1="2" x2="22" y2="2" stroke="#B8C6DA" strokeWidth="1.4" strokeDasharray="4 3" /></svg>提及</div>
    </div>
  );
}

// 步骤正文：只认 [[条目]]，点了在图谱里选中它
function StepText({ text, onPick }) {
  return inlineTokens(text).map((t, i) => {
    if (t.t === 'link') return <span key={i} className="wl" onClick={() => onPick(t.v)}>{t.v}</span>;
    if (t.t === 'cite') return null;
    return <span key={i}>{t.v}</span>;
  });
}

// 底部操作流程面板：关联业务流程 / 操作步骤（ST-05）
function FlowPanel({ flow, loading, onClose, onPickEntry, onPickName }) {
  const [tab, setTab] = useState('biz');
  const [min, setMin] = useState(false);
  const [zoom, setZoom] = useState(100);
  const ref = useRef(null);
  const bf = flow?.business_flow;
  const full = async () => {
    try { await ref.current?.requestFullscreen(); } catch { toast('浏览器不允许全屏', 'alert'); }
  };
  return (
    <div className={`sd-flow${min ? ' min' : ''}`} ref={ref}>
      <div className="h">
        <span style={{ color: 'var(--teal)' }}><Icon name="flow" size={14} /></span>
        <b className="sm">操作流程 · {flow?.operation?.name || '…'}</b>
        <Seg items={[['biz', '关联业务流程'], ['steps', '操作步骤']]} value={tab} onChange={setTab} />
        <span className="xs muted"><Icon name="info" size={12} /> 展示操作所在的完整业务流程，引用节点已高亮</span>
        <span className="sp" />
        <div className="row g4">
          <Button size="xs" icon="zoomout" title="缩小流程图" onClick={() => setZoom((z) => Math.max(50, z - 10))} />
          <span className="xs num muted" style={{ width: 36, textAlign: 'center' }}>{zoom}%</span>
          <Button size="xs" icon="zoomin" title="放大流程图" onClick={() => setZoom((z) => Math.min(200, z + 10))} />
          <Button size="xs" icon="maximize" title="全屏查看流程图" onClick={full} />
          <Button size="xs" icon={min ? 'chevu' : 'chevd'} title={min ? '展开' : '收起'} onClick={() => setMin((v) => !v)} />
          <Button size="xs" icon="x" title="关闭操作流程" onClick={onClose} />
        </div>
      </div>
      <div className="b">
        <div style={{ zoom: zoom / 100 }}>
          {loading && !flow ? <div className="xs muted">正在读取…</div> : null}
          {flow && tab === 'biz' ? (
            <>
              <div className="row" style={{ gap: 6, alignItems: 'center' }}>
                {(bf?.steps || []).map((s, i) => (
                  <span key={i} className="row" style={{ gap: 6 }}>
                    {i ? <span style={{ color: 'var(--line3)' }}><Icon name="arrow" size={14} /></span> : null}
                    <div className={`sd-step${s.hit ? ' hit' : ''}`} data-go={s.entry ? '' : undefined} onClick={() => s.entry && onPickEntry(s.entry)}>
                      <b>{s.label}</b><small>{s.owner_label}</small>
                    </div>
                  </span>
                ))}
              </div>
              <div className="row mt12 xs muted">
                <Icon name="branch" size={12} />
                所在流程：{bf?.main_operation?.name || '—'}（主操作）· 子操作 {bf?.sub_operations ?? 0} 个 · 本操作被 {bf?.referenced_by_scenarios ?? 0} 个场景引用
              </div>
            </>
          ) : null}
          {flow && tab === 'steps' ? (
            flow.steps?.length ? (
              <ol style={{ paddingLeft: 18, fontSize: 12.8, lineHeight: 1.9, color: 'var(--ink2)' }}>
                {flow.steps.map((s) => <li key={s.index}><StepText text={s.text_md} onPick={onPickName} /></li>)}
              </ol>
            ) : <div className="xs muted">这个操作还没有写步骤</div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function EvidenceCell({ cell }) {
  if (!cell) return null;
  if (cell.expert_confirmed) return <Chip tone="gray" sm>专家确认</Chip>;
  if (cell.missing || !cell.anchor) return <Chip tone="amber" sm>缺证据</Chip>;
  const db = cell.scheme === 'db';
  return (
    <span className={`ev${db ? ' db' : ''}`} data-ev={cell.anchor}>
      <Icon name={db ? 'db' : cell.scheme === 'code' ? 'code' : 'quote'} />
      {evidenceShortLabel(cell)}
    </span>
  );
}

function EvidenceCard({ item, pid }) {
  const { api } = useApp();
  const [used, setUsed] = useState(null);
  const db = item.scheme === 'db';
  const [conf, tone] = CONFIDENCE_CHIP[item.confidence] || [];
  const page = item.resolved?.page || (db ? 'schema' : 'resources');
  const whereUsed = async () => {
    if (used) { setUsed(null); return; }
    try { setUsed(await api.evidenceWhereUsed(pid, item.anchor)); } catch (e) { toast(errorText(e), 'alert'); }
  };
  return (
    <div className="card pad mb8" style={{ padding: '10px 12px' }}>
      <div className="row">
        <span style={{ color: db ? 'var(--teal)' : 'var(--blue)' }}><Icon name={db ? 'db' : 'filetext'} size={14} /></span>
        <b className="sm grow">{item.title}</b>
        {conf ? <Chip tone={tone} sm>{conf}</Chip> : null}
      </div>
      <div className="xs muted mt4">用于属性{(item.used_for || []).map((u) => `「${u}」`).join('、')}</div>
      <div className="row mt8">
        <Button size="xs" onClick={() => navigate(pagePath(page, pid), { anchor: item.anchor })}>查看原文</Button>
        <Button size="xs" kind="ghost" onClick={whereUsed}>被谁引用</Button>
        <span className="sp" />
        <Button size="xs" kind="ghost" icon="copy" title="复制锚点" onClick={async () => toast((await copyText(item.anchor)) ? '已复制锚点' : '复制失败，请手动选择', 'copy')} />
      </div>
      {used ? (
        <div className="sd-wu">
          {used.items?.length ? used.items.map((u, i) => {
            const v = backlinkView(u.target);
            return (
              <div key={i} className="it" onClick={() => !v.artifact && gotoEntry(pid, u.target.id)}>
                <Icon name={v.icon} size={12} /><b style={{ fontWeight: 600 }}>{v.name}</b><span className="muted">{v.kind} · {u.location}</span>
              </div>
            );
          }) : <span className="muted">没有别的条目用到这个出处</span>}
        </div>
      ) : null}
    </div>
  );
}

function fmtCollected(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 右侧选中详情
function SidePanel({ entry, evidence, loading, onClose, onSelect }) {
  const { pid } = useProject();
  const lockFor = useLockCopy();
  const [tab, setTab] = useState('attr');
  useEffect(() => { setTab('attr'); }, [entry?.id]);
  if (!entry) {
    return (
      <aside className="sd-side">
        <div className="sd-empty">{loading ? '正在读取…' : '点选图谱里的节点，查看它的属性、关系和证据'}</div>
      </aside>
    );
  }
  const ref = { kind: entry.kind, id: entry.id, key: entry.key, name: entry.name };
  const lock = lockFor(ref, entry.name);
  const c = entry.counts || {};
  const declared = entry.relations?.declared || [];
  const mentions = entry.relations?.mentions || [];
  const backlinks = entry.backlinks || [];
  const evItems = evidence?.items || [];
  const basis = evidence?.snapshot_basis || [];
  // 快照的来源叫法取同一快照下证据的 source_label（「业务系统 ERP」），不显示数据源 id
  const sourceLabel = (b) => {
    const hit = evItems.find((x) => x.scheme === 'db' && x.resolved?.snapshot_id === b.snapshot_id);
    return hit ? `${hit.resolved.source_label} ` : `${b.source_id} `;
  };
  const openBacklink = (src) => {
    if (src.type === 'trace.scenario') navigate(pagePath('scenarios', pid), { key: src.key });
    else if (src.type) gotoEntry(pid, `flowchart:${src.key}`);
    else gotoEntry(pid, src.id);
  };
  return (
    <aside className="sd-side">
      <div className="hd">
        <div className="row">
          <span style={kindDotStyle(entry.kind, 12)} />
          <b className="lg">{entry.name}</b>
          <Chip tone={KIND_META[entry.kind]?.tone} sm>{kindLabel(entry.kind)}</Chip>
          {entry.status === 'editing' ? <Chip tone="blue" sm icon="edit">编辑中</Chip> : <St k={STATE_KEY[entry.status] || 'confirmed'} sm />}
          <LockMark info={lock} />
          <span className="sp" />
          <Button size="xs" kind="ghost" icon="x" title="关闭摘要" onClick={onClose} />
        </div>
        <div className="row mt4 xs muted"><span>Key</span><span className="mono">{entry.key}</span></div>
        {entry.description ? <p className="sm mt8" style={{ color: 'var(--ink2)' }}>{entry.description}</p> : null}
        <div className="row g16 mt8 sm">
          <span><b className="num">{c.refs_out ?? 0}</b> <span className="muted">引用</span></span>
          <span><b className="num">{c.refs_in ?? 0}</b> <span className="muted">被引</span></span>
          <span><b className="num">{c.fields ?? 0}</b> <span className="muted">属性</span></span>
          <span><b className="num">{c.evidence ?? 0}</b> <span className="muted">证据</span></span>
        </div>
        <div className="mt8">
          <Tabs items={[['attr', '属性', c.fields ?? 0], ['rel', '关系', backlinks.length + declared.length], ['ev', '证据', c.evidence ?? 0, 'quote']]} value={tab} onChange={setTab} />
        </div>
      </div>
      {tab === 'attr' ? (
        <div className="bd">
          {entry.fields?.length ? (
            <table className="tbl compact sd-attr">
              <thead><tr><th>属性</th><th>类型</th><th>证据</th></tr></thead>
              <tbody>
                {entry.fields.map((f) => (
                  <tr key={f.column || f.name}>
                    <td><div className="b">{f.name}</div><div className="en">{f.column}</div></td>
                    <td className="muted">{f.type}</td>
                    <td><EvidenceCell cell={f.evidence} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="xs muted">{entry.kind === 'object' ? '还没有属性' : `${kindLabel(entry.kind)}没有属性表，内容在「编辑条目」里`}</div>}
          <div className="row mt12">
            <Button size="sm" kind="primary" icon="edit" onClick={() => gotoEntry(pid, entry.id)}>编辑条目</Button>
            <Button size="sm" icon="sparkle" data-pop={`回到工作台，把「${entry.name}」作为引用带进对话`} onClick={() => askAssistant(pid, entry.id)}>问助手</Button>
          </div>
        </div>
      ) : null}
      {tab === 'rel' ? (
        <div className="bd">
          <div className="sec-t">被谁引用 · {backlinks.length}</div>
          {backlinks.map((b, i) => {
            const v = backlinkView(b.source);
            const lk = b.locked ? lockFor(b.source, v.name) : null;
            return (
              <div key={i} className="ov-li sm" style={{ cursor: 'pointer' }} onClick={() => openBacklink(b.source)}>
                <Icon name={v.icon} size={14} />
                <span className="grow"><b style={{ fontWeight: 600 }}>{v.name}</b><span className="xs muted" style={{ marginLeft: 6 }}>{v.kind} · {relationText(b.relation)}</span></span>
                <LockIcon info={lk} />
                <Icon name="chev" size={12} />
              </div>
            );
          })}
          <div className="sec-t mt12">关系声明 · {declared.length}</div>
          {declared.map((r, i) => (
            <div key={i} className="ov-li sm" style={{ cursor: 'pointer' }} onClick={() => onSelect(r.target.id)}>
              <Icon name="link" size={14} /><span className="grow">{r.target.name} · {r.label}</span><Chip tone="line" sm>实线</Chip>
            </div>
          ))}
          <div className="sec-t mt12">只是提及 · {mentions.length}</div>
          {mentions.map((r, i) => (
            <div key={i} className="ov-li sm muted" style={{ cursor: 'pointer' }} onClick={() => onSelect(r.target.id)}>
              <Icon name="quote" size={14} /><span className="grow">{r.target.name} · {r.where}</span><Chip tone="gray" sm>虚线</Chip>
            </div>
          ))}
        </div>
      ) : null}
      {tab === 'ev' ? (
        <div className="bd">
          <div className="note teal" style={{ marginBottom: 10 }}>
            <Icon name="shieldok" />
            <div>
              {basis.length
                ? `证据来自资料原文，以及${basis.map((b) => `${sourceLabel(b)}最近一次采集（${fmtCollected(b.collected_at)}）`).join('、')}`
                : '证据来自资料原文'}
            </div>
          </div>
          {evidence ? evItems.map((it) => <EvidenceCard key={it.anchor + it.title} item={it} pid={pid} />) : <div className="xs muted">正在读取…</div>}
          {evidence && !evItems.length ? <div className="xs muted">还没有证据锚点</div> : null}
        </div>
      ) : null}
    </aside>
  );
}

export default function StudioPage({ route }) {
  const { api } = useApp();
  const { pid, subscribe } = useProject();
  const selectedId = route.query?.entry || '';
  const [stats, setStats] = useState(null);
  const [graph, setGraph] = useState(null);
  const [mentions, setMentions] = useState(true);
  const [q, setQ] = useState('');
  const query = useDebounced(q, 300);
  const [hl, setHl] = useState('all');
  const [entry, setEntry] = useState(null);
  const [evidence, setEvidence] = useState(null);
  const [loadingEntry, setLoadingEntry] = useState(false);
  const [flow, setFlow] = useState(null);
  const [flowLoading, setFlowLoading] = useState(false);
  const [flowClosed, setFlowClosed] = useState(false);
  const [tick, setTick] = useState(0);
  const canvasRef = useRef(null);

  const fail = useCallback((e) => toast(errorText(e), 'alert'), []);
  useEffect(() => { api.studioStats(pid).then(setStats).catch(fail); }, [api, pid, tick, fail]);
  useEffect(() => { api.studioGraph(pid, mentions ? undefined : { include_mentions: false }).then(setGraph).catch(fail); }, [api, pid, mentions, tick, fail]);
  // 别处保存了新版本（Studio、助手、MCP）：统计、图、选中详情一起刷新
  useEffect(() => subscribe((env) => { if (env.type === 'workspace.changed') setTick((t) => t + 1); }), [subscribe]);

  useEffect(() => {
    if (!selectedId) { setEntry(null); setEvidence(null); return undefined; }
    let alive = true;
    setLoadingEntry(true);
    setEvidence(null);
    api.studioEntry(pid, selectedId)
      .then((e) => { if (alive) setEntry(e); })
      .catch((e) => { if (alive) { setEntry(null); if (e.code !== 'endpoint_removed') fail(e); } })
      .finally(() => alive && setLoadingEntry(false));
    api.studioEntryEvidence(pid, selectedId).then((r) => alive && setEvidence(r)).catch(() => alive && setEvidence({ items: [], snapshot_basis: [] }));
    return () => { alive = false; };
  }, [api, pid, selectedId, tick, fail]);

  // 操作流程：选中操作就是它；选中对象 / 规则时取引用它的第一个操作
  const operationId = useMemo(() => {
    if (!entry || entry.id !== selectedId) return null;
    if (entry.kind === 'operation') return entry.id;
    return (entry.backlinks || []).find((b) => b.source?.kind === 'operation')?.source.id || null;
  }, [entry, selectedId]);
  useEffect(() => { if (entry?.kind === 'operation') setFlowClosed(false); }, [entry]);
  useEffect(() => {
    if (!operationId) { setFlow(null); return undefined; }
    let alive = true;
    setFlowLoading(true);
    api.studioEntryFlow(pid, operationId).then((f) => alive && setFlow(f)).catch(() => alive && setFlow(null)).finally(() => alive && setFlowLoading(false));
    return () => { alive = false; };
  }, [api, pid, operationId, tick]);

  const select = useCallback((id) => navigate(pagePath('studio', pid), id ? { entry: id } : undefined), [pid]);
  const open = useCallback((id) => gotoEntry(pid, id), [pid]);
  const pickName = (name) => {
    const n = (graph?.nodes || []).find((x) => x.label === name);
    if (n) select(n.id);
  };
  const fullscreen = async () => {
    try {
      await canvasRef.current?.requestFullscreen();
      toast('已进入全屏（Esc 退出）', 'maximize');
    } catch {
      toast('浏览器不允许全屏', 'alert');
    }
  };

  return (
    <ShellP active="studio" flush>
      <div className="sd">
        <div className="sd-tb">
          <Inp icon="search" w={270} placeholder="搜索名称、Key 或说明" value={q} onChange={(e) => setQ(e.target.value)} />
          <Seg items={[['all', '全部'], ['rule', '规则链'], ['metric', '指标链']]} value={hl} onChange={setHl} />
          <div className="vdiv" style={{ height: 22, alignSelf: 'center' }} />
          <label className="row sm" style={{ gap: 7 }}><Toggle on={mentions} onChange={setMentions} /><span>显示「提及」</span></label>
          <span className="sp" />
          <StatsBar stats={stats} pid={pid} />
          <Button size="sm" kind="ghost" icon="info" data-pop={LEGEND_POP} aria-label="图例" />
          <Button size="sm" icon="maximize" title="全屏查看" onClick={fullscreen} />
        </div>
        <div className="sd-main">
          <div className="sd-canvas" ref={canvasRef}>
            {graph ? (
              <LegacyStudioGraphHost graph={graph} query={query} highlight={hl} selectedId={selectedId} onSelect={select} onOpen={open} />
            ) : null}
            <Legend stats={stats} />
            {operationId && !flowClosed ? (
              <FlowPanel
                flow={flow}
                loading={flowLoading}
                onClose={() => { setFlowClosed(true); toast('已关闭操作流程，选中操作节点可再次打开', 'flow'); }}
                onPickEntry={(ref) => select(ref.id)}
                onPickName={pickName}
              />
            ) : null}
          </div>
          <SidePanel entry={entry && entry.id === selectedId ? entry : null} evidence={evidence} loading={loadingEntry} onClose={() => select('')} onSelect={select} />
        </div>
      </div>
    </ShellP>
  );
}
