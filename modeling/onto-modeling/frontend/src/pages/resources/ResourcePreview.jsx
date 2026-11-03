// 资料区右侧的预览卡（设计稿 .evd-prev）：文档（R-06，带出处编号、定位到锚点）、表格与 CSV（F-05）、
// drawio（F-02 读、F-03 存）、HTML 报告（F-04，沙箱 iframe）；「被引用」页签（R-05）；外部反馈的逐条查看（R-11）
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { DrawioEditorFrame, drawioStateText } from '../../components/DrawioView.jsx';
import DrawioPreview from '../../components/DrawioPreview.jsx';
import { isLocked } from '../../state/readonly.js';
import { Button, Chip, Icon, Modal, Note, Seg, cx, useEscape } from '../../ui/index.jsx';
import { useProjectEvent } from '../plugin/parts.jsx';
import {
  PREVIEW_ICON, blockBody, isDocPreview, blockLabel, coverageChip, csvText, diamondPoints, drawioLayout, feedbackView, fmtSize,
  parseDrawio, parseFragment, purposeView, refRows, refTarget, scaleText, sheetView, splitHighlights, tableTruncated, cellPos,
} from './resourcesModel.js';

const enc = encodeURIComponent;
const rawPath = (pid, path, download) => `/projects/${enc(pid)}/files/raw?path=${enc(path)}${download ? '&download=true' : ''}`;

function useDownload(name, path) {
  const { services } = useApp();
  const { pid } = useProject();
  return async () => {
    try {
      await services.download(rawPath(pid, path, true), name);
      toast('已开始下载原文件', 'download');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
}

// ---------- 文档：带锚点的块 ----------
function DocPane({ m, fragment, onCoverage, onUploadText, tasks }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [data, setData] = useState(null);
  const paperRef = useRef(null);
  const around = parseFragment(fragment)?.type === 'block' ? fragment : null;

  useEffect(() => {
    let alive = true;
    setData(null);
    api.resourcePreview(pid, m.path, around || undefined)
      .then((r) => { if (alive) { setData(r); onCoverage(r.coverage || null); } })
      .catch((e) => alive && setData({ error: errorText(e) }));
    return () => { alive = false; };
  }, [api, pid, m.path, m.revision, around, onCoverage]);

  // 跳过来的锚点：把那一段滚到预览中间
  useLayoutEffect(() => {
    const paper = paperRef.current;
    const at = paper?.querySelector('.rs-at');
    if (!paper || !at) return;
    const p = paper.getBoundingClientRect();
    const t = at.getBoundingClientRect();
    paper.scrollTop += t.top - p.top - (p.height - t.height) / 2;
  }, [data]);

  if (!data) return <div className="evd-paper rs-loading"><span className="spin" />正在读取原文…</div>;
  if (data.error) return <Note tone="red">{data.error}</Note>;
  const blocks = data.blocks || [];
  if (!blocks.length) {
    return m.kind === 'pdf' ? (
      <Note tone="amber">
        这种格式读不了，请上传文本版
        <div className="row mt8"><Button size="xs" kind="primary" icon="upload" onClick={onUploadText}>上传文本版</Button></div>
      </Note>
    ) : <Note tone="gray">这份资料还没有可预览的文字。</Note>;
  }
  const missing = around && !blocks.some((b) => b.anchor === around);
  const cov = data.coverage;
  const partial = cov && !cov.complete && cov.read_pct > 0;
  return (
    <>
      {missing ? <Note tone="amber" className="mb8">原文里找不到 {blockLabel(around) || around}，资料可能已改过。</Note> : null}
      <div className="evd-paper" ref={paperRef}>
        {blocks.map((b) => {
          if (b.kind === 'heading') return <h5 key={b.anchor} data-anchor={b.anchor}>{b.text}</h5>;
          const label = blockLabel(b.anchor);
          const body = blockBody(b);
          return (
            <p key={b.anchor} data-anchor={b.anchor} className={cx(b.anchor === around && 'rs-at')}>
              {label ? <span className="an">{label}</span> : null}
              {splitHighlights(body.text, body.highlights).map((s, i) => (s.hit ? <span key={i} className="hit">{s.t}</span> : s.t))}
            </p>
          );
        })}
      </div>
      {partial ? (
        <div className="row mt8">
          <span className="sp" />
          <Button
            size="xs" kind="soft" icon="sparkle" disabled={tasks.sending}
            onClick={() => tasks.send({ command: '/ontology-research', args: { path: m.path, reading_mode: 'full' }, text: `把「${m.name}」没读的部分读完` })}
          >
            让助手读完
          </Button>
        </div>
      ) : null}
    </>
  );
}

// ---------- 表格（xlsx 多工作表）与 CSV ----------
function useTable(path, revision) {
  const { api } = useApp();
  const { pid } = useProject();
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    setData(null);
    api.tablePreview(pid, path).then((r) => alive && setData(r)).catch((e) => alive && setData({ error: errorText(e) }));
    return () => { alive = false; };
  }, [api, pid, path, revision]);
  return data;
}

