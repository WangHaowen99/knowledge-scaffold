// 待处理与复核（设计稿 inbox）：左栏列表与筛选，中间按类型的详情，右栏影响范围、今天的进度。
// 接口：IN-01～IN-04；高置信合并项里单独改判走 AB-11 / AB-12；「运行结果」读 AB-16、回退走 AB-17。
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { evidenceInProject } from '../../state/evidence.js';
import { multiSlice, sliceName, unitTitle } from '../../state/flow.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { ShellP } from '../../shell/Shell.jsx';
import {
  Button, Card, Chip, Empty, Ev, Icon, KV, Kbd, Menu, Modal, Note, Prog, cx, useOutside,
} from '../../ui/index.jsx';
import { Conf } from '../workbench/autobuild.jsx';
import { inlineTokens } from '../workbench/md.js';
import * as M from './inboxModel.js';
import '../../styles/inbox.css';

const AMBER_CARD = { borderColor: 'var(--a100)', background: 'linear-gradient(180deg,#FFFBF2,#fff)' };
const DONE_TOAST = {
  accept: ['已接受', 'check'],
  rejudge: ['已改判，助手正在重做', 'edit'],
  use_generic: ['已按通用要求处理', 'check'],
  refresh: ['已开始刷新', 'refresh'],
  keep_stale: ['先不管，保持过期', 'check'],
  resume: ['已续跑，进展在对话里', 'play'],
  abandon: ['已放弃', 'undo'],
  send_to_evolution: ['已交给反馈演进处理，进展在对话里', 'refresh'],
  execute: ['清单已确认，开始按清单修改', 'check'],
  dismiss: ['已忽略', 'check'],
};

// 正文里的 [^n] 渲染成出处角标，[[名称]] 只保留文字
function BgText({ text, anchors }) {
  const map = Object.fromEntries((anchors || []).map((a) => [a.n, a]));
  return inlineTokens(text).map((t, i) => {
    if (t.t === 'cite') {
      const a = map[t.v];
      return a ? <Ev key={i} anchor={a.anchor} label={a.label || M.anchorLabel(a.anchor)} /> : null;
    }
    return <Fragment key={i}>{t.v}</Fragment>;
  });
}

function Highlight({ text, mark }) {
  if (!mark || !text?.includes(mark)) return text || null;
  const i = text.indexOf(mark);
  return <>{text.slice(0, i)}<mark>{mark}</mark>{text.slice(i + mark.length)}</>;
}

function Opts({ options, sel, onPick, tagOf, text, setText, placeholder }) {
  return (
    <div className="col" style={{ gap: 8, marginTop: 14 }}>
      {options.map((o, i) => (
        <Fragment key={o.id}>
          <div className={cx('opt', sel === i && 'on')} onClick={() => onPick(i)}>
            <span className="k">{i + 1}</span>
            <div className="grow">
              <div className="t row g4"><span>{o.custom ? `${o.label}（E）` : o.label}</span>{tagOf?.(o, i)}</div>
              {o.consequence ? <div className="c">{o.consequence}</div> : null}
            </div>
          </div>
          {o.custom && sel === i && text != null ? (
            <div className="inp focus ib-custom">
              <Icon name="edit" size={14} />
              <input data-ib-edit="1" autoFocus value={text} placeholder={placeholder || '说说你的方向…'} onChange={(e) => setText(e.target.value)} />
            </div>
          ) : null}
        </Fragment>
      ))}
    </div>
  );
}

// ---------- 各类事项的详情 ----------
function DecisionBody({ q, ...p }) {
  return (
    <>
      {q.background_md ? <div className="sm muted mt12"><BgText text={q.background_md} anchors={q.anchors} /></div> : null}
      <Opts options={q.options || []} {...p} placeholder={q.custom_placeholder} tagOf={(o) => (o.recommended ? <Chip tone="blue" sm>推荐</Chip> : null)} />
    </>
  );
}

function Side({ s }) {
  const icon = /^db:/.test(s.anchor || '') ? 'db' : /\.xlsx?$/i.test(s.label) ? 'table' : 'filetext';
  const chip = s.type_label === '发布日期'
    ? <Chip tone="line" sm>{s.date} 发布</Chip>
    : <Chip tone={s.type_label === '系统元数据' ? 'teal' : 'line'} sm>{s.type_label}</Chip>;
  return (
    <div className="side">
      <div className="row sm"><Icon name={icon} size={14} /><b>{s.label}</b><span className="sp" />{chip}</div>
      <div className="big">{s.value}</div>
      <div className="sm muted"><Highlight text={s.note} mark={s.highlight} /></div>
      <div className="xs faint mt8">出处：<span data-ev={s.anchor || undefined} style={{ cursor: 'help' }}>{s.anchor_label || M.anchorLabel(s.anchor)}</span></div>
    </div>
  );
}

