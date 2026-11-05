// 自主构建的对话卡片（契约 4.7：autobuild_start、slice_progress、auto_decision、suspension、run_report），样式见 autobuild.css
import { useContext, useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Ev, Icon, cx } from '../../ui/index.jsx';
import { Conf, useAutobuildCtx } from './autobuild.jsx';
import {
  REPORT_CHIP, hhmm, reportMetrics, reportSub, sliceSegments,
} from './autobuildModel.js';
import { evidenceInProject } from '../../state/evidence.js';
import { multiSlice, sliceName, unitTitle } from '../../state/flow.js';
import { CC, Tli, WbCtx } from './parts.jsx';
import '../../styles/autobuild.css';

const POLICY_ICON = { 范围: 'layers', 生产库: 'db', 拿不准时: 'scale' };

// 开始时的策略：范围 / 生产库 / 拿不准时（生产库一格随证据源出现，本期不开放）
function AutobuildStartCard({ card }) {
  const { pid } = useContext(WbCtx);
  const evidenceOn = evidenceInProject(useProject());
  const chips = (card.chips || []).filter((c) => c.label !== '生产库' || evidenceOn);
  return (
    <>
      <div className="g2-pol">
        {chips.map((c) => <span key={c.label}><Icon name={POLICY_ICON[c.label] || 'dot'} size={12} />{c.label} <b>{c.value}</b></span>)}
      </div>
      <p className="sm muted g2-pol-note">
        <a className="link" onClick={() => navigate(pagePath('proj-settings', pid), { section: 'build-mode' })}>查看或调整策略</a>
      </p>
    </>
  );
}

// 自动决定（对话里单独出现，或嵌在按段的进展块里）
export function AutoDecision({ card }) {
  const { pid } = useContext(WbCtx);
  const review = card.review_state || 'pending';
  return (
    <div className="g2-ad">
      <div className="row">
        <Chip tone="indigo" sm icon="bot">自动决定 · {hhmm(card.at)}</Chip>
        <span className="sp" />
        <span className="xs muted">置信度</span><Conf v={card.confidence} threshold={card.threshold} /><span className="xs faint">阈值 {(card.threshold ?? 0.8).toFixed(2)}</span>
      </div>
      <div className="ln"><b>问题</b><span>{card.question}</span></div>
      <div className="ln"><b>采用</b><span><b className="v">{card.chosen}</b> {(card.anchors || []).map((a) => <Ev key={a.n} anchor={a.anchor} label={a.label} />)}</span></div>
      <div className="row mt8">
        {review === 'accepted' ? <Chip tone="gray" sm>已复核 · 接受</Chip>
          : review === 'rejudged' ? <Chip tone="indigo" sm>已改判</Chip>
            : <Chip tone={card.assumption ? 'amber' : 'indigo'} sm>{card.assumption ? '需复核 · 记为假设' : '需复核 · 高置信'}</Chip>}
        <span className="sp" />
        {review === 'pending' ? <a className="link xs" onClick={() => navigate(pagePath('inbox', pid), { entry: card.ledger_id })}>改判</a> : null}
      </div>
    </div>
  );
}

// 挂起：不停下来问，先记着继续跑
export function Suspended({ card }) {
  const { pid } = useContext(WbCtx);
  return (
    <div className="g2-sp">
      <span className="pi"><Icon name="pause" size={12} /></span>
      <div className="grow"><div className="b">已挂起，先做下一步：{card.title}</div><div className="muted mt4">{card.reason}</div></div>
      <Button size="xs" kind="ghost" onClick={() => navigate(pagePath('inbox', pid), { item: card.inbox_item_id || `suspension:${card.suspension_id}` })}>现在回答</Button>
    </div>
  );
}

const SG = { done: ['done', 'bot'], running: ['run', null], queued: ['queue', 'clock'], partial: ['part', 'pause'] };
const STEP_K = { done: 'ok', running: 'running', waiting: 'waiting', failed: 'failed', info: 'info', interrupted: 'interrupted' };

// 进展块：可展开，带版本标签（F8 之前按段跑时每段一块）
function SliceProgressCard({ card }) {
  const [cls, icon] = SG[card.state] || SG.done;
  const segs = sliceSegments(card.timeline);
  const hasBody = card.state !== 'queued' && segs.length > 0;
  const [open, setOpen] = useState(card.state === 'running');
  useEffect(() => { if (card.state === 'running') setOpen(true); }, [card.state]);
  return (
    <details className={cx('g2-sg', cls)} open={hasBody && open}>
      <summary onClick={(e) => { e.preventDefault(); if (hasBody) setOpen(!open); }}>
        <span className="si">{icon ? <Icon name={icon} size={12} /> : <span className="spin" style={{ width: 10, height: 10 }} />}</span>
        <span className="nm">{unitTitle(card.slice?.id, card.slice?.name) || card.title}</span>
        <span className="xs muted grow ell">{card.summary}</span>
        {card.version_label ? <span className="tag">{card.version_label}</span> : null}
        {card.state !== 'queued' ? <Icon name="chevd" size={12} /> : null}
      </summary>
      {hasBody ? (
        <div className="g2-sgb">
          {segs.map((seg) => {
            if (seg.kind === 'auto_decision') return <AutoDecision key={seg.key} card={seg.card} />;
            if (seg.kind === 'suspension') return <Suspended key={seg.key} card={seg.card} />;
            return (
              <div className="tl" key={seg.key}>
                {seg.steps.map((s) => <Tli key={s.id} k={STEP_K[s.state] || 'ok'} t={s.text} m={s.meta} />)}
              </div>
            );
          })}
        </div>
      ) : null}
    </details>
  );
}

