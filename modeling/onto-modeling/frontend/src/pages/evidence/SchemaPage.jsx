// 证据浏览器（系统逆向建模插件的资料子页，设计稿 schema）：按快照只读检索。
// 快照下拉（EV-18）、左栏分层摘要（EV-23）与检索（EV-24）、中栏 ER 视图（EV-28）、
// 右栏记录详情（EV-25）、被引用（EV-26）、相邻记录（EV-27）。
// 接住 Studio 证据卡「查看原文」：?anchor=db:<源>/<库.表[.字段]>，按锚点解析（EV-29）出的快照打开，定位到表和字段。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { ShellP } from '../../shell/Shell.jsx';
import { Button, Chip, Empty, Icon, Inp, Seg, Select, Table, cx } from '../../ui/index.jsx';
import { backlinkView } from '../studio/studioModel.js';
import {
  domainIndex, fieldAnchor, fieldDesc, fmtNum, hubTable, parseDbAnchor, pickSnapshot, shortName, treeTotal,
} from './evidenceModel.js';
import { ErView } from './ErView.jsx';
import { ConfChip, EvdPlg, EvidenceGate, copyText } from './parts.jsx';

const SEARCH_TYPES = [['all', '全部'], ['entity', '实体'], ['field', '字段']];
const JUMPS = [['fields', '字段'], ['ddl', 'DDL'], ['samples', '样本'], ['profile', '画像'], ['used', '被引用'], ['lineage', '相邻']];
const ZOOMS = [0.6, 0.8, 1, 1.25, 1.5, 2];

function Tree({ tree, sel, collapsed, onToggle, onPick }) {
  return (
    <>
      <div className="xs muted" style={{ padding: '2px 6px 6px', lineHeight: 1.5 }}><Icon name="layers" size={12} /> 按 summary.md 分层：业务域 → 实体，先读摘要再下钻</div>
      {(tree?.domains || []).map((d) => {
        const off = collapsed.has(d.name);
        return (
          <div key={d.name}>
            <div className={cx('dm', off && 'off')} onClick={() => onToggle(d.name)}>
              <Icon name="chevd" size={12} />{d.name}<span className="faint" style={{ fontWeight: 500 }}>{d.count}</span>
            </div>
            {off ? null : d.entities.map((e) => (
              <div key={e.anchor} className={cx('en', e.anchor === sel?.table && 'on')} onClick={() => onPick(e.anchor)}>
                <Icon name="table" size={14} />
                <span className="mono" style={{ fontSize: 11.5 }}>{shortName(e.name)}</span>
                <span className="muted xs ell">{e.label}</span>
                <span className="m" data-pop={e.mapped_to ? `已映射到本体对象「${e.mapped_to.name}」` : '还没有映射'}>
                  {e.mapped_to ? <span style={{ color: 'var(--blue)' }}><Icon name="check" size={12} /></span> : <span className="faint"><Icon name="dot" size={12} /></span>}
                </span>
              </div>
            ))}
          </div>
        );
      })}
    </>
  );
}

function Hits({ hits, sel, onPick }) {
  if (!hits) return <div className="sm muted" style={{ padding: 6 }}>正在检索…</div>;
  if (!hits.items.length) return <div className="sm muted" style={{ padding: 6 }}>没有匹配的表或字段。换个说法，或者去掉类型筛选。</div>;
  return (
    <>
      <div className="xs muted" style={{ padding: '2px 6px 6px' }}>evidence.search · 共 {hits.total} 条</div>
      {hits.items.map((h) => {
        const table = h.type === 'field' ? h.parent_anchor : h.anchor;
        const field = h.type === 'field' ? h.name : null;
        const on = sel?.table === table && (sel?.field || null) === field;
        return (
          <div key={h.anchor} className={cx('evp-hit', on && 'on')} onClick={() => onPick(table, field)}>
            <Icon name={h.type === 'entity' ? 'table' : 'list'} size={14} style={{ marginTop: 2 }} />
            <div className="t">
              <div className="row g4"><span className="mono b" style={{ fontSize: 11.5 }}>{h.type === 'entity' ? shortName(h.name) : h.name}</span><span className="muted xs ell">{h.label}</span></div>
              <div className="xs faint ell">{h.snippet}</div>
            </div>
            <ConfChip k={h.confidence} />
          </div>
        );
      })}
    </>
  );
}

