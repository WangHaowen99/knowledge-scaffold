// 报告（设计稿 reports）：报告列表（RP-01）、详情（RP-02）与逐条审阅（RP-03）、报告原文（RP-04）、
// 发给助手修复（RP-05）、重新校验（RP-06）、交给你裁决（Question，S-11 回答）；资料分析报告带核验（R-09）
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { ShellP } from '../../shell/Shell.jsx';
import { Button, Card, Chip, Ev, Icon, KV, Note, Prog, Ring, Seg, St, cx } from '../../ui/index.jsx';
import { inlineTokens } from '../workbench/md.js';
import { anchorChipLabel } from '../studio/studioModel.js';
import { Doc, useAttachmentAnswer } from '../evaluation/EvaluationDoc.jsx';
import { findHeadingIndex } from '../../lib/sourceLocation.js';
import '../../styles/evaluation.css';
import { SourceText, useKnownMaterials } from '../studio/SourceChips.jsx';
import '../../styles/reports.css';
import {
  ITEM_STATUS, LINK_STATS, PURPOSE_LABEL, PURPOSE_TONE, RECHECK_KINDS, fmtStamp, infoPairs, linkKind,
  locateTarget, materialCounts, progressView, repairView,
} from './reportModel.js';

const FIX_TOAST = '已发给助手';

// 正文：[[条目]] 画成条目链接，[^n] 画成出处小标签（悬停看原文）
function RichText({ text, anchors }) {
  const map = Object.fromEntries((anchors || []).map((a) => [a.n, a.anchor]));
  return inlineTokens(text).map((t, i) => {
    if (t.t === 'link') return <span key={i} className="wl">{t.v}</span>;
    if (t.t === 'cite') return map[t.v] ? <Ev key={i} anchor={map[t.v]} label={anchorChipLabel(map[t.v])} /> : null;
    return <span key={i}>{t.v}</span>;
  });
}

function ItemStatus({ item }) {
  const s = ITEM_STATUS[item.status] || ITEM_STATUS.pending;
  if (s.running) return <St k="running" sm label={s.running} />;
  return <Chip tone={s.chip[1]} sm icon={s.chip[2]}>{s.chip[0]}</Chip>;
}

function ItemRow({ item, link, onLocate, onFix }) {
  // 跳过的也算处理过（淡显），但还能再发给助手修
  const done = item.status === 'fixed' || item.status === 'skipped';
  const [k, tone] = link ? linkKind(item) : item.level === 'error' ? ['错误', 'red'] : ['警告', 'amber'];
  return (
    <div className={cx('rp-row', link ? 'warn' : item.level === 'error' ? 'err' : 'warn', done && 'done')}>
      <span><Chip tone={tone} sm>{k}</Chip></span>
      <div style={{ minWidth: 0 }}>
        <div className="sm b"><RichText text={item.text} /></div>
        <div className="xs muted mt4 row g4"><Icon name="target" size={12} /><span className="ell">{item.location?.label}</span></div>
      </div>
      <div className="col" style={{ gap: 3, alignItems: 'flex-start' }}>
        <ItemStatus item={item} />
        <span className="xs faint">{item.status_note}</span>
      </div>
      <div className="row g4" style={{ justifyContent: 'flex-end' }}>
        <Button size="xs" icon="target" disabled={!item.location?.target} onClick={() => onLocate(item)}>定位</Button>
        {item.status === 'fixed' ? null : <Button size="xs" kind="soft" icon="sparkle" disabled={item.status === 'fixing'} onClick={() => onFix(item)}>发给助手</Button>}
      </div>
    </div>
  );
}

