// 资料地图弹窗（设计稿 wb-materials #enMapM）：每份资料的用途、读取覆盖、候选实时流、问题区
// R-07 资料地图、R-08 候选、R-03 改用途、R-10 问题转待答、R-06 原文、P-05 联网 / 仅本地、S-20 继续读取 / 追加分析
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { navigate, pagePath } from '../../../app/router.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import {
  Button, Chip, Ev, Icon, Modal, Prog, Seg, cx, useOutside,
} from '../../../ui/index.jsx';
import { AnchorText, MatIcon, pageCommand, useMaterialUpload } from './parts.jsx';
import {
  PURPOSES, countPairs, parseAnchor, purposeLabel, readState,
} from './model.js';
import { VerificationDrawer } from './VerificationDrawer.jsx';
import { useSheetScope } from '../../resources/SheetScope.jsx';
import { isSchemaTable } from '../../resources/sheetScopeModel.js';
import '../../../styles/materials.css';

const CAND_KIND = { object: ['对象', 'blue'], rule: ['规则', 'amber'], term: ['术语', 'teal'] };
// 候选实时流：打开时露最新 3 条，新冒出来的往上顶，最多留 6 条（设计稿同样的节奏）
const STREAM_FIRST = 3;
const STREAM_SIZE = 6;

function ReadChip({ item }) {
  const st = readState(item);
  if (st === 'done') return <Chip tone="blue" sm icon="check">读完</Chip>;
  if (st === 'reading') return <span className="chip st-run sm"><span className="spin" />读取中</span>;
  if (st === 'queued') return <Chip tone="gray" sm>排队中</Chip>;
  return <Chip tone="amber" sm>{item.read_pct}%</Chip>;
}

function PurposeMenu({ value, onPick, onClose }) {
  const ref = useRef(null);
  useOutside(ref, onClose);
  return (
    <div className="menu en-rmenu" ref={ref} onClick={(e) => e.stopPropagation()}>
      {PURPOSES.map((p) => (
        <div key={p.value} className="mi" onClick={() => onPick(p.value)}>
          {p.value === value ? <Icon name="check" size={14} /> : <span style={{ width: 14 }} />}
          <span>{p.label}</span>
        </div>
      ))}
    </div>
  );
}

function MaterialCard({
  item, menuOpen, onMenu, onCloseMenu, onPurpose, onContinue,
}) {
  const st = readState(item);
  const ai = item.purpose === 'auto';
  return (
    <div className={cx('en-mc', st === 'reading' && 'run', item.blocked && 'blk')}>
      <div className="row g12">
        <MatIcon kind={item.kind} boxed />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="nm ell" title={item.label || item.name}>{item.label || item.name}</div>
          <div className="mt4">
            <span className={cx('role', ai && 'ai')} onMouseDown={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onMenu(); }}>
              {ai ? <Icon name="sparkle" size={12} /> : null}{purposeLabel(item.purpose)}<Icon name="chevd" size={12} />
            </span>
          </div>
        </div>
        <ReadChip item={item} />
      </div>
      <div className="mt8 row g8">
        <div className="grow"><Prog p={item.read_pct} thin tone={item.blocked ? 'amber' : undefined} /></div>
        <span className="xs muted num" style={{ width: 34, textAlign: 'right' }}>{item.read_pct}%</span>
      </div>
      {st === 'reading' && item.position ? <div className="xs muted mt4">{item.position}</div> : null}
      {st === 'queued' ? <div className="xs muted mt4">排队中{item.queue_reason ? ` · ${item.queue_reason}` : ''}</div> : null}
      {item.contradiction ? <div className="xs mt4" style={{ color: 'var(--amber)' }}><Icon name="alert" size={12} /> {item.contradiction}</div> : null}
      <div className="cnt">{countPairs(item.counts).map(([k, v]) => <span key={k}>{k} <b>{v}</b></span>)}</div>
      {item.blocked ? (
        <div className="note amber mt8" style={{ padding: '7px 9px', fontSize: 11.8 }}>
          <Icon name="lock" />
          <div className="grow"><b>没读完不能进入下一步。</b>{item.blocked.reason}。<a className="link" onClick={onContinue}>继续读取</a></div>
        </div>
      ) : null}
      {menuOpen ? <PurposeMenu value={item.purpose} onPick={onPurpose} onClose={onCloseMenu} /> : null}
    </div>
  );
}