function SheetPane({ m, fragment }) {
  const data = useTable(m.path, m.revision);
  const focus = parseFragment(fragment);
  const cell = focus?.type === 'cell' ? cellPos(focus.cell) : null;
  const [pick, setPick] = useState(null);
  const sheets = data?.sheets || [];
  const focusIndex = focus?.type === 'cell' ? sheets.findIndex((s) => s.name === focus.sheet) : -1;
  const index = pick ?? (focusIndex >= 0 ? focusIndex : 0);
  const sheet = sheets[index];
  const view = useMemo(() => sheetView(sheet), [sheet]);
  const hereFocus = cell && (focusIndex < 0 || focusIndex === index);
  const scrollRef = useRef(null);
  useLayoutEffect(() => {
    scrollRef.current?.querySelector('.rs-cell')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [view]);
  if (!data) return <div className="evd-sheet rs-loading"><span className="spin" />正在读取表格…</div>;
  if (data.error) return <Note tone="red">{data.error}</Note>;
  return (
    <>
      <div className="evd-sheet">
        <div className="rs-sheet-scroll" ref={scrollRef}>
          <table>
            <thead>
              <tr>
                <th className="rn" />
                {view.header.map((h, c) => <th key={c} className={cx(hereFocus && cell.row === 1 && cell.col === c && 'rs-cell')}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {view.body.map((r) => (
                <tr key={r.n}>
                  <td className="rn">{r.n}</td>
                  {r.cells.map((v, c) => <td key={c} className={cx(hereFocus && cell.row === r.n && cell.col === c && 'rs-cell')}>{v}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="evd-tabs">
          {sheets.map((s, i) => <span key={s.name} className={i === index ? 'on' : ''} onClick={() => setPick(i)}>{s.name}</span>)}
        </div>
      </div>
      {tableTruncated(data, sheet) ? <div className="xs muted mt8 row g4"><Icon name="info" size={12} />仅预览前 500 行 × 60 列</div> : null}
    </>
  );
}

function CsvPane({ m }) {
  const data = useTable(m.path, m.revision);
  if (!data) return <div className="evd-csv rs-loading"><span className="spin" />正在读取…</div>;
  if (data.error) return <Note tone="red">{data.error}</Note>;
  const sheet = data.sheets?.[0];
  const total = sheet?.total_rows ?? sheet?.rows?.length ?? 0;
  return (
    <>
      <div className="evd-csv">{csvText(sheet?.rows || [])}</div>
      {total > 500 ? <div className="xs muted mt8">仅预览前 500 行</div> : null}
    </>
  );
}

// ---------- drawio ----------
function DrawioSvg({ xml, full }) {
  const model = useMemo(() => parseDrawio(xml), [xml]);
  const lay = useMemo(() => drawioLayout(model), [model]);
  const id = useMemo(() => `rsA${Math.random().toString(36).slice(2, 8)}`, []);
  if (model.compressed) return <DrawioPreview xml={xml} hint="" />;
  if (!lay.nodes.length) return <span className="xs muted">空白流程图</span>;
  return (
    <svg viewBox={lay.viewBox} width={full ? '100%' : lay.width} height={full ? '100%' : lay.height} style={{ maxWidth: '100%', maxHeight: '100%' }}>
      <defs><marker id={id} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 7 3.5 0 7z" fill="#93A1B5" /></marker></defs>
      <g stroke="#93A1B5" strokeWidth="1.4" fill="none">
        {lay.edges.map((e) => <path key={e.id} d={e.d} markerEnd={`url(#${id})`} />)}
      </g>
      {lay.edges.filter((e) => e.label).map((e) => <text key={`l${e.id}`} x={e.label.x} y={e.label.y} textAnchor={e.label.anchor} style={{ fill: '#B45309', fontSize: 10 }}>{e.label.text}</text>)}
      {lay.nodes.map((n) => {
        const common = { fill: n.fill, stroke: n.stroke, strokeDasharray: n.dashed ? '4 3' : undefined };
        const lines = n.label.split('\n');
        return (
          <g key={n.id}>
            {n.shape === 'diamond' ? <polygon points={diamondPoints(n)} {...common} />
              : n.shape === 'ellipse' ? <ellipse cx={n.x + n.w / 2} cy={n.y + n.h / 2} rx={n.w / 2} ry={n.h / 2} {...common} />
                : <rect x={n.x} y={n.y} width={n.w} height={n.h} rx={n.rx} {...common} />}
            <text x={n.x + n.w / 2} y={n.y + n.h / 2 + 4 - ((lines.length - 1) * 13) / 2} textAnchor="middle" style={{ fill: n.color }}>
              {lines.map((t, i) => <tspan key={i} x={n.x + n.w / 2} dy={i ? 13 : 0}>{t}</tspan>)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function DrawioPane({ m, full, onFull, onSaved }) {
  const { api, config } = useApp();
  const { pid, scope } = useProject();
  const [doc, setDoc] = useState(null);
  const [tick, setTick] = useState(0);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null); // 编辑器里没保存的内容（关掉编辑器也保留）
  const [changed, setChanged] = useState(false); // 有草稿时文件被别处改了（设计稿「被助手修改」）
  const [edState, setEdState] = useState('loading');
  const [saving, setSaving] = useState(false);
  const frame = useRef(null);
  const latest = useRef(null);
  const download = useDownload(m.name, m.path);
  const locked = isLocked(scope, { path: m.path });

  useEffect(() => {
    let alive = true;
    api.fileContent(pid, m.path).then((r) => alive && setDoc(r)).catch((e) => alive && setDoc({ error: errorText(e) }));
    return () => { alive = false; };
  }, [api, pid, m.path, m.revision, tick]);

  useProjectEvent('workspace.changed', (d) => {
    if (!(d.paths || []).includes(m.path) || d.source === 'web') return;
    if (draft != null || editing) setChanged(true); else setTick((t) => t + 1);
  });

  const closeEditor = () => {
    if (latest.current != null) setDraft(latest.current);
    setEditing(false);
  };
  const persist = async (xml) => {
    setSaving(true);
    try {
      const r = await api.saveFileContent(pid, { path: m.path, content: xml, base_revision: doc.revision, summary: `编辑 ${m.name}` });
      setDoc({ ...doc, content: xml, revision: r.revision });
      latest.current = null;
      setDraft(null);
      setChanged(false);
      setEditing(false);
      toast(`已保存，版本 ${r.version_label}`, 'check');
      onSaved?.();
    } catch (e) {
      latest.current = xml;
      if (e.code === 'revision_conflict') {
        setDraft(xml);
        setChanged(true);
        setEditing(false);
      } else toast(errorText(e), 'alert');
      throw e;
    } finally {
      setSaving(false);
    }
  };
  const reloadLatest = () => {
    latest.current = null;
    setDraft(null);
    setChanged(false);
    setTick((t) => t + 1);
    toast('已载入最新版本', 'refresh');
  };

  return (
    <>
      <div className="row mb8">
        <Button size="sm" kind="soft" icon="edit" disabled={!doc || !!doc.error || locked} title={locked ? '在助手的写入范围里，这一轮结束后才能编辑' : undefined} onClick={() => { latest.current = draft; setEditing(true); }}>编辑</Button>
        <Button size="sm" icon="refresh" title="刷新预览" onClick={() => setTick((t) => t + 1)} />
        <Button size="sm" icon="download" title="下载原文件" onClick={download} />
        <Button size="sm" icon="maximize" title={full ? '退出全屏' : '全屏'} onClick={onFull} />
        {draft != null && !changed ? <><span className="sp" /><Chip tone="amber" sm dot>有未保存的修改</Chip></> : null}
      </div>
      {changed ? (
        <div style={{ marginBottom: 8 }}>
          <Note tone="amber">
            流程图已被助手修改，你的编辑还在。
            <div className="row mt8">
              <Button size="xs" onClick={() => { latest.current = draft; setEditing(true); }}>继续编辑</Button>
              <Button size="xs" kind="primary" onClick={reloadLatest}>载入最新版本</Button>
            </div>
          </Note>
        </div>
      ) : null}
      <div className="evd-dio">
        {!doc ? <span className="xs muted"><span className="spin" /> 正在读取流程图…</span>
          : doc.error ? <Note tone="red">{doc.error}</Note>
            : <DrawioSvg xml={doc.content} full={full} />}
      </div>
      <Modal
        open={editing} onClose={closeEditor} icon="flow" title={`编辑 ${m.name}`} w={1080}
        foot={(
          <>
            <span className="xs muted grow">{drawioStateText(edState)}</span>
            <Button onClick={closeEditor}>关闭</Button>
            <Button kind="primary" icon="check" disabled={edState !== 'ready' || saving} onClick={() => frame.current?.save()}>保存</Button>
          </>
        )}
      >
        <div className="rs-dio-edit">
          {editing ? (
            <DrawioEditorFrame
              ref={frame} xml={draft ?? doc?.content ?? ''} title={m.name} available={config?.features?.drawio !== false}
              onStatus={setEdState} onChange={(x) => { latest.current = x; }} onSave={persist}
            />
          ) : null}
        </div>
      </Modal>
    </>
  );
}

// ---------- HTML 报告：沙箱 iframe（不给脚本权限） ----------
function HtmlPane({ m }) {
  const { api, services } = useApp();
  const { pid } = useProject();
  const [html, setHtml] = useState(null);
  const [bg, setBg] = useState('w');
  useEffect(() => {
    let alive = true;
    setHtml(null);
    services.client.request('GET', `/projects/${enc(pid)}/files/raw`, { query: { path: m.path }, raw: true })
      .then((r) => r.text())
      .then((t) => alive && setHtml(t))
      .catch((e) => alive && setHtml({ error: errorText(e) }));
    return () => { alive = false; };
  }, [services, pid, m.path, m.revision]);
  return (
    <>
      <div className="row mb8">
        <span className="sm muted">预览底色</span>
        <Seg items={[['w', '白底'], ['g', '透明格'], ['d', '深色']]} value={bg} onChange={setBg} />
        <span className="sp" />
        <Button size="sm" icon="arrow" onClick={() => window.open(api.fileRawUrl(pid, m.path), '_blank', 'noopener')}>在新窗口打开</Button>
      </div>
      <div className={cx('evd-html', bg === 'g' && 'bg-grid', bg === 'd' && 'bg-dark')}>
        {html == null ? <span className="xs muted"><span className="spin" /> 正在读取…</span>
          : html.error ? <Note tone="red">{html.error}</Note>
            : <iframe className="rs-html-frame" title={m.name} sandbox="" srcDoc={html} />}
      </div>
    </>
  );
}

// ---------- 被引用（R-05） ----------
function RefsPane({ m, onLocate }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    setData(null);
    api.resourceReferences(pid, m.path).then((r) => alive && setData(r)).catch((e) => alive && setData({ error: errorText(e) }));
    return () => { alive = false; };
  }, [api, pid, m.path, m.revision]);
  if (!data) return <div className="xs muted">正在查引用…</div>;
  if (data.error) return <Note tone="red">{data.error}</Note>;
  const rows = refRows(data.items);
  if (!rows.length) return <div className="sm muted">还没有产物引用这份资料。</div>;
  return (
    <div className="col" style={{ gap: 0 }}>
      {rows.map((r, i) => {
        const to = refTarget(r.target);
        return (
          <div key={i} className={cx('evd-wu', to && 'rs-go')} title={to ? '去看这一项' : undefined} onClick={() => to && navigate(pagePath(to.page, pid), to.query)}>
            <Icon name="link" size={14} />
            <div className="grow"><b>{r.title}</b><div className="xs muted">{r.sub}</div></div>
            {r.chip ? (
              <span
                className="chip line sm rs-go" title="在原文里定位"
                onClick={(e) => { e.stopPropagation(); if (isDocPreview(m)) onLocate(r.anchors[0]); }}
              >
                {r.chip}
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

// ---------- 预览卡 ----------
export function MaterialPreview({ m, purposes, fragment, full, onFull, onSaved, onUploadText, tasks }) {
  const [tab, setTab] = useState('pv');
  const [focus, setFocus] = useState(fragment);
  const [coverage, setCoverage] = useState(null);
  const download = useDownload(m.name, m.path);
  useEffect(() => { setFocus(fragment); setTab('pv'); }, [m.path, fragment]);
  useEffect(() => { setCoverage(null); }, [m.path]);
  const onCoverage = useCallback((c) => setCoverage(c), []);
  const cov = isDocPreview(m) ? coverageChip(coverage) : null;
  const p = purposeView(m, purposes);
  const scale = isDocPreview(m) || m.preview === 'drawio' || m.preview === 'html' ? fmtSize(m.size) : scaleText(m);

  let pane;
  if (isDocPreview(m)) pane = <DocPane m={m} fragment={focus} onCoverage={onCoverage} onUploadText={onUploadText} tasks={tasks} />;
  else if (m.preview === 'xls') pane = <SheetPane key={`${m.path}:${focus || ''}`} m={m} fragment={focus} />;
  else if (m.preview === 'csv') pane = <CsvPane m={m} />;
  else if (m.preview === 'drawio') pane = <DrawioPane key={m.path} m={m} full={full} onFull={onFull} onSaved={onSaved} />;
  else if (m.preview === 'html') pane = <HtmlPane m={m} />;
  else pane = <Note tone="gray">不能在线预览<div className="row mt8"><Button size="xs" icon="download" onClick={download}>下载原文件</Button></div></Note>;

  return (
    <div className="card">
      <div className="card-h">
        <span style={{ color: 'var(--blue)' }}><Icon name={PREVIEW_ICON[m.preview] || 'filetext'} /></span>
        <h3 className="ell" title={m.name}>{m.name}</h3>
        <span className="sp" />
        <Button size="sm" kind="ghost" icon="download" title="下载原文件" onClick={download} />
        <Button size="sm" kind="ghost" icon="maximize" title={full ? '退出全屏' : '全屏'} onClick={onFull} />
      </div>
      <div className="card-b">
        <div className="row mb12">
          <span className="row g4">
            <Chip tone="blue">{p.label}</Chip>
            <Chip tone="line">{scale}</Chip>
            {cov ? <Chip tone={cov.tone} icon={cov.icon}>{cov.text}</Chip> : null}
          </span>
          <span className="sp" />
          <Seg items={[['pv', '预览'], ['ref', '被引用']]} value={tab} onChange={setTab} />
        </div>
        {tab === 'pv' ? pane : <RefsPane m={m} onLocate={(a) => { setFocus(a); setTab('pv'); }} />}
      </div>
    </div>
  );
}

// ---------- 外部反馈逐条查看（待处理「逐条查看」跳过来） ----------
export function FeedbackPreview({ fb, onSetState, onHandle, busy }) {
  const { pid } = useProject();
  const v = feedbackView(fb);
  return (
    <div className="card">
      <div className="card-h">
        <span style={{ color: v.open ? 'var(--amber2)' : 'var(--ink3)' }}><Icon name={v.icon} /></span>
        <h3 className="ell">{v.title}</h3>
        <span className="sp" />
        <Chip tone={v.state[1]} sm icon={v.state[2]}>{v.state[0]}</Chip>
      </div>
      <div className="card-b">
        <div className="row g4 mb12 wrap">
          <Chip tone="line">{fb.source?.service || '外部'}</Chip>
          {fb.release_version ? <Chip tone="line">知识包 {fb.release_version}</Chip> : null}
          {v.note ? <span className="xs muted">{v.note}</span> : null}
        </div>
        <div className="col" style={{ gap: 0 }}>
          {(fb.items || []).map((x) => (
            <div key={x.case_id} className="rs-case">
              <b className="sm">{x.question}</b>
              <div className="xs muted mt4">期望：{x.expected}　·　实际：{x.actual}</div>
              {x.loss_reason ? <div className="xs mt4" style={{ color: 'var(--amber)' }}>疑似原因：{x.loss_reason}</div> : null}
              {(x.suspected || []).length ? (
                <div className="row g4 mt4 wrap">
                  {x.suspected.map((t, i) => {
                    const to = refTarget(t);
                    return <Chip key={i} tone="line" sm icon="link" className="rs-go" title="去看这一项" onClick={() => to && navigate(pagePath(to.page, pid), to.query)}>{t.name || t.title}</Chip>;
                  })}
                </div>
              ) : null}
            </div>
          ))}
        </div>
        <div className="row mt12">
          {fb.state === 'open' ? (
            <>
              <Button size="sm" kind="primary" icon="msg" disabled={busy} onClick={onHandle}>在对话里处理</Button>
              <Button size="sm" disabled={busy} onClick={() => onSetState('handled')}>标为已处理</Button>
              <Button size="sm" kind="ghost" disabled={busy} onClick={() => onSetState('dismissed')}>忽略</Button>
            </>
          ) : (
            <>
              {fb.state === 'handled' && fb.handled_version_label ? <Button size="sm" icon="history" onClick={() => navigate(pagePath('versions', pid), { v: fb.handled_version_label })}>看对应版本</Button> : null}
              <Button size="sm" kind="ghost" disabled={busy} onClick={() => onSetState('open')}>重新打开</Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// 全屏预览：Esc 退出
export function useFullscreen() {
  const [full, setFull] = useState(false);
  const close = useCallback(() => setFull(false), []);
  useEscape(full, close);
  return [full, () => setFull((v) => !v), close];
}