// 交给你裁决：修了 2 轮还没收敛的那一项（Question kind=expert，S-11 回答）
function Arbitration({ q, onAnswered }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [pick, setPick] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setPick(null); }, [q.question_id]);
  const answer = async () => {
    setBusy(true);
    try {
      await api.answerQuestion(pid, q.conversation_id, q.question_id, { option_id: pick });
      toast('已提交裁决，助手开始修改', 'hand');
      onAnswered();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="wb-dc mt12" style={{ marginTop: 12 }}>
      <h4><Icon name="hand" size={14} />{q.title}</h4>
      <div className="sm muted mt4"><RichText text={q.background_md} anchors={q.anchors} /></div>
      <div className="opts">
        {q.options.map((o) => (
          <div key={o.id} className={cx('opt', pick === o.id && 'on')} onClick={() => setPick(o.id)}>
            <span className="k">{o.key}</span>
            <div className="grow"><div className="t">{o.label}</div>{o.consequence ? <div className="c">{o.consequence}</div> : null}</div>
          </div>
        ))}
      </div>
      <div className="row mt12">
        <span className="sp" />
        <Button size="sm" kind="primary" disabled={!pick || busy} onClick={answer}>确认裁决</Button>
      </div>
    </div>
  );
}

function RepairCard({ repair, items, arbitration, onAnswered }) {
  const v = repairView(repair, items);
  if (!v && !arbitration) return null;
  const max = Math.max(1, ...(v?.bars || []).map((b) => b.count));
  return (
    <div className="card pad" style={{ background: 'linear-gradient(180deg,#F7F9FF,#fff)', borderColor: 'var(--i100)' }}>
      {v ? (
        <div className="row g16">
          <div className="rp-round">
            {v.bars.map((b, i) => (
              <div key={i} className={cx('bar', b.cur && 'cur')}>
                <b className="num" style={{ color: 'var(--ink)' }}>{b.count}</b>
                <i style={{ height: Math.round((b.count / max) * 48) + 6 }} />
                {b.label}
              </div>
            ))}
          </div>
          <div className="grow">
            <div className="row"><b>{v.title}</b>{v.exhausted ? <Chip tone="indigo" sm>已到上限</Chip> : null}</div>
            {v.note ? <div className="sm muted mt4">{v.note}</div> : null}
          </div>
        </div>
      ) : null}
      {arbitration ? <Arbitration q={arbitration} onAnswered={onAnswered} /> : null}
    </div>
  );
}

// 评测服务写回的报告：原文按 Markdown 渲染，「完整回答」链接弹出原始最终答复（develop 的报告预览）
const EVAL_REPORT = /^reports\/eval-[^/]+\.md$/;