// 并排看原文：问题涉及的两处出处（R-06，按锚点定位）
function SideBySide({ problem, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [docs, setDocs] = useState(null);
  useEffect(() => {
    let alive = true;
    Promise.all((problem.anchors || []).slice(0, 2).map(async (a) => {
      const { path, fragment } = parseAnchor(a.anchor);
      try {
        const r = await api.resourcePreview(pid, path, fragment);
        return { a, path, fragment, blocks: r.blocks || [] };
      } catch (e) {
        return { a, path, fragment, error: errorText(e), blocks: [] };
      }
    })).then((x) => { if (alive) setDocs(x); });
    return () => { alive = false; };
  }, [api, pid, problem]);
  return (
    <Modal open onClose={onClose} icon="filetext" title="并排看原文" sub={<AnchorText text={problem.display_md || problem.text_md} anchors={problem.anchors} />} w={960} foot={<Button onClick={onClose}>关闭</Button>}>
      {!docs ? <div className="muted sm">正在读取原文…</div> : (
        <div className="en-side">
          {docs.map((d) => (
            <div className="en-doc" key={d.a.anchor}>
              <div className="en-doc-h"><MatIcon kind={/xls|csv/.test(d.path) ? 'xls' : 'doc'} size={14} /><b className="ell grow">{d.path.split('/').pop()}</b><Ev anchor={d.a.anchor} label={d.a.label} /></div>
              <div className="en-doc-b">
                {d.error ? <div className="note red"><Icon name="alert" /><div className="grow">{d.error}</div></div> : null}
                {d.blocks.map((b) => (
                  <div key={b.anchor} className={cx(b.kind === 'heading' && 'hd', b.anchor === d.fragment && 'on')}>
                    <Marked text={b.text} highlights={b.highlights} />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function Marked({ text, highlights }) {
  if (!highlights?.length) return text;
  const out = [];
  let pos = 0;
  highlights.forEach((h, i) => {
    if (h.start > pos) out.push(text.slice(pos, h.start));
    out.push(<mark key={i}>{text.slice(h.start, h.end)}</mark>);
    pos = h.end;
  });
  if (pos < text.length) out.push(text.slice(pos));
  return out;
}

function VerifyChip({ v, onClick }) {
  if (!v || !v.checked_at) return <span className="chip gray en-map-chip"><Icon name="shieldok" />待核验</span>;
  if (v.unverifiable) return <span className="chip amber en-map-chip" onClick={onClick}><Icon name="shieldok" />已自动核验 · {v.unverifiable} 条来源无法核验<Icon name="chev" /></span>;
  return <span className="chip teal en-map-chip" onClick={onClick}><Icon name="shieldok" />已自动核验 · {v.pending ? `${v.pending} 条待核验` : '全部通过'}<Icon name="chev" /></span>;
}

export function MaterialMapDialog({ onClose, initialVerify = false }) {
  const { api } = useApp();
  const { pid, subscribe, setMeta } = useProject();
  const [map, setMap] = useState(null);
  const [error, setError] = useState(null);
  const [cands, setCands] = useState({ total: 0, items: [] });
  const [menu, setMenu] = useState(null);
  const [verify, setVerify] = useState(initialVerify);
  const [side, setSide] = useState(null);
  const up = useMaterialUpload(pid);

  const load = useCallback(() => api.materialMap(pid).then(setMap).catch(setError), [api, pid]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    let alive = true;
    api.materialCandidates(pid).then((r) => {
      if (!alive) return;
      const first = (r.items || []).slice(-STREAM_FIRST).reverse();
      // 响应晚于项目流推来的新候选时，保留已推来的，不整体覆盖
      setCands((c) => ({
        total: Math.max(r.total || 0, c.total),
        items: [...c.items, ...first.filter((x) => !c.items.some((y) => y.id === x.id))].slice(0, STREAM_SIZE),
      }));
    }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid]);
  // 项目流：资料读到哪（EP-11）、新冒出的候选（EP-12）
  useEffect(() => subscribe((env) => {
    const d = env.data || {};
    if (env.type === 'materials.updated') {
      setMap((m) => {
        if (!m) return m;
        const items = [...m.items];
        (d.items || []).forEach((x) => {
          const i = items.findIndex((y) => y.path === x.path);
          if (i >= 0) items[i] = { ...items[i], ...x }; else items.push(x);
        });
        return { ...m, items, total: items.length, read_pct: d.read_pct ?? m.read_pct };
      });
      if (d.problems_count !== undefined) load();
    }
    if (env.type === 'materials.candidate' && d.candidate) {
      setCands((c) => ({ total: d.total ?? c.total + 1, items: [d.candidate, ...c.items.filter((x) => x.id !== d.candidate.id)].slice(0, STREAM_SIZE) }));
    }
  }), [subscribe, load]);

  // 表格改成数据 Schema 与资料区同一条路：先预检表头，有问题弹窗选 Sheet 范围
  const sheetScope = useSheetScope(pid, (r) => {
    setMap((m) => ({ ...m, items: m.items.map((x) => (x.path === r.material.path ? { ...x, purpose: 'schema', purpose_auto: false } : x)) }));
  });
  const setPurpose = async (item, purpose) => {
    setMenu(null);
    if (purpose === item.purpose) return;
    if (purpose === 'schema' && isSchemaTable(item.name)) { sheetScope.choose({ path: item.path, name: item.name }); return; }
    try {
      await api.setResourcePurpose(pid, { path: item.path, purpose });
      setMap((m) => ({ ...m, items: m.items.map((x) => (x.path === item.path ? { ...x, purpose, purpose_auto: false } : x)) }));
      toast(`用途已改为：${purposeLabel(purpose)}`, 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const setMode = async (mode) => {
    if (!map || mode === map.analysis_mode) return;
    if (mode === 'web' && !map.web_available) { toast('「联网调研」插件未启用', 'alert'); return; }
    try {
      const meta = await api.patchProject(pid, { research_mode: mode });
      setMeta?.(meta);
      setMap((m) => ({ ...m, analysis_mode: mode }));
      toast(mode === 'web' ? '已改为联网资料分析' : '已改为仅本地资料', 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const promote = async (p, as) => {
    try {
      await api.promoteMaterialProblem(pid, p.id, { as });
      setMap((m) => ({ ...m, problems: m.problems.map((x) => (x.id === p.id ? { ...x, state: 'promoted' } : x)) }));
      toast('已加入待答问题', 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const continueRead = (item) => pageCommand(api, pid, {
    command: '/ontology-research', args: { path: item.path, parts: item.blocked?.unread_parts || [] },
    text: `继续读取「${item.name}」：${(item.blocked?.unread_parts || []).join('、') || '没读完的部分'}`, done: '已发给助手',
  });
  const appendAnalysis = () => pageCommand(api, pid, { command: '/ontology-research', args: { mode: 'append' }, text: '追加分析', done: '已开始追加分析' });

  const reading = !!map?.items.some((x) => ['reading', 'queued'].includes(x.status));
  const problems = map?.problems || [];
  return (
    <>
      <Modal
        open onClose={onClose} icon="map" title="资料地图" w={1180}
        foot={<Button onClick={onClose}>关闭</Button>}
      >
        {error ? <div className="note red"><Icon name="alert" /><div className="grow">{errorText(error)}</div></div> : null}
        {!map && !error ? <div className="muted sm">正在读取资料地图…</div> : null}
        {map ? (
          <>
            <div className="row g8">
              <Chip tone="line">{map.total} 份资料 · 已读 {map.read_pct}%</Chip>
              <span className="sp" />
              <Seg items={[['web', '联网资料分析'], ['local', '仅本地资料']]} value={map.analysis_mode} onChange={setMode} />
              <Button size="sm" icon="plus" onClick={appendAnalysis}>追加分析</Button>
              <VerifyChip v={map.verification} onClick={() => setVerify(true)} />
            </div>
            <div className="en-mm mt12">
              <div className="en-mc-g">
                {map.items.map((x) => (
                  <MaterialCard
                    key={x.path} item={x} menuOpen={menu === x.path}
                    onMenu={() => setMenu((v) => (v === x.path ? null : x.path))} onCloseMenu={() => setMenu(null)}
                    onPurpose={(p) => setPurpose(x, p)} onContinue={() => continueRead(x)}
                  />
                ))}
                <div className="en-mt add" style={{ minHeight: 120, borderRadius: 14 }} onClick={up.pick}>
                  {up.busy ? <span className="spin" /> : <Icon name="plus" size={14} />}{up.busy ? '正在上传…' : '追加资料'}
                </div>
                {up.input}
              </div>
              <div>
                <div className="sec-t">
                  <Icon name="sparkle" size={14} />候选实时流 {reading ? <i className="en-live-dot" /> : null}
                  <span className="sp" /><span className="faint xs">{cands.total} 条</span>
                </div>
                <div className="en-stream">
                  {cands.items.length ? cands.items.map((c) => {
                    const [label, tone] = CAND_KIND[c.kind] || ['条目', 'gray'];
                    return (
                      <div className="en-si" key={c.id}>
                        <Chip tone={tone} sm>{label}</Chip>
                        <div className="grow"><div style={{ fontWeight: 600 }}>{c.name}</div><div className="mt4"><Ev anchor={c.anchor} label={c.anchor_label} /></div></div>
                      </div>
                    );
                  }) : <div className="xs muted">还没有候选</div>}
                </div>
                <div className="sec-t" style={{ marginTop: 16 }}>
                  <Icon name="alert" size={14} />问题区 <span className="faint">{problems.length}</span>
                </div>
                {problems.length ? problems.map((p) => {
                  const done = p.state !== 'open';
                  return (
                    <div className="en-iss" key={p.id}>
                      <div className="tp" style={{ color: p.kind === 'contradiction' ? 'var(--amber)' : 'var(--indigo)' }}>{p.kind === 'contradiction' ? '资料矛盾' : '疑似缺失'}</div>
                      <div className="sm mt4"><AnchorText text={p.display_md || p.text_md} anchors={p.anchors} /></div>
                      <div className="row mt8">
                        {p.kind === 'contradiction' ? (
                          <>
                            <Button size="xs" kind="soft" disabled={done} onClick={() => promote(p, 'blueprint_question')}>{done ? '已记为待答问题' : '记为待答问题'}</Button>
                            <Button size="xs" kind="ghost" onClick={() => setSide(p)}>并排看原文</Button>
                          </>
                        ) : (
                          <>
                            <Button size="xs" kind="soft" disabled={done} onClick={() => promote(p, 'ask_me')}>{done ? '已加入待答问题' : '向我确认'}</Button>
                            <Button size="xs" kind="ghost" onClick={() => navigate(pagePath('resources', pid))}>补传资料</Button>
                          </>
                        )}
                      </div>
                    </div>
                  );
                }) : <div className="xs muted">没有发现资料矛盾或疑似缺失</div>}
              </div>
            </div>
          </>
        ) : null}
      </Modal>
      {verify ? <VerificationDrawer onClose={() => setVerify(false)} /> : null}
      {side ? <SideBySide problem={side} onClose={() => setSide(null)} /> : null}
      {sheetScope.dialog}
    </>
  );
}