function Detail({ record, sel, used, lineage, onField, onPick, pid }) {
  const scrollRef = useRef(null);
  const refs = useRef({});
  const mark = (k) => (el) => { refs.current[k] = el; };
  const jump = (k) => {
    const box = scrollRef.current;
    const el = refs.current[k];
    if (box && el) box.scrollTo({ top: el.offsetTop - 4, behavior: 'smooth' });
  };
  // 从锚点进来时把选中的字段滚到眼前
  useEffect(() => {
    if (!sel?.field) return;
    const el = scrollRef.current?.querySelector(`[data-f="${CSS.escape(sel.field)}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [record, sel?.field]);

  const shown = sel?.field ? fieldAnchor(record.anchor, sel.field) : record.anchor;
  const stats = (record.profile || []).filter((p) => p.stats);
  const topk = (record.profile || []).filter((p) => p.top_k);
  return (
    <>
      <div style={{ padding: '14px 14px 10px', borderBottom: '1px solid var(--line)' }}>
        <div className="row">
          <span style={{ width: 30, height: 30, borderRadius: 9, display: 'grid', placeItems: 'center', background: 'var(--t50)', color: 'var(--teal)' }}><Icon name="table" size={14} /></span>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="b lg ell">{record.name}</div>
            <div className="xs muted">{[record.label, `${record.fields?.length || 0} 个字段`, record.rows != null ? `${fmtNum(record.rows)} 行` : null].filter(Boolean).join(' · ')}</div>
          </div>
          <Chip tone="line" sm>{record.type}</Chip>
          <ConfChip k={record.confidence} />
        </div>
        <div className="row mt8">
          <span className="anchor grow ell">{shown}</span>
          <Button size="xs" kind="ghost" icon="copy" title="复制锚点" onClick={async () => toast((await copyText(shown)) ? `已复制锚点 ${shown}` : '复制失败，请手动选择', 'copy')} />
        </div>
        <div className="row g4 mt8 wrap evp-jump">
          {JUMPS.map(([k, t]) => <span key={k} className="chip line sm" onClick={() => jump(k)}>{t}</span>)}
          <span className="xs faint">evidence.show</span>
        </div>
      </div>
      <div className="scroll evd-det" ref={scrollRef} style={{ flex: 1, padding: '0 14px 14px', position: 'relative' }}>
        <div className="sh" ref={mark('fields')}><Icon name="list" size={12} />字段</div>
        {(record.fields || []).map((f) => (
          <div
            key={f.name}
            data-f={f.name}
            className={cx('evd-fl', sel?.field === f.name && 'on')}
            title={sel?.field === f.name ? '再点一下回到整张表' : '选中这个字段：被引用按字段看，锚点也换成字段的'}
            onClick={() => onField(sel?.field === f.name ? null : f.name)}
          >
            <div><span className="mono b" style={{ fontSize: 12 }}>{f.name}</span><div className="xs muted">{fieldDesc(f)}</div></div>
            <div><ConfChip k={f.confidence} /></div>
          </div>
        ))}
        <div className="sh" ref={mark('ddl')}><Icon name="code" size={12} />DDL</div>
        {record.ddl ? <div className="code" style={{ fontSize: 11 }}>{record.ddl}</div> : <div className="xs muted">这类记录没有 DDL。</div>}
        <div className="sh" ref={mark('samples')}><Icon name="table" size={12} />脱敏样本 {record.samples ? <span className="faint" style={{ fontWeight: 500 }}>{record.samples.shown} / {record.samples.max} 行</span> : null}</div>
        {record.samples?.rows?.length ? (
          <Table
            compact
            cols={record.samples.columns}
            rows={record.samples.rows.map((r) => r.map((c) => (/^h:/.test(c) ? <span className="mono" style={{ color: 'var(--indigo)' }}>{c}</span> : c)))}
          />
        ) : <div className="xs muted">这次快照没有采这张表的样本。</div>}
        <div className="sh" ref={mark('profile')}><Icon name="bars" size={12} />数据画像{topk[0] ? ` · ${topk[0].column} top-k` : ''}</div>
        {topk.map((p) => p.top_k.map((x) => (
          <div className="evd-bar" key={`${p.column}-${x.value}`}><span>{x.value}</span><i style={{ width: `${x.pct}%` }} /><span className="num muted">{x.pct}%</span></div>
        )))}
        {stats.map((p, i) => (
          <div className="xs muted mt4" key={p.column}>
            {p.column}：最小 {fmtNum(p.stats.min)} · 最大 {fmtNum(p.stats.max)} · 空值率 {p.stats.null_pct}%{i === stats.length - 1 ? ' · 数值列只给统计量' : ''}
          </div>
        ))}
        {!topk.length && !stats.length ? <div className="xs muted">还没有画像。</div> : null}
        <div className="sh" ref={mark('used')}><Icon name="link" size={12} />被引用 where-used <span className="faint" style={{ fontWeight: 500 }}>{used ? used.total : ''}</span>{sel?.field ? <span className="faint" style={{ fontWeight: 500 }}>· 只看 {sel.field}</span> : null}</div>
        {used === null ? <div className="xs muted">正在查…</div> : used.items.length ? used.items.map((u, i) => {
          const v = backlinkView(u.target);
          const go = () => (v.artifact
            ? navigate(pagePath(u.target.type === 'trace.scenario' ? 'scenarios' : 'studio-table', pid), u.target.key ? { key: u.target.key } : undefined)
            : navigate(pagePath('studio-table', pid), { entry: u.target.id }));
          return (
            <div className="evd-wu" key={i}>
              <Icon name={v.icon} size={14} />
              <div className="grow"><b>{v.name}</b><div className="xs muted">{v.kind} · {u.location}</div></div>
              <Button size="xs" kind="ghost" icon="arrow" title="去看这条" onClick={go} />
            </div>
          );
        }) : <div className="xs muted">{sel?.field ? '还没有产物引用这个字段。' : '还没有产物引用这张表。'}</div>}
        <div className="sh" ref={mark('lineage')}><Icon name="graph" size={12} />相邻记录 lineage</div>
        {lineage === null ? <div className="xs muted">正在查…</div> : lineage.length ? lineage.map((l) => {
          const db = /^db:/.test(l.anchor);
          const t = parseDbAnchor(l.anchor);
          return (
            <div className={cx('evd-wu', db && 'go')} key={l.anchor} title={db ? '打开这张表' : undefined} onClick={() => db && t && onPick(t.tableAnchor)}>
              <Icon name={db ? 'table' : 'code'} size={14} />
              <div className="grow" style={{ minWidth: 0 }}><b className="mono" style={{ fontSize: 12 }}>{l.label}</b><div className="xs muted ell">{l.basis}</div></div>
              <ConfChip k={l.confidence} />
            </div>
          );
        }) : <div className="xs muted">没有相邻记录。</div>}
      </div>
    </>
  );
}

function SchemaBody({ route }) {
  const { api } = useApp();
  const { pid } = useProject();
  const anchorQ = route?.query?.anchor || '';
  const snapQ = route?.query?.snapshot || '';
  const [snaps, setSnaps] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [tree, setTree] = useState(null);
  const [er, setEr] = useState(null);
  const [sel, setSel] = useState(null);
  const [record, setRecord] = useState(null);
  const [recErr, setRecErr] = useState(null);
  const [used, setUsed] = useState(null);
  const [lineage, setLineage] = useState(null);
  const [q, setQ] = useState('');
  const [qType, setQType] = useState('all');
  const [hits, setHits] = useState(null);
  const [collapsed, setCollapsed] = useState(() => new Set());
  const [zoom, setZoom] = useState(1);
  const [maxed, setMaxed] = useState(false);

  // 进页面：快照列表 → 定快照（地址里的、锚点解析出的、或最新完整的）→ 定选中的表
  useEffect(() => {
    let alive = true;
    (async () => {
      const list = (await api.evidenceSnapshots(pid)).items || [];
      const target = parseDbAnchor(anchorQ);
      let wanted = snapQ;
      if (!wanted && anchorQ) {
        try { wanted = (await api.resolveAnchors(pid, [anchorQ])).items?.[0]?.snapshot_id || ''; } catch { wanted = ''; }
      }
      if (!alive) return;
      setSnaps(list);
      setSnapshot(pickSnapshot(list, { wanted, sourceId: target?.source }));
      setSel(target ? { table: target.tableAnchor, field: target.field } : null);
      if (anchorQ && !target) toast('这个出处不是数据库锚点，证据浏览器打不开', 'alert');
    })().catch((e) => { if (alive) { setSnaps([]); toast(errorText(e), 'alert'); } });
    return () => { alive = false; };
  }, [api, pid, anchorQ, snapQ]);

  useEffect(() => {
    if (!snapshot) return undefined;
    let alive = true;
    setTree(null);
    api.evidenceSummaryTree(pid, { snapshot }).then((r) => alive && setTree(r)).catch((e) => toast(errorText(e), 'alert'));
    return () => { alive = false; };
  }, [api, pid, snapshot]);

  // ER：快照变了、或选中的表不在当前这张图里时重取；没指定表时选关系最多的那张
  const selTable = sel?.table || null;
  const needEr = !!snapshot && (!er || er.snapshot !== snapshot || (selTable && !er.tables.some((t) => t.anchor === selTable)));
  useEffect(() => {
    if (!needEr) return undefined;
    let alive = true;
    api.evidenceEr(pid, { snapshot, focus: selTable || undefined, depth: 2 }).then((r) => {
      if (!alive) return;
      const focus = selTable && r.tables.some((t) => t.anchor === selTable) ? selTable : hubTable(r);
      setEr({ ...r, snapshot, focus });
      if (!selTable && focus) setSel({ table: focus, field: null });
    }).catch((e) => toast(errorText(e), 'alert'));
    return () => { alive = false; };
  }, [api, pid, needEr, snapshot, selTable]);

  useEffect(() => {
    if (!snapshot || !selTable) return undefined;
    let alive = true;
    setRecord(null);
    setRecErr(null);
    setLineage(null);
    api.evidenceRecord(pid, selTable, snapshot).then((r) => alive && setRecord(r)).catch((e) => alive && setRecErr(e));
    api.evidenceLineage(pid, selTable, snapshot).then((r) => alive && setLineage(r.items || [])).catch(() => alive && setLineage([]));
    return () => { alive = false; };
  }, [api, pid, snapshot, selTable]);

  const selField = sel?.field || null;
  useEffect(() => {
    if (!selTable) return undefined;
    let alive = true;
    setUsed(null);
    const anchor = selField ? fieldAnchor(selTable, selField) : selTable;
    api.evidenceWhereUsed(pid, anchor, !selField).then((r) => alive && setUsed({ items: r.items || [], total: r.total ?? (r.items || []).length }))
      .catch(() => alive && setUsed({ items: [], total: 0 }));
    return () => { alive = false; };
  }, [api, pid, selTable, selField]);

  // 检索（evidence.search）：停手 250 毫秒再查
  useEffect(() => {
    const text = q.trim();
    if (!text || !snapshot) { setHits(null); return undefined; }
    let alive = true;
    const t = setTimeout(() => {
      api.evidenceSearch(pid, { q: text, type: qType === 'all' ? undefined : qType, snapshot })
        .then((r) => alive && setHits({ items: r.items || [], total: r.total ?? (r.items || []).length }))
        .catch((e) => { if (alive) { setHits({ items: [], total: 0 }); toast(errorText(e), 'alert'); } });
    }, 250);
    return () => { alive = false; clearTimeout(t); };
  }, [api, pid, q, qType, snapshot]);

  const domains = useMemo(() => domainIndex(tree), [tree]);
  const domainOf = useCallback((a) => domains.get(a) || null, [domains]);
  const pick = useCallback((table, field = null) => setSel({ table, field }), []);
  const toggleDomain = (name) => setCollapsed((s) => { const n = new Set(s); if (n.has(name)) n.delete(name); else n.add(name); return n; });
  const snapOptions = (snaps || []).map((s) => ({ value: s.id, label: `${s.source?.id} · ${s.id}（${s.complete ? '完整' : '不完整'}）` }));
  const focusTable = er?.tables.find((t) => t.anchor === er.focus);
  const selName = er?.tables.find((t) => t.anchor === selTable)?.name || shortName(selTable);
  const zi = ZOOMS.indexOf(zoom);

  if (snaps && !snaps.length) {
    return (
      <div className="evd-full">
        <div className="row"><b className="lg">证据浏览器</b><EvdPlg /><span className="muted sm">按快照只读检索，不碰运行中的系统</span></div>
        <div className="card" style={{ maxWidth: 640, margin: '40px auto' }}>
          <Empty icon="db" title="还没有证据快照" actions={<Button kind="primary" icon="scan" onClick={() => navigate(pagePath('snapshots', pid))}>去采集一次</Button>}>
            证据浏览器只读快照。先在「采集与快照」里对已授权的数据源采集一次快照。
          </Empty>
        </div>
      </div>
    );
  }

  return (
    <div className="evd-full">
      <div className="row">
        <b className="lg">证据浏览器</b><EvdPlg /><span className="muted sm">按快照只读检索，不碰运行中的系统</span>
        <span className="sp" />
        <Select icon="db" w="356px" value={snapshot} options={snapOptions} onChange={(id) => setSnapshot(id)} />
        <Button size="sm" icon="split" disabled={!snapshot} onClick={() => navigate(pagePath('snapshots', pid), { to: snapshot })}>对比快照</Button>
      </div>
      <div className="evd-sg" style={maxed ? { gridTemplateColumns: 'minmax(0,1fr)' } : undefined}>
        {maxed ? null : (
          <div className="card">
            <div style={{ padding: '12px 12px 8px' }}>
              <Inp icon="search" value={q} placeholder="evidence.search：表、字段、注释…" onChange={(e) => setQ(e.target.value)} after={q ? <span style={{ cursor: 'pointer', display: 'inline-flex', color: 'var(--muted)' }} title="清空" onClick={() => setQ('')}><Icon name="x" size={12} /></span> : null} />
              <div className="row mt8"><Seg items={SEARCH_TYPES} value={qType} onChange={setQType} /><span className="sp" /><span className="xs faint">{treeTotal(tree)} 实体</span></div>
            </div>
            <div className="scroll evd-tree" style={{ flex: 1, padding: '0 8px 10px' }}>
              {q.trim() ? <Hits hits={hits} sel={sel} onPick={pick} /> : tree ? <Tree tree={tree} sel={sel} collapsed={collapsed} onToggle={toggleDomain} onPick={(a) => pick(a)} /> : <div className="sm muted" style={{ padding: 6 }}>正在读取分层摘要…</div>}
            </div>
          </div>
        )}
        <div className="card">
          <div className="row" style={{ padding: '12px 14px', borderBottom: '1px solid var(--line)' }}>
            <b>ER 视图</b><span className="muted sm">{focusTable ? `${focusTable.label || focusTable.name}相关 ${er.tables.length} 张表` : ''}</span><span className="sp" />
            <span className="row g4 xs muted"><svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke="#64748B" strokeWidth="1.6" /></svg>declared 外键</span>
            <span className="row g4 xs muted"><svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke="#64748B" strokeWidth="1.8" strokeDasharray="1.5 4" strokeLinecap="round" /></svg>observed 取值重合</span>
            <span className="row g4 xs muted"><svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke="#64748B" strokeWidth="1.6" strokeDasharray="6 4" /></svg>inferred 命名推断</span>
          </div>
          <div style={{ flex: 1, minHeight: 0, padding: 12, position: 'relative' }}>
            <div className="evp-er-cv">
              {er ? <ErView er={er} focus={er.focus} selected={selTable} selField={selField} domainOf={domainOf} zoom={zoom} onPick={(a) => pick(a)} /> : <div className="sm muted" style={{ padding: 20 }}>正在排版…</div>}
            </div>
            <div className="row g4" style={{ position: 'absolute', right: 14, top: 12 }}>
              <Button size="sm" icon="zoomout" title="缩小" disabled={zi <= 0} onClick={() => setZoom(ZOOMS[Math.max(0, zi - 1)])} />
              <Button size="sm" icon="zoomin" title="放大" disabled={zi >= ZOOMS.length - 1} onClick={() => setZoom(ZOOMS[Math.min(ZOOMS.length - 1, zi + 1)])} />
              <Button size="sm" icon="maximize" title={maxed ? '还原三栏' : '只看 ER 视图'} onClick={() => setMaxed((v) => !v)} />
            </div>
            {selTable ? <div className="xs muted" style={{ position: 'absolute', left: 16, top: 16 }}>蓝色：当前选中 erp.{selName}{selField ? `.${selField}` : ''} 及其相邻关系</div> : null}
          </div>
        </div>
        {maxed ? null : (
          <div className="card">
            {record ? <Detail record={record} sel={sel} used={used} lineage={lineage} pid={pid} onField={(f) => setSel((s) => ({ ...s, field: f }))} onPick={(a) => pick(a)} />
              : recErr ? (
                <Empty icon="alert" title={recErr.code === 'anchor_not_found' ? '这份快照里没有这条记录' : '读不出记录详情'}>
                  {recErr.code === 'anchor_not_found' ? `${anchorQ || selTable} 不在快照 ${snapshot} 里，换一份快照看看。` : errorText(recErr)}
                </Empty>
              ) : <div className="sm muted" style={{ padding: 16 }}>{selTable ? '正在读取记录…' : '在左栏或 ER 视图里选一张表。'}</div>}
          </div>
        )}
      </div>
    </div>
  );
}

export default function SchemaPage({ route }) {
  return (
    <ShellP active="schema" flush>
      <EvidenceGate page="schema"><SchemaBody route={route} /></EvidenceGate>
    </ShellP>
  );
}