function RawPane({ raw, section, answers = null, back = null }) {
  const bodyRef = useRef(null);
  // 原文里的资料路径（带锚点）、报告小节显示成芯片（develop 文件预览），点开后「返回 <报告文件名>」
  const known = useKnownMaterials();
  const origin = raw?.path ? raw.path.split('/').pop() : '';
  const lines = raw ? raw.content_md.split('\n') : [];
  // 从芯片带小节打开（develop FilePreviewBody）：标题全文相同优先，其次包含小节名；找不到提示并显示开头
  const heads = lines.map((line, i) => [/^#{1,3}\s+(.*)$/.exec(line.trim())?.[1]?.trim(), i]).filter(([t]) => t != null);
  const found = section ? findHeadingIndex(heads.map(([t]) => t), section) : -1;
  const at = found >= 0 ? heads[found][1] : -1;
  useLayoutEffect(() => { bodyRef.current?.querySelector('.rp-at')?.scrollIntoView({ block: 'start' }); }, [raw, at]);
  useEffect(() => {
    if (raw && section && at < 0) toast(`小节「${section}」在文档里已不存在，显示文档开头`, 'alert');
  }, [raw, section, at]);
  return (
    <div className="bd">
      <div className="row mb12">
        {back}
        <span className="sp" />
        <Button size="xs" icon="copy" disabled={!raw} onClick={async () => { try { await navigator.clipboard.writeText(raw.content_md); toast('已复制', 'copy'); } catch { toast('复制失败，请手动选择', 'alert'); } }}>复制</Button>
      </div>
      {raw && answers && at < 0 ? (
        <Doc content={raw.content_md} onOpenAnswer={answers.open} />
      ) : raw ? (
        <div className="code" ref={bodyRef}>
          {at < 0 ? <SourceText text={raw.content_md} origin={origin} known={known} />
            : lines.map((line, i) => <div key={i} className={i === at ? 'rp-at' : undefined}>{line ? <SourceText text={line} origin={origin} known={known} /> : '\u00a0'}</div>)}
        </div>
      ) : <div className="xs muted">正在读取…</div>}
      {raw?.truncated ? <div className="xs muted mt8">原文太长，只显示了前一部分。</div> : null}
    </div>
  );
}

function ResearchBody({ report }) {
  const x = report.extras || {};
  const found = x.found || {};
  const ver = x.verification || {};
  const allVerified = ver.total != null && !ver.unverifiable && !ver.pending;
  return (
    <div className="bd">
      <div className="row g16">
        <div style={{ position: 'relative' }}>
          <Ring p={x.read_pct ?? 0} r={26} />
          <b style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', fontSize: 13 }}>{x.read_pct ?? 0}%</b>
        </div>
        <div className="grow">
          <div className="b">{x.read_pct === 100 ? `${x.total} 份资料全部读完` : `${x.total} 份资料读了 ${x.read_pct ?? 0}%`}</div>
          <div className="xs muted mt4">识别业务项 {found.items ?? 0} · 术语 {found.terms ?? 0} · 规则候选 {found.rule_candidates ?? 0}{allVerified ? ' · 来源全部可核验' : ''}</div>
        </div>
      </div>
      <div className="mt16" style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 10 }}>
        {[
          ['候选', `对象 ${x.candidates?.objects ?? 0} · 规则 ${x.candidates?.rules ?? 0}`],
          ['待答问题', `${x.open_questions ?? 0} 个`],
          ['资料矛盾', x.contradictions?.total ? `${x.contradictions.total} 处 · ${x.contradictions.resolved >= x.contradictions.total ? '已裁决' : `${x.contradictions.total - (x.contradictions.resolved || 0)} 处待裁决`}` : '没有'],
        ].map(([a, b]) => (
          <div key={a} style={{ background: 'var(--panel2)', borderRadius: 10, padding: '9px 12px' }}>
            <div className="xs muted">{a}</div>
            <div className="sm b mt4">{b}</div>
          </div>
        ))}
      </div>
      <div className="sec-t mt16">资料用途</div>
      <div className="row wrap" style={{ gap: 6 }}>
        {(x.purposes || []).map((p) => <Chip key={p.value} tone={PURPOSE_TONE[p.value] || 'gray'} sm>{PURPOSE_LABEL[p.value] || p.value} {p.count}</Chip>)}
      </div>
      <div className="sec-t mt16">逐份结果 · {(x.items || []).length}</div>
      {(x.items || []).map((m) => (
        <div key={m.path} className="ov-li sm">
          <span style={{ color: 'var(--blue)' }}><Icon name={m.kind === 'xls' || m.kind === 'csv' ? 'table' : 'filetext'} size={14} /></span>
          <span className="grow ell b" style={{ fontWeight: 600 }}>{m.label || m.name}</span>
          <span className="xs muted" style={{ width: 150 }}>{materialCounts(m.counts)}</span>
          <Chip tone="line" sm>{PURPOSE_LABEL[m.purpose] || m.purpose}</Chip>
          <span className="num xs muted" style={{ width: 38, textAlign: 'right' }}>{m.read_pct}%</span>
        </div>
      ))}
      {(x.problems || []).map((p) => (
        <div key={p.id} className="row mt12 xs muted"><Icon name="quote" size={12} /><span><RichText text={p.display_md || p.text_md} /></span></div>
      ))}
    </div>
  );
}

