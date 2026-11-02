// 增量更新的对话卡片（设计稿 incremental：变更点清单、影响面、回归、外部反馈、执行后的「这一轮改了什么」），在工作台对话里走完
// 契约 4.7 change_list / impact / regression / external_feedback / round_changes（带 change_list_id 的那种）。
// 清单弹窗、确认清单并执行由工作台注入（useActions 的 openChangeList / executeChangeList）；没有宿主的页面跳到 incremental。
import { Fragment, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { roundLockNote } from '../../state/readonly.js';
import { Button, Chip, Ev, Icon, St, Table } from '../../ui/index.jsx';
import { PluginEyebrow, useProjectEvent } from '../plugin/parts.jsx';
import { useActions } from './cards.jsx';
import { CC, DiffBlock, Inline, WbCtx, Wl } from './parts.jsx';
import * as M from './incremental/model.js';
import '../../styles/more.css';
import '../../styles/incremental.css';

function useOpenList(id) {
  const { openChangeList } = useActions();
  const { pid } = useContext(WbCtx);
  return useCallback(() => {
    if (openChangeList) openChangeList(id);
    else navigate(pagePath('incremental', pid), { dialog: 'change_list', change_list_id: id });
  }, [openChangeList, pid, id]);
}

function useExecute(id) {
  const { executeChangeList } = useActions();
  const { pid } = useContext(WbCtx);
  return useCallback(() => (executeChangeList ? executeChangeList(id) : navigate(pagePath('incremental', pid))), [executeChangeList, pid, id]);
}

// 把握：高 / 中 / 低（低的悬停看原因）；已接受、已改写、需你确认跟在后面
export function Confidence({ p, mark = true }) {
  const [label, tone] = M.CONFIDENCE[p.confidence] || ['—', 'gray'];
  const chip = p.confidence === 'low'
    ? <span data-pop={M.lowPop(p)}><Chip tone="red" sm icon="alert">{label}</Chip></span>
    : <Chip tone={tone} sm>{label}</Chip>;
  let tail = null;
  if (mark && M.isDone(p)) tail = <Chip tone="blue" sm icon="check">{p.state === 'rewritten' ? '已改写' : '已接受'}</Chip>;
  else if (mark && M.isLowPending(p)) tail = <Chip tone="amber" sm>需你确认</Chip>;
  return tail ? <div className="row g4 inc-conf">{chip}{tail}</div> : chip;
}

// ---------- 变更点清单卡：只读表格，双击打开弹窗逐条改 ----------
function ChangeListCard({ card }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const id = card.change_list_id;
  const [live, setLive] = useState(null);
  const [busy, setBusy] = useState(false);
  const open = useOpenList(id);
  const load = useCallback(() => api.changeList(pid, id).then(setLive).catch(() => {}), [api, pid, id]);
  // 卡片状态变了（执行、撤回）或清单在别处改过（EP-18）都重取一次，接受状态以 INC-01 为准
  useEffect(() => { load(); }, [load, card.state]);
  useProjectEvent('change_list.updated', (d) => { if (d.change_list_id === id) load(); });

  const cl = { id, source: card.source, state: card.state, points: live?.points || card.rows || [] };
  const executed = cl.state === 'executed';
  const high = M.highPending(cl.points);
  const acceptHigh = async () => {
    setBusy(true);
    try {
      setLive(await api.acceptChangePoints(pid, id, { high_confidence: true }));
      toast(`已接受 ${high.length} 条高把握的变更点`, 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
      load();
    } finally {
      setBusy(false);
    }
  };
  return (
    <CC
      id={`inc-cl-${id}`} icon="list" title={card.title || '变更点清单'} chip={<Chip tone="line" sm>{cl.points.length} 条</Chip>}
      sub={M.listSubtitle(cl)} dbl onDoubleClick={open}
      foot={executed ? (
        <><span className="xs muted grow">清单已确认并执行；双击打开查看</span><Button size="xs" icon="eye" onClick={open}>查看清单</Button></>
      ) : (
        <>
          <span className="xs muted grow">双击打开，逐条修改条件、取值和范围</span>
          <Button size="xs" kind="soft" icon="check" disabled={busy || !high.length} onClick={acceptHigh}>接受高把握项 {high.length}</Button>
          <Button size="xs" icon="edit" onClick={open}>逐条修改</Button>
        </>
      )}
    >
      <Table
        compact className="mr-ct"
        cols={[{ t: '#', w: '24px' }, '条件', '取值', { t: '范围', w: '118px' }, { t: '出处', w: '64px' }, { t: '把握', w: '92px' }]}
        rowClassName={(i) => (cl.points[i]?.confidence === 'low' ? 'mr-low' : undefined)}
        rows={cl.points.map((p) => {
          const c = M.pointCells(p);
          return [p.n, c.condition, c.value, c.scope, p.anchor ? <Ev anchor={p.anchor} label={M.shortAnchor(p.anchor_label)} /> : <span className="xs faint">{p.anchor_label || '—'}</span>, <Confidence p={p} />];
        })}
      />
    </CC>
  );
}

// ---------- 影响面卡 ----------
function ImpactCard({ card }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const id = card.change_list_id;
  const [live, setLive] = useState(null);
  const [busy, setBusy] = useState(false);
  const execute = useExecute(id);
  // 清单保存后影响面重算（EP-18），取 INC-05
  useProjectEvent('change_list.updated', (d) => {
    if (d.change_list_id === id) api.changeListImpact(pid, id).then(setLive).catch(() => {});
  });
  const imp = live ? { ...card, ...live } : card;
  const reg = imp.regression || { affected: [], guard: [] };
  const run = async () => {
    setBusy(true);
    try { await execute(); } finally { setBusy(false); }
  };
  return (
    <CC
      icon="target" title={card.title || '影响面'} chip={<Chip tone="blue" sm icon="zap">{M.secs(imp.duration_ms)}</Chip>} sub={M.phasesText(imp.phases)}
      foot={(
        <>
          <span className="xs muted grow">{imp.semantic_scope_text}</span>
          {card.executed
            ? <Button size="sm" kind="primary" icon="check" disabled>已确认并执行</Button>
            : <Button size="sm" kind="primary" icon="play" disabled={busy} onClick={run}>确认清单并执行</Button>}
        </>
      )}
    >
      <div className="mr-imp">
        <div>
          <div className="mr-sec" style={{ marginTop: 0 }}>将标为过期的产物 {(imp.stale || []).length}</div>
          {(imp.stale || []).map((s, i) => (
            <div className="r" key={i}>
              <Icon name={M.KIND_ICON[s.target?.kind] || 'layers'} size={14} />
              <span className="muted xs">{M.KIND_LABEL[s.target?.kind] || '产物'}</span>
              <Wl name={M.refName(s.target)} />
              {s.note ? <span className="xs faint">{s.note}</span> : null}
              <span className="sp" />
              <St k="stale" sm label="将过期" />
            </div>
          ))}
        </div>
        <div>
          <div className="mr-sec" style={{ marginTop: 0 }}>回归场景 {reg.affected.length + reg.guard.length}</div>
          <div className="xs muted mb8">受影响的旧场景 {reg.affected.length}</div>
          <div className="row wrap g4">{reg.affected.map((s) => <Chip key={s.key} tone="dash" sm>{M.refName(s)}</Chip>)}</div>
          <div className="xs muted mb8 mt12">守护老路径的场景 {reg.guard.length} · 变更没碰到的分支也跑一遍</div>
          <div className="row wrap g4">{reg.guard.map((s) => <Chip key={s.key} tone="line" sm icon="shield">{M.refName(s)}</Chip>)}</div>
        </div>
      </div>
    </CC>
  );
}

// ---------- 回归结果卡：受影响 / 守护老路径；不通过的行「交给助手修」 ----------
function RegressionCard({ card }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const { conversationId } = useActions();
  const [sent, setSent] = useState(() => new Set());
  const sum = M.regressionSummary(card);
  const fix = async (r) => {
    try {
      await api.sendCommand(pid, M.fixCommand(card.change_list_id, r.scenario, conversationId));
      setSent((s) => new Set(s).add(r.scenario.key));
      toast('已交给助手，修完会重跑这个场景', 'sparkle');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return (
    <CC
      icon="flask" title={card.title || '回归结果'} chip={<Chip tone={sum.failed ? 'amber' : 'teal'} sm>{sum.passed} / {sum.total} 通过</Chip>}
      sub={`受影响的旧场景 ${sum.affected} 个 + 守护老路径的场景 ${sum.guard} 个`}
    >
      {(card.rows || []).map((r) => (
        <Fragment key={r.scenario.key}>
          <div className="mr-rg">
            <Icon name="flask" size={14} />
            <span className="grow">{M.refName(r.scenario)}</span>
            <span className="xs muted">{r.type === 'guard' ? '守护老路径' : '受影响'}</span>
            <St k={r.result === 'fail' ? 'fail' : 'pass'} sm />
          </div>
          {r.result === 'fail' ? (
            <div className="mr-why">
              <span style={{ color: 'var(--red)', display: 'flex' }}><Icon name="alert" size={14} /></span>
              <span className="grow"><Inline text={r.reason || ''} /></span>
              <Button size="xs" kind="soft" icon="sparkle" disabled={sent.has(r.scenario.key)} onClick={() => fix(r)}>{sent.has(r.scenario.key) ? '已交给助手' : '交给助手修'}</Button>
            </div>
          ) : null}
        </Fragment>
      ))}
    </CC>
  );
}

// ---------- 外部反馈卡（反馈演进插件）：「让助手分析」发命令，「稍后处理」留在待处理 ----------
function ExternalFeedbackCard({ card }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const { conversationId } = useActions();
  const [done, setDone] = useState(null);
  // R-11：这份反馈在别处（待处理、资料区）已经处理过的，卡上直接显示
  useEffect(() => {
    let alive = true;
    api.feedback(pid, {}).then((r) => {
      const f = (r.items || []).find((x) => x.id === card.feedback_id);
      if (alive && f && f.state !== 'open') setDone(f.state === 'handled' ? `已处理${f.handled_version_label ? ` · 版本 ${f.handled_version_label}` : ''}` : '已忽略');
    }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid, card.feedback_id]);
  const items = card.items || [];
  const todo = M.feedbackToAnalyze(card);
  const analyze = async () => {
    const ns = todo.map((x) => x.i).join('、');
    try {
      await api.sendCommand(pid, {
        command: '/ontology-evolution', args: { feedback_id: card.feedback_id, case_ids: todo.map((x) => x.case_id) },
        text: `分析外部反馈的第 ${ns} 条失分用例`, origin: { page: 'wb-l2', label: '外部反馈' }, ...(conversationId ? { conversation_id: conversationId } : {}),
      });
      setDone('已交给助手分析');
      toast(`已交给助手分析第 ${ns} 条反馈`, 'sparkle');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const later = () => {
    setDone('留在待处理里');
    toast('这份反馈留在「待处理 › 外部反馈」里，随时可以处理', 'inbox');
  };
  return (
    <CC
      icon="inbox" tone="indigo" title={card.title} chip={card.plugin ? <PluginEyebrow pack={card.plugin.name} label={card.plugin.label} /> : null}
      sub={card.source ? M.feedbackSource(card.source) : card.subtitle}
      foot={(
        <>
          <span className="xs muted grow">反馈也记入了资料区的「外部反馈」</span>
          {done ? <Chip tone="gray" sm icon="check">{done}</Chip> : (
            <>
              <Button size="xs" kind="ghost" onClick={later}>稍后处理</Button>
              <Button size="xs" kind="soft" icon="sparkle" disabled={!todo.length} onClick={analyze}>让助手分析</Button>
            </>
          )}
        </>
      )}
    >
      <Table
        compact className="mr-ct" cols={['问题', '失分原因', '涉及产物', '']}
        rows={items.map((it) => [
          it.question, it.loss_reason || '—',
          <span className="row wrap g4">{(it.artifacts || []).map((a, i) => <Wl key={i} name={M.refName(a)} />)}</span>,
          it.related ? <Chip tone="blue" sm>{it.related}</Chip> : '',
        ])}
      />
    </CC>
  );
}

// ---------- 执行后的「这一轮改了什么」：逐行改动、查看改动（CS-03）、撤回（CS-05）、确认生效（CS-04） ----------
function IncrementalRoundCard({ card, scope }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const actions = useActions();
  const [diff, setDiff] = useState(null);
  const [showDiff, setShowDiff] = useState(false);
  const [busy, setBusy] = useState(false);
  const [closed, setClosed] = useState(null);
  const ref = useRef(null);
  // 在待处理等别处确认、撤回时（EP-04），卡片跟着变
  useProjectEvent('changeset.updated', (d) => {
    if (d.changeset_id === card.changeset_id && (d.state === 'merged' || d.state === 'discarded')) setClosed(d);
  });
  const state = card.state === 'pending_confirm' && closed ? closed.state : card.state;
  const merged = state === 'merged';
  const discarded = state === 'discarded';
  const pending = !merged && !discarded;
  const mine = scope && scope.changeset_id === card.changeset_id;
  const running = mine && scope.state === 'assistant_running';

  const toggleDiff = async () => {
    if (!showDiff && !diff) {
      try {
        const r = await api.changesetDiff(pid, card.changeset_id);
        setDiff(r.items.flatMap((it) => [
          { kind: 'header', text: `${M.KIND_LABEL[it.target?.kind] || '产物'} ${M.refName(it.target)}` },
          ...it.sections.flatMap((s) => s.lines),
        ]));
      } catch (e) {
        toast(errorText(e), 'alert');
        return;
      }
    }
    setShowDiff((v) => !v);
  };
  useEffect(() => actions.registerRound?.(card.changeset_id, () => {
    if (!showDiff) toggleDiff();
    ref.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }));
  const run = async (fn) => {
    setBusy(true);
    try { await fn(); } finally { setBusy(false); }
  };
  const note = M.remainingNote({ ...card, state });
  return (
    <div ref={ref}>
      <CC
        icon={discarded ? 'undo' : 'edit'} title={card.title || '这一轮改了什么'} sub={card.subtitle}
        foot={(
          <>
            <Button size="sm" icon="eye" onClick={toggleDiff}>{showDiff ? '收起改动' : '查看改动'}</Button>
            {pending ? <Button size="sm" kind="ghost" icon="undo" disabled={busy || running} onClick={() => run(() => actions.discard(card.changeset_id))}>撤回这一轮</Button> : null}
            <span className="sp" />
            {note && !discarded ? <span className="xs muted">{note}</span> : null}
            {merged ? <Chip tone="blue" icon="check">已确认生效 · 版本 {card.merged_version_label || closed?.version_label}</Chip>
              : discarded ? <span className="xs faint">已撤回这一轮，主线没有变化</span>
                : (
                  <span title={running ? '助手还在跑，这一步结束后才能确认' : `确认后保存为版本 ${card.next_version_label}`}>
                    <Button size="sm" kind="primary" icon="merge" disabled={busy || running} onClick={() => run(() => actions.merge(card.changeset_id))}>确认生效</Button>
                  </span>
                )}
          </>
        )}
      >
        <ul className="mr-done">
          {M.roundLines(card).map((l, i) => <li key={i}><Icon name={l.icon} size={14} /><Inline text={`${l.verb} ${l.text}`} /></li>)}
        </ul>
        {pending ? <div className="xs row g4 inc-lock"><Icon name="lock" size={12} />{mine ? roundLockNote(scope) : card.lock_note || roundLockNote(null)}</div> : null}
        {showDiff && diff ? <DiffBlock lines={diff} style={{ marginTop: 10 }} /> : null}
      </CC>
    </div>
  );
}

export const INCREMENTAL_CARDS = {
  change_list: ChangeListCard,
  impact: ImpactCard,
  regression: RegressionCard,
  external_feedback: ExternalFeedbackCard,
  // 带 change_list_id 的「这一轮」按设计稿 incremental 的样子画（cards.jsx 的 CardItem 按它分流）
  'round_changes.incremental': IncrementalRoundCard,
};