function ConflictBody({ d, ...p }) {
  const [a, b] = d.sides || [];
  return (
    <>
      <div className="g2-vs">{a ? <Side s={a} /> : <div />}<div className="ne">≠</div>{b ? <Side s={b} /> : <div />}</div>
      <Opts options={d.question?.options || []} {...p} tagOf={(o) => (o.recommended ? <Chip tone="blue" sm>推荐</Chip> : null)} />
    </>
  );
}

function ChangeListBody({ d, act, batch, busy }) {
  const points = d.points || [];
  const [checked, setChecked] = useState(() => new Set(points.filter((x) => x.confidence === 'high' && x.state === 'pending').map((x) => x.id)));
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState('');
  const sel = points.filter((x) => x.state === 'pending' && x.confidence === 'high' && checked.has(x.id)).map((x) => x.id);
  const toggle = (id) => setChecked((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const save = async (pt) => {
    if (!draft.trim()) return;
    if (await act('rewrite_point', { point_id: pt.id, text: draft.trim() }, { silent: true })) {
      setEditing(null);
      toast('已保存改写', 'edit');
    }
  };
  const low = points.filter((x) => x.confidence === 'low' && x.state === 'pending');
  return (
    <>
      <div className="row mt12">
        <span className="sp" />
        <Button size="sm" kind="primary" disabled={!sel.length || busy} onClick={() => batch({ action: 'accept_change_points', change_list_id: d.id, point_ids: sel })}>接受选中的 {sel.length} 项</Button>
      </div>
      <div className="tbl-wrap mt12">
        <table className="tbl g2-ct">
          <thead><tr><th style={{ width: 34 }} /><th>变更点（条件 / 取值 / 范围）</th><th>出处</th><th>把握</th><th>影响</th><th style={{ width: 120 }} /></tr></thead>
          <tbody>
            {points.map((pt) => {
              const [cl, tone] = M.CONFIDENCE_LABEL[pt.confidence] || ['—', 'gray'];
              const isEdit = editing === pt.id;
              const done = pt.state !== 'pending';
              return (
                <tr key={pt.id} className={cx(pt.confidence === 'low' && 'low', isEdit && 'edit')}>
                  <td>
                    {done ? <span style={{ color: 'var(--blue)' }}><Icon name="check" size={14} /></span>
                      : pt.confidence === 'low' ? <span title="把握低，只能单独确认"><Icon name="lock" size={14} /></span>
                        : (
                          <span
                            className={cx('chk', checked.has(pt.id) && pt.confidence === 'high' && 'on')}
                            title={pt.confidence === 'high' ? '' : '把握中等，要单独确认'}
                            style={pt.confidence === 'high' ? undefined : { cursor: 'not-allowed' }}
                            onClick={() => pt.confidence === 'high' && toggle(pt.id)}
                          />
                        )}
                  </td>
                  <td>
                    {isEdit ? (
                      <div className="inp focus" style={{ height: 30 }}>
                        <input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') save(pt); if (e.key === 'Escape') setEditing(null); }} />
                      </div>
                    ) : (
                      <><b style={{ fontWeight: 600 }}>{pt.n}.</b> {M.changePointText(pt)}{done ? <> <Chip tone="gray" sm>{pt.state === 'rewritten' ? '已改写' : '已接受'}</Chip></> : null}</>
                    )}
                  </td>
                  <td><Ev anchor={pt.anchor} label={pt.anchor_label} /></td>
                  <td><Chip tone={tone} sm>{cl}</Chip></td>
                  <td className="muted">{(pt.impact || []).map(M.impactName).join('、')}</td>
                  <td>
                    {isEdit ? <Button size="xs" kind="soft" disabled={!draft.trim() || busy} onClick={() => save(pt)}>保存改写</Button> : (
                      <span className="row g4">
                        {!done ? <Button size="xs" disabled={busy} onClick={() => act('accept_points', { point_ids: [pt.id] }, { silent: true }).then((ok) => ok && toast(`已接受第 ${pt.n} 条`, 'check'))}>接受</Button> : null}
                        <Button size="xs" kind="ghost" onClick={() => { setEditing(pt.id); setDraft(M.changePointText(pt)); }}>改写</Button>
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {low.map((pt) => (
        <div className="mt12" key={pt.id}><Note tone="amber">第 {pt.n} 条把握低：{pt.confidence_note || '资料说法不完整'}</Note></div>
      ))}
    </>
  );
}

function ReviewBody({ d, item, act, sub, busy, ...p }) {
  const { pid } = useProject();
  const adopted = M.adoptedIndex(d.options, d.chosen);
  const changed = p.text != null ? !!p.text.trim() : p.sel != null && p.sel !== adopted;
  const rejudge = () => {
    if (!changed) { toast(M.pickHint(d.options, '先选一个别的选项'), 'info'); return; }
    const plan = M.confirmPlan({ kind: 'review', actions: ['rejudge'] }, d, p.sel, p.text);
    if (plan.error) toast(plan.error, 'alert');
    else act(plan.action, plan.body);
  };
  return (
    <>
      <div className="card pad mt16" style={{ background: 'linear-gradient(180deg,#F7F7FF,#fff)', borderColor: 'var(--i100)' }}>
        <div className="row">
          <Chip tone="indigo" icon="bot">运行 #{item.run?.run_no} · {M.hhmm(d.at)} 自动决定</Chip><span className="sp" />
          置信度 <Conf v={d.confidence} threshold={d.threshold} /><span className="xs muted">阈值 {(d.threshold ?? 0.8).toFixed(2)}</span>
        </div>
        <div className="sm mt8">{d.reason_text}依据：{(d.anchors || []).map((a) => <Ev key={a.n} anchor={a.anchor} label={a.label || M.anchorLabel(a.anchor)} />)}</div>
      </div>
      <Opts options={d.options || []} {...p} tagOf={(o, i) => (i === adopted ? <Chip tone="indigo" sm>自主构建采用</Chip> : null)} />
      <div className="row mt16 wrap" style={{ gap: 8 }}>
        <Button size="sm" kind="primary" icon="check" disabled={busy} onClick={() => act('accept')}>接受这个决定</Button>
        <Button size="sm" icon="edit" disabled={busy} onClick={rejudge}>改判为选中的选项</Button>
        <span className="sp" />
        <Button size="sm" kind="ghost" icon="eye" onClick={() => navigate(pagePath('versions', pid), { v: d.slice_version_label || '', run: d.run_id, slice: d.slice?.id })}>{sliceName(d.slice?.id, d.slice?.name) ? '查看这一段的改动' : '查看改动'}</Button>
        {!sub && item.actions?.includes('revert_slice') ? (
          <Button size="sm" kind="ghost" icon="undo" disabled={busy} onClick={() => act('revert_slice')}>回退{sliceName(d.slice?.id, d.slice?.name) ? `「${d.slice.name}」` : ''}到运行前{d.base_version_label ? ` · ${d.base_version_label}` : ''}</Button>
        ) : null}
      </div>
    </>
  );
}

function HighBody({ d, act, openSub, busy }) {
  const n = d.entries?.length || 0;
  return (
    <>
      <div className="row mt12"><span className="sp" /><Button size="sm" kind="primary" icon="check" disabled={busy || !n} onClick={() => act('accept')}>全部接受（{n}）</Button></div>
      <div className="tbl-wrap mt8">
        <table className="tbl compact">
          <thead><tr><th>时间</th><th>自动决定</th><th>置信度</th><th /></tr></thead>
          <tbody>
            {(d.entries || []).map((l) => (
              <tr key={l.id}>
                <td className="muted num">{M.hhmm(l.at)}</td>
                <td>{l.summary || l.chosen}</td>
                <td><Conf v={l.confidence} threshold={l.threshold} /></td>
                <td><Button size="xs" kind="ghost" onClick={() => openSub(l)}>改判</Button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function SuspensionBody({ d, item, act, busy, ...p }) {
  const { pid } = useProject();
  return (
    <>
      <div className="card pad mt16" style={AMBER_CARD}>
        <KV w="84px" pairs={[['挂起时间', `${M.hhmm(d.at)}${item.run ? ` · 运行 #${item.run.run_no}` : ''}`], ['原因', d.reason], ['当时怎么处理', d.handled_as]]} />
      </div>
      {d.options?.length ? <Opts options={d.options} {...p} /> : (
        <div className="row mt16 wrap" style={{ gap: 8 }}>
          {(d.actions || []).includes('upload_text') ? <Button size="sm" kind="primary" icon="upload" onClick={() => navigate(pagePath('resources', pid), d.material ? { path: d.material.path } : undefined)}>上传文本版</Button> : null}
          {(d.actions || []).includes('use_generic') ? <Button size="sm" disabled={busy} onClick={() => act('use_generic')}>先按通用要求</Button> : null}
        </div>
      )}
    </>
  );
}

function StaleBody({ d, item, act, batch, staleCount, busy }) {
  const what = item.impact?.[0]?.label || '这一项';
  const lines = d.upstream_diff?.lines || [];
  return (
    <>
      <div className="card pad mt16">
        <div className="sec-t"><Icon name="refresh" size={12} />上游改了什么</div>
        <div className="diff">
          <div className="h">{d.upstream_diff?.title}</div>
          {lines.map((l, i) => <div key={i} className={{ del: 'd', add: 'a', header: 'h', note: 'h' }[l.kind]}>{l.text}</div>)}
        </div>
        <div className="sec-t" style={{ marginTop: 16 }}><Icon name="flask" size={12} />这个{what}受到的影响</div>
        <KV w="64px" pairs={[['原预期', d.impact?.expected_before], ['现在', d.impact?.now], [what === '场景' ? '推演' : '处理', d.impact?.action]]} />
      </div>
      <div className="row mt16">
        <Button kind="primary" size="sm" icon="refresh" disabled={busy} onClick={() => act('refresh')}>刷新这个{what}</Button>
        {staleCount > 1 ? <Button size="sm" disabled={busy} onClick={() => batch({ action: 'refresh_stale' })}>全部刷新 {staleCount} 项</Button> : null}
        <Button size="sm" kind="ghost" disabled={busy} onClick={() => act('keep_stale')}>先不管，保持过期</Button>
      </div>
    </>
  );
}

const NOT_RESUMABLE = { inputs_changed: '输入已经变了，按新版从头做', node_upgraded: '这一步的能力已经升级，按新版从头做' };
function ResumableBody({ d, act, busy }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [conv, setConv] = useState('');
  useEffect(() => {
    let alive = true;
    api.conversations(pid).then((r) => { if (alive) setConv((r.items || []).find((c) => c.id === d.conversation_id)?.title || ''); }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid, d.conversation_id]);
  const why = d.interrupted?.reason === 'restart' ? `服务在 ${M.hhmm(d.interrupted.at)} 重启，这一步没有正常结束` : d.interrupted?.reason_text;
  return (
    <div className="card pad mt16" style={AMBER_CARD}>
      <KV w="72px" pairs={[
        ['原因', why || '—'], ['中断位置', d.phase?.label || '—'], ['已保存', d.saved ? `${d.saved.files} 个文件，版本 ${d.saved.version_label || '—'}` : '—'],
        ['怎么继续', d.resumable ? '从中断处接着做' : NOT_RESUMABLE[d.not_resumable_reason] || '按新版从头做'], ['所在会话', conv || '—'],
      ]} />
      <div className="row mt16">
        <Button size="sm" kind="ghost" disabled={busy} onClick={() => act('abandon')}>放弃，从头再来</Button><span className="sp" />
        <Button size="sm" kind="primary" icon="play" disabled={busy} onClick={() => act('resume')}>{d.resumable ? `从步骤 ${d.phase?.index} 续跑` : '按新版重新做'}</Button>
      </div>
    </div>
  );
}

function FeedbackBody({ d, act, busy }) {
  const { pid } = useProject();
  return (
    <>
      <div className="tbl-wrap mt12">
        <table className="tbl compact">
          <thead><tr><th>用例</th><th>期望</th><th>实际</th><th>疑似原因</th></tr></thead>
          <tbody>{(d.items || []).map((x, i) => <tr key={i}><td>{x.question}</td><td>{x.expected}</td><td>{x.actual}</td><td>{x.loss_reason}</td></tr>)}</tbody>
        </table>
      </div>
      <div className="row mt16">
        <Button size="sm" kind="primary" icon="refresh" disabled={busy} onClick={() => act('send_to_evolution')}>交给反馈演进处理</Button>
        <Button size="sm" onClick={() => navigate(pagePath('resources', pid), { group: 'feedback', feedback: d.feedback_id })}>逐条查看</Button>
      </div>
    </>
  );
}

const ACTION_LABEL = { dismiss: '忽略', discard: '撤回这一轮', migrate: '迁移', send_to_assistant: '交给助手处理', resolve: '去处理' };
function GenericBody({ item, act, busy }) {
  return (
    <div className="row mt16 wrap" style={{ gap: 8 }}>
      {(item.actions || []).filter((a) => ACTION_LABEL[a]).map((a) => <Button key={a} size="sm" kind={a === 'dismiss' ? 'ghost' : 'soft'} disabled={busy} onClick={() => act(a)}>{ACTION_LABEL[a]}</Button>)}
    </div>
  );
}

function Detail({ view, sub, ...p }) {
  const { item, detail: d } = view;
  const [cls, icon] = M.kindStyle(item.kind);
  const title = sub ? sub.summary || sub.question : item.title;
  const scope = sub ? sub.slice : item.slice;
  const scopeName = sliceName(scope?.id, scope?.name);
  const age = sub ? M.ageText({ run: { ...item.run, at: sub.at } }) : M.ageText(item);
  let body = null;
  if (sub) body = <ReviewBody d={sub} item={item} sub {...p} />;
  else if (item.kind === 'decision') body = <DecisionBody q={d} {...p} />;
  else if (item.kind === 'evidence_conflict') body = <ConflictBody d={d} {...p} />;
  else if (item.kind === 'change_list') body = <ChangeListBody key={item.id} d={d} act={p.act} batch={p.batch} busy={p.busy} />;
  else if (item.kind === 'review' && d?.group) body = <HighBody d={d} act={p.act} openSub={p.openSub} busy={p.busy} />;
  else if (item.kind === 'review') body = <ReviewBody d={d} item={item} {...p} />;
  else if (item.kind === 'suspension') body = <SuspensionBody d={d} item={item} {...p} />;
  else if (item.kind === 'stale') body = <StaleBody d={d} item={item} act={p.act} batch={p.batch} staleCount={p.staleCount} busy={p.busy} />;
  else if (item.kind === 'resumable') body = <ResumableBody d={d} act={p.act} busy={p.busy} />;
  else if (item.kind === 'feedback') body = <FeedbackBody d={d} act={p.act} busy={p.busy} />;
  else body = <GenericBody item={item} act={p.act} busy={p.busy} />;
  return (
    <>
      {sub ? <a className="link sm row g4" style={{ marginBottom: 8 }} onClick={p.closeSub}><Icon name="arrowl" size={12} />返回{item.title.replace(/，.*$/, '')}</a> : null}
      <div className="row">
        <span className={cx('chip', `k-${cls}`)} style={{ border: 0 }}><Icon name={icon} />{item.kind_label}</span>
        <span className="muted sm">{[scopeName, age].filter(Boolean).join(' · ')}</span>
      </div>
      <h1>{title}</h1>
      {body}
    </>
  );
}

// 「自主构建复核」筛选带上的运行汇总条
function RunBanner({ s, onSlices, onAcceptHigh, busy }) {
  const nums = [[s.decisions, '自动决策'], [s.high_confidence, '高置信'], [s.needs_look, '需看'], [s.suspended, '挂起的问题'], [s.assumptions, '假设']];
  const pending = s.high_confidence_pending ?? s.high_confidence;
  return (
    <div className="g2-l4b">
      <span className="tile"><Icon name="bot" size={18} /></span>
      <div style={{ minWidth: 0 }}><div className="b">自主构建 · 运行 #{s.run_no}</div><div className="xs muted">{M.summarySub(s)}</div></div>
      <div className="nums">{nums.map(([b, t]) => <div key={t}><b>{b ?? '—'}</b><span>{t}</span></div>)}</div>
      <span className="sp" />
      <Button size="sm" icon="layers" onClick={onSlices}>运行结果</Button>
      <Button size="sm" kind="primary" icon="check" disabled={busy || !pending} onClick={onAcceptHigh}>{pending ? `一键接受高置信 ${pending} 项` : '高置信已全部接受'}</Button>
    </div>
  );
}

// 运行结果（AB-16）：查看改动、回退到运行前（AB-17）；F8 之前按段跑的运行每段一行
function SliceResults({ open, onClose, runId, onReverted }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [r, setR] = useState(null);
  const [busy, setBusy] = useState(null);
  const load = useCallback(() => api.autobuildReport(pid, runId).then(setR), [api, pid, runId]);
  useEffect(() => { if (open && runId) load().catch((e) => { toast(errorText(e), 'alert'); setR(null); }); }, [open, runId, load]);
  const revert = async (row) => {
    setBusy(row.slice_id);
    try {
      const x = await api.revertAutobuildSlice(pid, runId, row.slice_id);
      toast(`已把「${row.name}」回退到运行前（${x.reverted_to_version_label}），并保存为新版本 ${x.version_label}`, 'undo');
      await load();
      onReverted?.();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(null);
    }
  };
  const span = r ? `${M.hhmm(r.started_at)} – ${M.hhmm(r.ended_at)}` : '';
  const multi = multiSlice((r?.slices || []).map((row) => row.slice_id));
  const scenarios = (r?.slices || []).some((row) => row.scenarios?.total);
  return (
    <Modal open={open} onClose={onClose} icon="layers" w={860} title={`运行 #${r?.run_no ?? ''} · 运行结果`} sub={r ? M.summarySub({ ...r, cost: null }) : ''}>
      {r ? (
        <>
          <div className="tbl-wrap">
            <table className="tbl compact">
              <thead><tr><th>{multi ? '分段' : ''}</th><th>结果</th><th>产出</th>{scenarios ? <th>场景</th> : null}<th>运行后</th><th /></tr></thead>
              <tbody>
                {r.slices.map((row) => (
                  <tr key={row.slice_id}>
                    <td><b style={{ fontWeight: 600 }}>{unitTitle(row.slice_id, row.name)}</b></td>
                    <td><Chip tone={row.reverted || row.result === 'skipped' ? 'gray' : /部分/.test(row.result_text) ? 'amber' : 'indigo'} sm>{row.result_text}</Chip></td>
                    <td className="muted">{row.produced_text}</td>
                    {scenarios ? <td className="num">{row.scenarios?.total ? `${row.scenarios.passed} / ${row.scenarios.total}` : '—'}</td> : null}
                    <td><span className="tag">{row.version_label}</span></td>
                    <td>
                      <span className="row g4">
                        <Button size="xs" onClick={() => { onClose(); navigate(pagePath('versions', pid), { v: row.version_label, run: runId, slice: row.slice_id }); }}>查看改动</Button>
                        <Button size="xs" kind="ghost" disabled={!row.revertible || !!busy} onClick={() => revert(row)}>回退到运行前</Button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt12"><Note tone="gray" icon="undo">{multi ? '回退只影响这一段，另存为新版本' : '回退这次运行的修改，另存为新版本'}</Note></div>
        </>
      ) : <div className="sm muted">{span || '正在读取…'}</div>}
    </Modal>
  );
}

function SortButton({ sort, setSort }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  return (
    <span ref={ref}>
      <Button icon="sortd" size="sm" kind="ghost" title="排序" onClick={() => setOpen((v) => !v)} />
      {open ? (
        <Menu
          className="ib-sort"
          items={M.SORTS.map(([k, t]) => ({ label: t, icon: k === sort ? 'check' : 'dot', onClick: () => setSort(k) }))}
          onPick={() => setOpen(false)}
        />
      ) : null}
    </span>
  );
}

export default function InboxPage({ route }) {
  const { api } = useApp();
  const project = useProject();
  const { pid, subscribe } = project;
  const evidenceOn = evidenceInProject(project);
  const q = route?.query || {};
  const validFilter = (f) => (M.FILTERS.some(([k]) => k === f) ? f : 'all');
  const [filter, setFilter] = useState(validFilter(q.filter));
  const [sort, setSort] = useState('default');
  const [data, setData] = useState(null);
  const [cur, setCur] = useState(null);
  const [want, setWant] = useState({ item: q.item || null, entry: q.entry || null });
  const [detail, setDetail] = useState(null);
  const [rev, setRev] = useState(0);
  const [sub, setSub] = useState(null);
  const [sel, setSel] = useState(null);
  const [text, setText] = useState(null);
  const [busy, setBusy] = useState(false);
  const [slices, setSlices] = useState(false);
  const subWant = useRef(null);
  const shownId = useRef(null);

  const load = useCallback(() => api.inbox(pid, { filter, limit: 200 }).then(setData), [api, pid, filter]);
  useEffect(() => { load().catch((e) => toast(errorText(e), 'alert')); }, [load]);
  useEffect(() => subscribe((env) => {
    if (env.type === 'inbox.updated' || env.type === 'autobuild.finished') load().catch(() => {});
  }), [subscribe, load]);
  // 从别的页面带着 filter / item / entry 跳过来
  useEffect(() => {
    if (q.filter) setFilter(validFilter(q.filter));
    if (q.item || q.entry) setWant({ item: q.item || null, entry: q.entry || null });
  }, [q.filter, q.item, q.entry]); // eslint-disable-line react-hooks/exhaustive-deps

  const items = useMemo(() => M.sortItems(M.withoutEvidence(data?.items, evidenceOn), sort), [data, sort, evidenceOn]);

  useEffect(() => {
    if (!data) return;
    const ids = items.map((x) => x.id);
    if (want.item || want.entry) {
      let id = want.item && ids.includes(want.item) ? want.item : null;
      if (!id && want.entry) {
        id = ids.find((x) => x === `review:${want.entry}`) || null;
        const hi = items.find((x) => x.high_confidence);
        if (!id && hi) { id = hi.id; subWant.current = want.entry; }
      }
      if (!id && filter !== 'all') { setFilter('all'); return; }
      setWant({ item: null, entry: null });
      if (id) { setCur(id); return; }
    }
    if (!cur || !ids.includes(cur)) setCur(ids[0] || null);
  }, [data, items]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!cur) { setDetail(null); return undefined; }
    let alive = true;
    api.inboxItem(pid, cur).then((d) => {
      if (!alive) return;
      // 换了一项才重置选择；同一项刷新（接受了一条变更点等）保留
      if (shownId.current !== d.id) {
        shownId.current = d.id;
        setText(null);
        const s = subWant.current && d.detail?.group ? d.detail.entries.find((e) => e.id === subWant.current) || null : null;
        subWant.current = null;
        setSub(s);
        if (s) setSel(M.adoptedIndex(s.options, s.chosen));
        else setSel(d.kind === 'review' && !d.detail?.group ? M.adoptedIndex(M.optionsOf(d, d.detail), d.detail?.chosen) : null);
      }
      setDetail(d);
    }).catch((e) => {
      if (!alive) return;
      if (e.status === 404) load().catch(() => {});
      else toast(errorText(e), 'alert');
    });
    return () => { alive = false; };
  }, [api, pid, cur, rev]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { document.querySelector('.g2-qi.g2-sel')?.scrollIntoView({ block: 'nearest' }); }, [cur]);

  const view = detail && detail.id === cur ? { item: detail, detail: sub || detail.detail } : null;
  const curItem = items.find((x) => x.id === cur);
  const opts = view ? M.optionsOf(sub ? { kind: 'review' } : view.item, view.detail) : [];
  const keys = M.keyHints(opts);

  const goNext = (next) => setCur(M.afterResolve(items, cur, next));
  const act = async (action, body = {}, { silent } = {}) => {
    if (!view || busy) return false;
    setBusy(true);
    try {
      if (sub) {
        if (action === 'accept') await api.acceptLedgerEntry(pid, sub.id);
        else await api.rejudgeLedgerEntry(pid, sub.id, body);
        toast(...DONE_TOAST[action === 'accept' ? 'accept' : 'rejudge']);
        setSub(null);
        setSel(null);
        setText(null);
        setRev((x) => x + 1);
        await load();
        return true;
      }
      const r = await api.inboxAction(pid, cur, { action, ...body });
      if (!silent) {
        if (action === 'revert_slice') toast(`已回退到运行前（${r.result?.reverted_to_version_label || ''}），并保存为新版本 ${r.result?.version_label || ''}`, 'undo');
        else if (DONE_TOAST[action]) toast(...DONE_TOAST[action]);
        else toast(`已处理：${view.item.title.slice(0, 16)}…`, 'check');
      }
      if (r.resolved) {
        goNext(r.next_item_id);
        await load();
      } else setRev((x) => x + 1);
      return true;
    } catch (e) {
      toast(errorText(e), 'alert');
      return false;
    } finally {
      setBusy(false);
    }
  };
  const batch = async (body) => {
    setBusy(true);
    try {
      const r = await api.inboxBatch(pid, body);
      const skipped = r.skipped?.length ? `，${r.skipped.length} 项把握不够高要单独确认` : '';
      if (body.action === 'accept_high_confidence') toast(`已接受 ${r.accepted} 项`, 'check');
      else if (body.action === 'refresh_stale') toast(`已开始刷新 ${r.accepted} 项`, 'refresh');
      else toast(`已接受 ${r.accepted} 项${skipped}`, 'check');
      setRev((x) => x + 1);
      await load();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const pick = (i) => {
    const o = opts[i];
    if (!o) return;
    setSel(i);
    if (o.custom) setText((t) => t ?? '');
    else setText(null);
  };
  const confirm = () => {
    if (!view) return;
    const plan = M.confirmPlan(sub ? { kind: 'review', actions: ['accept', 'rejudge'] } : view.item, view.detail, sel, text);
    if (plan.error) { toast(plan.error, 'alert'); return; }
    act(plan.action, plan.body);
  };
  const later = () => setCur(M.stepId(items, cur, 1));

  // 键盘：J/K、1–4、E、↵、Esc
  const keyRef = useRef(null);
  keyRef.current = (e) => {
    if (document.querySelector('.overlay,#palette.show')) return;
    const t = e.target;
    const inInput = !!t?.matches?.('input,textarea,select,[contenteditable="true"]');
    if (inInput && !t.dataset?.ibEdit) return;
    const a = M.keyAction({ key: e.key, shiftKey: e.shiftKey, metaKey: e.metaKey, ctrlKey: e.ctrlKey, altKey: e.altKey, isComposing: e.isComposing, inInput });
    if (!a) return;
    if (a.type === 'move') setCur(M.stepId(items, cur, a.dir));
    else if (a.type === 'pick') pick(a.index);
    else if (a.type === 'rewrite') {
      // 没有自定义回答的事项（如人工节点的决策卡，后端只收选项）不进改写
      const i = opts.findIndex((o) => o.custom);
      if (i < 0) return;
      pick(i);
      toast('改写：在选项里写下你的方向', 'edit');
    } else if (a.type === 'confirm') confirm();
    else if (a.type === 'cancel') {
      if (text != null) { setText(null); if (opts[sel]?.custom) setSel(null); }
      else if (sub) setSub(null);
    } else return;
    e.preventDefault();
  };
  useEffect(() => {
    const h = (e) => keyRef.current?.(e);
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, []);

  const counts = M.countsWithoutEvidence(data?.counts, evidenceOn);
  const today = data?.today;
  const summary = filter === 'autobuild_review' ? data?.autobuild_summary : null;
  const impact = sub ? [...(sliceName(sub.slice?.id, sub.slice?.name) ? [{ label: '分段', items: [sub.slice.name] }] : []), ...(sub.affected?.length ? [{ label: '条目', items: sub.affected.map((x) => x.name || x.title) }] : [])] : view?.item.impact || [];
  const doneTotal = today ? today.total : 0;
  const pctDone = doneTotal ? Math.round((today.handled / doneTotal) * 100) : 0;

  return (
    <ShellP active="inbox" flush>
      <div className="g2-ibw">
        {summary ? <RunBanner s={summary} busy={busy} onSlices={() => setSlices(true)} onAcceptHigh={() => batch({ action: 'accept_high_confidence', run_id: summary.run_id })} /> : null}
        <div className="g2-ib">
          <div className="card" style={{ position: 'relative' }}>
            <div className="row" style={{ padding: '14px 14px 4px' }}>
              <b className="lg">待处理</b><span className="chip blue sm num">{counts?.total ?? '…'}</span><span className="sp" />
              <SortButton sort={sort} setSort={setSort} />
            </div>
            <div className="g2-fl">
              {M.visibleFilters(counts, filter, evidenceOn).map(([k, t]) => (
                <span key={k} className={k === filter ? 'on' : ''} onClick={() => setFilter(k)}>
                  {k === 'autobuild_review' ? <Icon name="bot" size={12} /> : null}{t} <i>{counts?.by_filter?.[k] ?? 0}</i>
                </span>
              ))}
            </div>
            <div className="scroll" style={{ flex: 1 }}>
              {!data ? <div className="sm muted" style={{ padding: 14 }}>正在读取…</div> : items.length ? items.map((it) => {
                const [cls, icon] = M.kindStyle(it.kind);
                return (
                  <div key={it.id} className={cx('g2-qi', it.id === cur && 'g2-sel')} onClick={() => setCur(it.id)}>
                    <div className={cx('g2-qic', `k-${cls}`)}><Icon name={icon} size={14} /></div>
                    <div style={{ minWidth: 0 }}>
                      <div className="g2-qt">{it.title}</div>
                      <div className="g2-qm"><span className={cx('chip sm', `k-${cls}`)}>{it.kind_label}</span>{it.slice?.name ? <><span>{it.slice.name}</span><span>·</span></> : null}<span>{M.ageText(it)}</span></div>
                    </div>
                  </div>
                );
              }) : <Empty icon="check" title="这一类都处理完了" />}
            </div>
          </div>
          <div className="card" style={{ position: 'relative' }}>
            <div className="g2-cf">
              {view ? (
                <Detail
                  key={`${cur}:${sub?.id || ''}`}
                  view={view} sub={sub} sel={sel} onPick={pick} text={text} setText={setText} act={act} batch={batch} busy={busy}
                  staleCount={counts?.by_filter?.stale || 0}
                  openSub={(l) => { setSub(l); setText(null); setSel(M.adoptedIndex(l.options, l.chosen)); }}
                  closeSub={() => { setSub(null); setSel(null); setText(null); }}
                />
              ) : data && !items.length ? <Empty icon="check" title="都处理完了" />
                : curItem ? <div className="sm muted">正在读取…</div> : null}
            </div>
            <div className="g2-cft">
              {keys ? <span className="xs muted row g4"><Kbd>1</Kbd>–<Kbd>4</Kbd> 选择{keys.rewrite ? <>　<Kbd>E</Kbd> 改写</> : null}</span> : null}
              <span className="sp" />
              <Button size="sm" kind="ghost" disabled={!view} onClick={later} title="J">稍后再说</Button>
              <Button size="sm" kind="primary" icon="check" disabled={!view || busy} onClick={confirm} title="↵">{M.confirmLabel(sub ? { kind: 'review' } : view?.item, view?.detail)}</Button>
            </div>
          </div>
          <div className="col" style={{ gap: 14, minHeight: 0, overflow: 'auto' }}>
            <Card title="影响范围" icon="target">
              {impact.map((x) => (
                <div key={x.label} className="row sm" style={{ padding: '4px 0', alignItems: 'flex-start' }}>
                  <span className="muted" style={{ width: 40, flex: 'none' }}>{x.label}</span><span>{(x.items || []).join('、')}</span>
                </div>
              ))}
              {view ? null : <div className="xs muted">选中一项后显示</div>}
            </Card>
            <Card title="今天的进度" icon="trend">
              <div className="row"><b className="num" style={{ fontSize: 22 }}>{today?.handled ?? 0}</b><span className="muted">/ {doneTotal} 已处理</span><span className="sp" /><span className="xs muted">约 {today?.eta_minutes ?? 0} 分钟</span></div>
              <Prog p={pctDone} />
              <div className="col mt12" style={{ gap: 6 }}>
                {M.todayRows(today).map(([a, b]) => (
                  <div key={a} className="row sm" style={{ alignItems: 'flex-start', gap: 8 }}><span style={{ flex: 'none' }}>{a}</span><span className="muted grow" style={{ textAlign: 'right' }}>{b}</span></div>
                ))}
              </div>
            </Card>
          </div>
        </div>
      </div>
      <SliceResults open={slices} onClose={() => setSlices(false)} runId={summary?.run_id} onReverted={() => load().catch(() => {})} />
    </ShellP>
  );
}
