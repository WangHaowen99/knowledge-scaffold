// 自主构建（设计稿 l4-run）：工作台里的运行条与运行详情；运行状态来自 AB-05 与项目流（EP-14～EP-17）
import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Drawer, Ev, Icon, St, cx } from '../../ui/index.jsx';
import { multiSlice, unitTitle } from '../../state/flow.js';
import {
  CELL, confidenceColor, hhmm, matrixTone, miniMetrics, runFlags, stripMetrics, stripShown, stripTitle,
} from './autobuildModel.js';
import '../../styles/autobuild.css';

// 卡片里要用到运行状态与控制（运行详情、转为陪伴），由工作台页提供
export const AutobuildCtx = createContext(null);
export const useAutobuildCtx = () => useContext(AutobuildCtx);

const OP = {
  pause: ['pauseAutobuild', '当前步骤做完后暂停', 'pause'],
  stop_at_safe_point: ['stopAutobuildAtSafePoint', '会在下一个安全点停下', 'stop'],
  convert_to_l2: ['convertAutobuildToL2', '到下一个安全点转为陪伴', 'hand'],
  resume: ['resumeAutobuild', '继续运行', 'play'],
  acknowledge: ['acknowledgeAutobuild', '已回到陪伴建模', 'check'],
};

export function useAutobuild({ pid, cid, list }) {
  const { api } = useApp();
  const { subscribe, setMeta } = useProject();
  const convRun = list?.items?.find((c) => c.id === cid)?.run_id || null;
  // 当前对话不是运行对话，但项目里有运行在跑：运行条照样显示
  const [projectRun, setProjectRun] = useState(null);
  const runId = convRun || projectRun;
  const [run, setRun] = useState(null);
  const [drawer, setDrawer] = useState(false);
  const [busy, setBusy] = useState(null);

  const listReady = !!list;
  const [runsTick, setRunsTick] = useState(0);
  useEffect(() => {
    setProjectRun(null);
    if (convRun || !pid || !listReady) return undefined;
    let alive = true;
    api.autobuildRuns(pid, { state: 'running' }).then((r) => { if (alive) setProjectRun(r.items?.[0]?.run_id || null); }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid, convRun, listReady, runsTick]);
  // 别的对话、别的页面开始了运行：重新找一次在跑的运行
  useEffect(() => subscribe((env) => {
    if (!convRun && !projectRun && env.type === 'autobuild.run.updated') setRunsTick((n) => n + 1);
  }), [subscribe, convRun, projectRun]);

  const load = useCallback(async () => {
    if (!runId) { setRun(null); return; }
    setRun(await api.autobuildRun(pid, runId));
  }, [api, pid, runId]);
  useEffect(() => { setRun(null); load().catch(() => setRun(null)); }, [load]);

  useEffect(() => subscribe((env) => {
    const d = env.data || {};
    if (!runId || (d.run_id && d.run_id !== runId)) return;
    switch (env.type) {
      case 'autobuild.run.updated': setRun((r) => (r ? { ...r, ...d } : r)); break;
      case 'autobuild.safe_point': if (d.next !== 'continue') load().catch(() => {}); break;
      case 'autobuild.finished': load().catch(() => {}); break;
      default: break;
    }
  }), [subscribe, runId, load]);

  const act = useCallback(async (op) => {
    if (op === 'details') { setDrawer(true); return; }
    const spec = OP[op];
    if (!spec || !run) return;
    setBusy(op);
    try {
      const r = await api[spec[0]](pid, run.run_id);
      setRun((x) => ({ ...x, ...r }));
      // 确认结束的运行：项目回到陪伴建模（服务端随后发 project.updated，以那份为准）
      if (op === 'acknowledge') setMeta((m) => (m ? { ...m, build_mode: 'L2' } : m));
      toast(spec[1], spec[2]);
      return true;
    } catch (e) {
      toast(errorText(e), 'alert');
      return false;
    } finally {
      setBusy(null);
    }
  }, [api, pid, run, setMeta]);

  const flags = runFlags(run);
  const shown = stripShown(run, !!convRun);
  return {
    run: shown ? run : null,
    ...flags,
    active: shown,
    // 输入框按「补充说明，不打断运行」处理：只在运行对话里
    running: flags.running && !!convRun,
    inConversation: !!convRun,
    drawer,
    openDetails: () => setDrawer(true),
    closeDetails: () => setDrawer(false),
    act,
    busy,
    reload: load,
  };
}

export function Conf({ v, threshold = 0.8 }) {
  const c = confidenceColor(v, threshold);
  return (
    <span className="g2-conf">
      <span className="bar"><i style={{ width: `${Math.round((v || 0) * 100)}%`, background: c }} /></span>
      <b style={{ color: c }}>{(v || 0).toFixed(2)}</b>
    </span>
  );
}

export function AutobuildBar({ ab }) {
  const { pid } = useProject();
  if (!ab?.run) return null;
  const { run } = ab;
  const { title, sub } = stripTitle(run);
  const live = ab.running || (ab.state === 'running' && !ab.inConversation);
  const review = run.needs_review ?? run.decisions ?? 0;
  const btn = (op, icon, label, w) => (
    <button type="button" className={cx('g2-wbtn', w && 'w')} disabled={!!ab.busy} onClick={() => ab.act(op)}><Icon name={icon} size={14} />{label}</button>
  );
  return (
    <>
      <div className="wx-run">
        {/* 别的对话里多一个「回到运行对话」：放不下时指标只留用量（上限在运行详情里） */}
        <div className={cx('l4-hero g2-rs', !ab.inConversation && (ab.state === 'running' || ab.state === 'queued') && 'many')}>
          {live ? <i className="g2-live" /> : <span className="g2-rs-tile"><Icon name={ab.ended ? 'check' : ab.paused ? 'pause' : 'clock'} size={14} /></span>}
          <div className="g2-rs-t"><b>{title}</b><span>{sub}</span></div>
          <div className="g2-rs-m">
            {stripMetrics(run).map(([lb, v, cap, p]) => (
              <div className="m" key={lb}>
                <span>{lb}</span><b>{v}</b>{cap ? <> <small>/ {cap}</small></> : null}
                {p == null ? null : <div className="bar"><i style={{ width: `${p}%` }} /></div>}
              </div>
            ))}
          </div>
          <span className="sp" />
          <button type="button" className="g2-wbtn" onClick={ab.openDetails}><Icon name="list" size={14} />运行详情</button>
          {!ab.inConversation ? (
            <button type="button" className="g2-wbtn" onClick={() => navigate(pagePath('l4-run', pid), { c: run.conversation_id })}><Icon name="msg" size={14} />回到运行对话</button>
          ) : null}
          {ab.state === 'running' || ab.state === 'queued' ? (
            <>
              {btn('pause', 'pause', '暂停')}
              {btn('stop_at_safe_point', 'stop', '停在安全点')}
              {btn('convert_to_l2', 'hand', '转为陪伴', true)}
            </>
          ) : null}
          {ab.paused ? (
            <>
              {btn('stop_at_safe_point', 'stop', '停在安全点')}
              {btn('resume', 'play', '续跑', true)}
            </>
          ) : null}
          {ab.ended && review ? (
            <button type="button" className="g2-wbtn" onClick={() => navigate(pagePath('inbox', pid), { filter: 'autobuild_review' })}>
              <Icon name="inbox" size={14} />去复核 {review} 项
            </button>
          ) : null}
          {ab.ended ? btn('acknowledge', 'check', '确认', true) : null}
        </div>
      </div>
      <RunDrawer ab={ab} />
    </>
  );
}

const REVIEW_CHIP = { accepted: ['已接受', 'gray'], rejudged: ['已改判', 'indigo'] };

function Cell({ k }) {
  const [cls, label] = CELL[k] || ['', k];
  return (
    <div className={cx('g2-cell', cls)}>
      {cls === 'ok' ? <Icon name="check" size={12} /> : cls === 'auto' ? <Icon name="bot" size={12} /> : cls === 'run' ? <span className="spin" style={{ width: 10, height: 10 }} /> : null}
      {label}
    </div>
  );
}

function ResultChip({ row }) {
  const tone = matrixTone(row);
  if (tone === 'running') return <St k="running" sm label={row.result || '进行中'} />;
  return <Chip tone={tone} sm>{row.result}</Chip>;
}

export function RunDrawer({ ab }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [ledger, setLedger] = useState(null);
  const [sus, setSus] = useState(null);
  const run = ab.run;
  const runId = run?.run_id;
  const { drawer, reload } = ab;
  useEffect(() => {
    if (!drawer || !runId) return;
    reload().catch(() => {});
    api.autobuildLedger(pid, runId, {}).then((r) => setLedger(r.items || [])).catch(() => setLedger([]));
    api.autobuildSuspensions(pid, runId).then((r) => setSus((r.items || []).filter((x) => x.state === 'open'))).catch(() => setSus([]));
  }, [api, pid, runId, drawer]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!run) return null;
  // 一个单元一行：现在是一个节点一行（名字 + 结果）；老运行按段跑，才有步骤的列（AB-05 stages），按好几段跑的才叫「分段流水线」
  const keys = (run.stages || []).map((s) => [s.id, s.label]);
  const multi = keys.length > 0 && multiSlice((run.matrix || []).map((row) => row.slice_id));
  const toInbox = (q) => { ab.closeDetails(); navigate(pagePath('inbox', pid), q); };
  return (
    <Drawer
      open={ab.drawer} onClose={ab.closeDetails} icon="rocket" w={820}
      title={`运行详情 · 运行 #${run.run_no}`}
      sub={`${hhmm(run.started_at)} 开始`}
    >
      <div className="g2-mini">
        {miniMetrics(run).map(([b, t]) => <div key={t}><b>{b}</b><span>{t}</span></div>)}
      </div>
      <div className="sec-t mt16">
        <Icon name="layers" size={14} />{multi ? '分段流水线' : '流水线'}
      </div>
      <div className="g2-pipe" style={{ gridTemplateColumns: keys.length ? `150px repeat(${keys.length},1fr) 128px` : '1fr 128px' }}>
        <span />
        {keys.map(([k, t]) => <span key={k} className="h">{t}</span>)}
        <span className="h">结果</span>
        {(run.matrix || []).map((row) => [
          <span key={`${row.slice_id}-n`} className="nm ell">{unitTitle(row.slice_id, row.slice_name)}</span>,
          ...keys.map(([k]) => <Cell key={`${row.slice_id}-${k}`} k={row.cells?.[k]} />),
          <span key={`${row.slice_id}-r`}><ResultChip row={row} /></span>,
        ])}
      </div>
      <div className="sec-t mt16">
        <Icon name="book" size={14} />自动决策台账<span className="sp" />
        <Button size="xs" kind="ghost" onClick={() => toInbox({ filter: 'autobuild_review' })}>全部在待处理里复核</Button>
      </div>
      {ledger == null ? <div className="xs muted">正在读取…</div> : ledger.length ? (
        <div className="tbl-wrap">
          <table className="tbl compact">
            <thead><tr><th>时间</th><th>问题</th><th>采用</th><th>置信度</th><th>依据</th><th /></tr></thead>
            <tbody>
              {ledger.map((l) => (
                <tr key={l.id}>
                  <td className="muted num">{hhmm(l.at)}</td>
                  <td>{l.question}</td>
                  <td><b style={{ fontWeight: 600 }}>{l.chosen}</b>{l.assumption ? <> <Chip tone="amber" sm>假设</Chip></> : null}</td>
                  <td><Conf v={l.confidence} threshold={l.threshold} /></td>
                  <td>{l.anchors?.[0] ? <Ev anchor={l.anchors[0].anchor} label={l.anchors[0].label} /> : null}</td>
                  <td>
                    {REVIEW_CHIP[l.review_state]
                      ? <Chip tone={REVIEW_CHIP[l.review_state][1]} sm>{REVIEW_CHIP[l.review_state][0]}</Chip>
                      : <Button size="xs" kind="ghost" onClick={() => toInbox({ entry: l.id })}>改判</Button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="xs muted">还没有自动决定</div>}
      <div className="sec-t mt16"><Icon name="pause" size={14} />挂起的问题 {sus?.length ?? ''}</div>
      {(sus || []).map((x) => (
        <div className="g2-ck" key={x.id}>
          <span className="ic warn"><Icon name="pause" size={12} /></span>
          <div className="grow"><div className="b" style={{ fontWeight: 600 }}>{x.title.replace(/[？?]$/, '')}</div><div className="xs muted ell">{x.reason}</div></div>
          <Button size="xs" kind="ghost" onClick={() => toInbox({ item: `suspension:${x.id}` })}>现在回答</Button>
        </div>
      ))}
      {sus && !sus.length ? <div className="xs muted">没有挂起的问题</div> : null}
    </Drawer>
  );
}