export default function ReportsPage({ route }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [list, setList] = useState(null);
  const [report, setReport] = useState(null);
  const [items, setItems] = useState(null);
  const [raw, setRaw] = useState(null);
  const [pane, setPane] = useState('items');
  const [verification, setVerification] = useState(null);
  const [tick, setTick] = useState(0);
  const fail = useCallback((e) => toast(errorText(e), 'alert'), []);
  const attachment = useAttachmentAnswer(api, pid);

  useEffect(() => { api.reports(pid).then((r) => setList(r.items || [])).catch(fail); }, [api, pid, tick, fail]);
  const selectedId = route.query?.report || list?.[0]?.id || '';
  const select = (id) => navigate(pagePath('reports', pid), { report: id });

  useEffect(() => {
    if (!selectedId) return undefined;
    let alive = true;
    Promise.all([api.report(pid, selectedId), api.reportItems(pid, selectedId)])
      .then(([r, it]) => { if (alive) { setReport(r); setItems(it); } })
      .catch((e) => { if (alive) fail(e); });
    return () => { alive = false; };
  }, [api, pid, selectedId, tick, fail]);
  // 规则「来源」里的报告小节跳来（?section=）：直接看原文并定位到那一节
  const section = route.query?.section || '';
  useEffect(() => { setRaw(null); }, [selectedId]);
  useEffect(() => { setPane(section ? 'raw' : 'items'); }, [selectedId, section]);
  useEffect(() => {
    if (pane !== 'raw' || !selectedId) return;
    api.reportRaw(pid, selectedId).then(setRaw).catch(fail);
  }, [api, pid, pane, selectedId, tick, fail]);
  // 资料分析报告：核验结果以 R-09 为准（拿不到时用报告里的摘要）
  useEffect(() => {
    if (report?.type !== 'research') return;
    api.materialVerification(pid).then(setVerification).catch(() => setVerification(null));
  }, [api, pid, report?.type]);

  const current = report && report.id === selectedId ? report : null;
  const reload = () => setTick((t) => t + 1);
  const locate = (item) => {
    const t = locateTarget(item.location);
    if (t) navigate(pagePath(t.page, pid), t.query);
  };
  const fixOne = async (item) => {
    try {
      await api.fixReport(pid, current.id, { item_ids: [item.id] });
      toast(FIX_TOAST, 'sparkle');
      reload();
    } catch (e) { fail(e); }
  };
  const fixAll = async () => {
    try {
      const r = await api.fixReport(pid, current.id, {});
      toast(FIX_TOAST, 'sparkle');
      navigate(pagePath('wb-l2', pid), r.conversation_id ? { c: r.conversation_id } : undefined);
    } catch (e) { fail(e); }
  };
  const recheck = async () => {
    const kind = RECHECK_KINDS.includes(current?.type) ? current.type : 'rule';
    try {
      const r = await api.recheckReports(pid, { kind, mode: 'regression' });
      if (r?.report) { toast('链接检查已完成', 'check'); reload(); } else toast('已开始重新校验', 'refresh');
    } catch (e) { fail(e); }
  };

  const its = items?.items || [];
  const counts = items?.counts || {};
  const prog = progressView(current);
  const isLink = current?.type === 'link';
  const isResearch = current?.type === 'research';

  // 从条目的出处芯片点进来（?from=）：「返回 <文档名>」回到那里（develop 的 onBackToOrigin）
  const back = section && route.query?.from ? (
    <Button size="sm" kind="ghost" icon="arrowl" onClick={() => { window.location.hash = route.query.from; }}>返回 {route.query.fromLabel || ''}</Button>
  ) : null;
  let header = null;
  let body = null;
  if (current) {
    const actions = isResearch ? (
      <>
        {verification || current.extras?.verification ? <Chip tone="blue" icon="check" sm>已核验</Chip> : null}
        <Button size="sm" icon="map" onClick={() => navigate(pagePath('wb-materials', pid), { dialog: 'material_map' })}>看资料地图</Button>
      </>
    ) : (
      <>
        {isLink ? null : <Seg items={[['items', '逐条审阅'], ['raw', '报告原文']]} value={pane} onChange={setPane} />}
        <Button size="sm" kind="primary" icon="sparkle" disabled={!its.some((x) => x.status !== 'fixed')} onClick={fixAll}>全部发给助手</Button>
      </>
    );
    header = (
      <div className="card-h" style={{ paddingBottom: 12 }}>
        <div>
          <h3 style={{ fontSize: 16 }}>{current.name}</h3>
        </div>
        <span className="sp" />
        {actions}
      </div>
    );
    // 资料分析报告平时是结构化视图；从规则「来源」的小节跳来时看原文并定位（再点列表里的报告回到结构化视图）
    if (isResearch) body = section ? <RawPane raw={raw} section={section} back={back} /> : <ResearchBody report={current} />;
    else if (pane === 'raw' && !isLink) body = <RawPane raw={raw} section={section} back={back} answers={EVAL_REPORT.test(current?.path || '') ? attachment : null} />;
    else {
      body = (
        <div className="bd">
          {isLink ? (
            <div className="row g12">
              {LINK_STATS.map(([k, t]) => (
                <div key={k} className="stat grow" style={{ padding: '12px 14px' }}>
                  <div className="lb">{t}</div><div className="v" style={{ fontSize: 22 }}>{current.extras?.[k] ?? 0}</div>
                </div>
              ))}
            </div>
          ) : <RepairCard repair={current.repair} items={its} arbitration={current.arbitration} onAnswered={reload} />}
          <div className={cx('sec-t', (isLink || current.repair || current.arbitration) && 'mt16')}>
            条目 · {its.length}
            <span className="sp" />
            {isLink ? null : (
              <span className="row g4">
                {counts.error ? <Chip tone="red" sm>错误 {counts.error}</Chip> : null}
                {counts.warning ? <Chip tone="amber" sm>警告 {counts.warning}</Chip> : null}
              </span>
            )}
          </div>
          {its.map((it) => <ItemRow key={it.id} item={it} link={isLink} onLocate={locate} onFix={fixOne} />)}
          {items && !its.length ? <div className="xs muted">没有需要处理的条目</div> : null}
        </div>
      );
    }
  }

  return (
    <ShellP active="reports" bodyStyle={{ padding: '14px 18px 56px' }}>
      <div className="rp">
        <aside className="rp-list card">
          <div className="card-h">
            <h3>报告</h3>
            <span className="sub">{list ? `${list.length} 份` : ''}</span>
            <span className="sp" />
            <Button size="xs" icon="refresh" kind="soft" disabled={!current} onClick={recheck}>重新校验</Button>
          </div>
          <div style={{ padding: 10, flex: 1, overflow: 'auto' }}>
            {(list || []).map((r) => (
              <div key={r.id} className={cx('rp-it', r.id === selectedId && 'on')} onClick={() => select(r.id)}>
                <div className="row"><b className="sm grow ell">{r.name.split(' · ')[0]}</b></div>
                <div className="row mt4 xs muted"><span className="num">{fmtStamp(r.at)}</span><span className="sp" /><span>{r.summary}</span></div>
              </div>
            ))}
            {list && !list.length ? <div className="rp-empty">还没有报告</div> : null}
          </div>
        </aside>
        <section className="rp-c card">
          {header}
          {body || <div className="rp-empty">{list && !list.length ? '选一份报告查看' : '正在读取…'}</div>}
        </section>
        <aside className="rp-side">
          <Card title="这份报告" icon="info"><KV w="72px" pairs={infoPairs(current)} /></Card>
          <Card title="处理进度" icon="activity">
            <div className="row"><span className="grow sm">{prog.text}</span><span className="num xs muted">{prog.pct}%</span></div>
            <Prog p={prog.pct} />
          </Card>
        </aside>
      </div>
      {attachment.modal}
    </ShellP>
  );
}