function ReportRow({ row, card, multi, scenarios, onRevert, busy }) {
  const { pid } = useContext(WbCtx);
  const tone = row.reverted || row.result === 'skipped' ? 'gray' : row.result === 'partial' || /部分/.test(row.result_text) ? 'amber' : 'indigo';
  return (
    <tr>
      <td><b style={{ fontWeight: 600 }}>{unitTitle(row.slice_id, row.name)}</b></td>
      <td><Chip tone={tone} sm>{row.result_text}</Chip></td>
      <td className="muted">{row.produced_text}</td>
      {scenarios ? <td className="num">{row.scenarios?.total ? `${row.scenarios.passed} / ${row.scenarios.total}` : '—'}</td> : null}
      <td><span className="tag">{row.version_label}</span></td>
      <td>
        <span className="row g4">
          <Button size="xs" onClick={() => navigate(pagePath('versions', pid), { v: row.version_label, run: card.run_id, slice: row.slice_id })}>查看改动</Button>
          <Button size="xs" kind="ghost" disabled={!row.revertible || busy} title={row.revertible ? '回退到运行前，另存新版本' : '不能撤回'} onClick={() => onRevert(row)}>{multi ? '回退这一段' : '回退到运行前'}</Button>
        </span>
      </td>
    </tr>
  );
}

// 运行报告卡：产出汇总、结果（F8 之前按段跑时每段一行）、去复核
function RunReportCard({ card }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const ab = useAutobuildCtx();
  const [busy, setBusy] = useState(false);
  const [chipText, chipTone] = REPORT_CHIP[card.state] || ['已结束', 'gray'];
  const review = ab?.run?.run_id === card.run_id ? (ab.run.needs_review ?? card.review_count) : card.review_count;
  const multi = multiSlice((card.slices || []).map((row) => row.slice_id));
  const scenarios = (card.slices || []).some((row) => row.scenarios?.total);
  const revert = async (row) => {
    setBusy(true);
    try {
      const r = await api.revertAutobuildSlice(pid, card.run_id, row.slice_id);
      const name = sliceName(row.slice_id, row.name);
      toast(name ? `已回退「${name}」，存为 ${r.version_label}` : `已回退到运行前，存为 ${r.version_label}`, 'undo');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const toL2 = async () => {
    if (ab?.run?.run_id === card.run_id && (ab.running || ab.paused)) { ab.act('convert_to_l2'); return; }
    try {
      const r = await api.conversations(pid);
      const c = (r.items || []).find((x) => x.kind !== 'autobuild');
      navigate(pagePath('wb-l2', pid), c ? { c: c.id } : undefined);
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return (
    <CC
      icon="check" title={card.title} chip={<Chip tone={chipTone} sm>{chipText}</Chip>} sub={reportSub(card)}
      foot={(
        <>
          <span className="sp" />
          <Button size="sm" kind="ghost" icon="hand" onClick={toL2}>转为陪伴继续</Button>
          <Button size="sm" kind="primary" icon="inbox" onClick={() => navigate(pagePath('inbox', pid), { filter: 'autobuild_review' })}>{review ? `去复核 ${review} 项` : '去待处理'}</Button>
        </>
      )}
    >
      <div className="g2-rpm">
        {reportMetrics(card).map(([a, b, c]) => <div key={a}><span>{a}</span><b>{b}</b><small>{c}</small></div>)}
      </div>
      <div className="tbl-wrap mt12">
        <table className="tbl compact">
          <thead><tr><th>{multi ? '分段' : ''}</th><th>结果</th><th>产出</th>{scenarios ? <th>场景</th> : null}<th>版本</th><th /></tr></thead>
          <tbody>{(card.slices || []).map((row) => <ReportRow key={row.slice_id} row={row} card={card} multi={multi} scenarios={scenarios} onRevert={revert} busy={busy} />)}</tbody>
        </table>
      </div>
    </CC>
  );
}

export const AUTOBUILD_CARDS = {
  autobuild_start: AutobuildStartCard,
  slice_progress: SliceProgressCard,
  auto_decision: AutoDecision,
  suspension: Suspended,
  run_report: RunReportCard,
};
