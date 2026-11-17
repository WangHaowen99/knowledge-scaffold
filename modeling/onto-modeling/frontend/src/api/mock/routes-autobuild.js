// 3.22 自主构建（AB-01～AB-19）
// 阶段 l4：主示例项目 ?mock=stage:l4 进入运行 #3（设计稿 l4-run），?mock=stage:l4,run:ended 是运行结束；
// 共享单车故障诊断默认就在这一阶段。主示例项目在其他阶段时，运行 #3 已经跑完（待处理里的复核来自它）。
import { RIDER_PID, riderReport, riderSummary } from './inbox-data.js';
import * as R from './run3.js';
import * as S from './settings-data.js';

let SC = {};
const stageRunState = () => (SC.run === 'ended' ? 'completed' : 'running');
const RUNNING = new Set(['queued', 'running', 'pausing', 'stopping', 'converting']);

const SLICE_FLOW = {
  running: {
    s2: { group: 'done', note: '自主完成 · 1 项需复核', stages: ['confirmed', 'confirmed', 'needs_review', 'needs_review', 'needs_review'] },
    s3: { group: 'done', note: '自主完成 · 已刷新过期 3 项', stages: ['confirmed', 'needs_review', 'needs_review', 'needs_review', 'needs_review'] },
    s4: { group: 'now', note: '自主构建正在做 · 规则 2 / 3', stages: ['needs_review', 'needs_review', 'running', 'not_ready', 'not_ready'] },
    s5: { group: 'todo', note: '排队中 · 下一段', stages: ['not_ready', 'not_ready', 'not_ready', 'not_ready', 'not_ready'] },
  },
  completed: {
    s2: { group: 'done', note: '自主完成 · 1 项需复核', stages: ['confirmed', 'confirmed', 'needs_review', 'needs_review', 'needs_review'] },
    s3: { group: 'done', note: '自主完成 · 已刷新过期 3 项', stages: ['confirmed', 'needs_review', 'needs_review', 'needs_review', 'needs_review'] },
    s4: { group: 'wait', note: '部分完成 · 挂起 2 项', stages: ['needs_review', 'needs_review', 'needs_review', 'needs_review', 'stale'] },
    s5: { group: 'wait', note: '自主完成 · 挂起 1 项', stages: ['needs_review', 'needs_review', 'needs_review', 'needs_review', 'stale'] },
  },
};

function l4Flow(base, state) {
  const map = SLICE_FLOW[state === 'completed' ? 'completed' : 'running'];
  const keys = base.slice_stages.map((s) => s.id);
  return {
    ...base,
    nodes: base.nodes.map((n) => (n.id === 'core.model' && state === 'completed'
      ? { ...n, state: 'confirmed', display_state: 'confirmed', flags: { ...n.flags, needs_review: true }, progress: null }
      : n)),
    slices: base.slices.map((s) => {
      const x = map[s.id];
      if (!x) return s;
      return { ...s, group: x.group, note: x.note, stages: Object.fromEntries(keys.map((k, i) => [k, x.stages[i]])) };
    }),
  };
}

export const stages = {
  l4: {
    conversations: () => [R.runConversation(stageRunState()), ...[
      { id: 'c7a1e2f03b44', title: '超量与审批提示', title_manual: true },
      { id: 'b2d94c11e0a7', title: '陈列量核定返工', title_manual: true },
    ].map((c, i) => ({ ...c, created_at: new Date(Date.now() - (i + 1) * 86400000).toISOString(), updated_at: new Date(Date.now() - (i + 1) * 86400000).toISOString(), live: false, state: 'closed', kind: 'chat', run_id: null }))],
    history: () => R.history(stageRunState()),
    flow: (base) => l4Flow(base, stageRunState()),
  },
};
export const projectStages = { 'bike-fleet-7b20e4a1': 'l4' };

const summaryOf = (run) => {
  const { run_id, run_no, state, current, elapsed_ms, est_total_ms, tokens, cost, slices, decisions, suspended, needs_review, started_at, ended_at, acknowledged, pause_reason } = run;
  return { run_id, run_no, state, current, elapsed_ms, est_total_ms, tokens, cost, slices, decisions, suspended, needs_review, started_at, ended_at, acknowledged, pause_reason };
};

export function register(ctx) {
  const { on, json, err, db, sc, F, stageOf, emitConv, emitProj, setSession, setScope, scopeOf, cardOf, uid } = ctx;
  SC = sc;
  // 运行 #3 的出处补进 EV-29 的解析表
  Object.assign(F.ANCHOR_RESOLUTIONS, R.AB_RESOLUTIONS);

  db.autobuild = new Map();
  const storeOf = (pid) => {
    if (!db.autobuild.has(pid)) {
      let st = null;
      if (stageOf(pid) === 'l4') st = stageRunState();
      else if (pid === F.MAIN_PID) st = 'completed';
      db.autobuild.set(pid, st ? R.createRunStore(st) : null);
    }
    return db.autobuild.get(pid);
  };
  db.autobuildOf = storeOf;

  // 运行中：正在做的那一段在写入范围锁里；项目卡跟着显示自主构建中
  [F.MAIN_PID, 'bike-fleet-7b20e4a1'].forEach((pid) => {
    if (stageOf(pid) !== 'l4' || stageRunState() !== 'running') return;
    db.scope.set(pid, R.sliceLock());
  });
  if (stageOf(F.MAIN_PID) === 'l4') {
    const c = cardOf(F.MAIN_PID);
    const run = storeOf(F.MAIN_PID).run();
    if (c) {
      c.inbox_count = db.inboxCountOf?.(F.MAIN_PID) ?? c.inbox_count;
      if (run.state === 'running') {
        Object.assign(c, {
          status: ['active', 'autobuild'], landing_page: 'l4-run',
          autobuild: { run_id: run.run_id, run_no: run.run_no, state: 'running', slice_done: 0, slice_total: 1, progress_pct: 45, eta_ms: 46 * 60000, decisions: run.decisions, suspended: run.suspended, needs_review: 0, duration_ms: run.elapsed_ms },
        });
      }
    }
  }

  const runOr404 = (pid, runId) => {
    const st = storeOf(pid);
    return st && st.run().run_id === runId ? st : null;
  };
  const emitRun = (pid, st) => emitProj(pid, 'autobuild.run.updated', summaryOf(st.run()));
  const emitInbox = (pid) => db.emitInbox?.(pid);
  const cidOf = () => R.RUN_CONV_ID;
  const notice = (pid, text, kind = 'info') => emitConv(pid, cidOf(), 'notice', { kind, text, version_label: null, changeset_id: null, align: 'center' });
  const timers = new Map();
  const after = (pid, ms, fn) => { clearTimeout(timers.get(pid)); timers.set(pid, setTimeout(fn, ms)); };

  // 停在安全点 / 转为陪伴：这一段做完生效后停下
  function finishAtSafePoint(pid, st, next) {
    st.state = next;
    emitProj(pid, 'autobuild.safe_point', { run_id: R.RUN_ID, slice_id: 's4', slice_name: R.RUN_SLICES.s4.name, version_label: 'v0.17', next });
    emitConv(pid, cidOf(), 'card.updated', R.sliceCard('s4', 'partial'));
    emitConv(pid, cidOf(), 'card.updated', R.sliceCard('s5', 'skipped'));
    if (next === 'converted') {
      notice(pid, '已转为陪伴：「临期与损耗检查」已完成（v0.17），剩下的「超量与审批提示」改成逐个确认');
    } else {
      notice(pid, '已在安全点停下：「临期与损耗检查」已完成，保存为版本 v0.17');
    }
    emitConv(pid, cidOf(), 'card', R.reportCard('stopped'));
    emitConv(pid, cidOf(), 'suggestions.updated', next === 'converted'
      ? { mode: 'normal', primary: { id: 'sg-ab-next', label: '继续做「超量与审批提示」', icon: 'play', primary: true, disabled: false, reason: '', priority: 3, action: { type: 'send', text: '继续做「超量与审批提示」' } }, secondary: [{ id: 'sg-ab-review', label: '去复核 10 项', icon: 'inbox', primary: false, disabled: false, reason: '', priority: 3, action: { type: 'navigate', page: 'inbox', params: { filter: 'autobuild_review' } } }], all_commands: true }
      : R.suggestions('stopped'));
    setSession(pid, cidOf(), 'idle');
    if (scopeOf(pid).state === 'autobuild_slice') setScope(pid, { ...scopeOf(pid), state: 'none', entries: [], artifacts: [], paths: [] });
    emitRun(pid, st);
    emitProj(pid, 'autobuild.finished', { run_id: R.RUN_ID, run_no: R.RUN_NO, state: next, report_available: true });
    emitInbox(pid);
  }

  // 策略按项目存（项目设置页保存后，预览、开始、l4-run 的策略说明都按它）；AB-04 起的新运行停在排队中
  db.abPolicy = new Map();
  db.abStarted = new Map();
  const policyOf = (pid) => {
    if (!db.abPolicy.has(pid)) db.abPolicy.set(pid, { ...R.policy(), revision: 'p3' });
    return db.abPolicy.get(pid);
  };
  const lastRunOf = (pid) => {
    const st = storeOf(pid);
    if (!st || RUNNING.has(st.state)) return null;
    const run = st.run();
    return { run_id: run.run_id, run_no: run.run_no, ended_at: run.ended_at, duration_ms: run.elapsed_ms, reviewed: run.needs_review === 0 };
  };
  db.autobuildActiveOf = (pid) => {
    const st = storeOf(pid);
    if (st && RUNNING.has(st.state)) return summaryOf(st.run());
    return db.abStarted.get(pid) || null;
  };

  on('GET', '/projects/([^/]+)/autobuild/policy', ({ m }) => json(200, {
    ...policyOf(m[1]),
    platform_limits: {},
    not_enforced: [],
    last_run: lastRunOf(m[1]),
    optional_nodes: S.OPTIONAL_NODES,
    until_order: S.untilOrder(policyOf(m[1])),
  })); // AB-01
  on('PUT', '/projects/([^/]+)/autobuild/policy', ({ m, body }) => {
    const cur = policyOf(m[1]);
    if (body.base_revision && body.base_revision !== cur.revision) return err(409, 'revision_conflict', '策略已被修改', { current_revision: cur.revision });
    const next = S.mergePolicy(cur, body);
    next.scope = S.normalizeScope(next.scope);
    const bad = S.checkPolicy(next);
    if (bad) return err(...bad);
    next.revision = `p${uid()}`;
    db.abPolicy.set(m[1], next);
    return json(200, { ...next, platform_limits: {}, not_enforced: [], last_run: lastRunOf(m[1]), optional_nodes: S.OPTIONAL_NODES, until_order: S.untilOrder(next) });
  }); // AB-02
  on('POST', '/projects/([^/]+)/autobuild/runs/preview', ({ m, body }) => {
    const pid = m[1];
    const active = db.autobuildActiveOf(pid);
    if (active) return err(409, 'autobuild_running', '自主构建运行中', { run_id: active.run_id });
    const policy = S.mergePolicy(policyOf(pid), body.policy_overrides);
    policy.scope = S.normalizeScope(policy.scope);
    const bad = S.checkPolicy(policy);
    if (bad) return err(...bad);
    const pv = S.previewOf(policy, ctx.flowOf(pid));
    if (!pv.slices.some((s) => s.included)) return err(422, 'invalid_argument', '没有要做的步骤', { fields: { scope: 'empty' } });
    const token = `pv-${uid()}`;
    db.abPreview = { token, pid, policy };
    return json(200, { preview_token: token, expires_at: new Date(Date.now() + 10 * 60000).toISOString(), ...pv });
  }); // AB-03
  on('POST', '/projects/([^/]+)/autobuild/runs', ({ m, body }) => {
    const pid = m[1];
    const active = db.autobuildActiveOf(pid);
    if (active) return err(409, 'autobuild_running', '自主构建运行中', { run_id: active.run_id });
    if (!body.preview_token) return err(410, 'preview_expired', '预览已过期');
    const policy = db.abPreview?.token === body.preview_token ? db.abPreview.policy : policyOf(pid);
    const pv = S.previewOf(policy, ctx.flowOf(pid));
    const included = pv.slices.filter((s) => s.included);
    const n = (storeOf(pid)?.run().run_no || 0) + 1;
    const run = S.queuedRun(n, included.length);
    // 运行在自己的对话里进行（契约 3.22）：开一条「自主构建 · 运行 #n」，先放开始卡和排队提示
    const list = ctx.convsOf(pid);
    const now = new Date().toISOString();
    const conv = { id: uid().padStart(12, '0').slice(-12), title: `自主构建 · 运行 #${n}`, title_manual: false, created_at: now, updated_at: now, live: true, state: 'idle', kind: 'autobuild', run_id: run.run_id };
    list.forEach((c) => { c.live = false; });
    list.unshift(conv);
    const ev = (id, type, data) => ({ id: String(id), type, ts: now, project_id: pid, conversation_id: conv.id, data });
    ctx.hub.seed(ctx.convKey(pid, conv.id), 'conversation', [
      ev(1, 'day.marker', { label: '今天' }),
      ev(2, 'card', {
        ...R.startCard(), card_id: `ab${n}-start`, title: `自主构建 · 运行 #${n}`, run_id: run.run_id, run_no: n,
        chips: [
          { label: '范围', value: `${included.length} 步` }, { label: '拿不准时', value: pv.below_threshold_text },
        ],
      }),
      ev(3, 'notice', { kind: 'info', text: `运行 #${n} 已排队，轮到时自动开始`, version_label: null, changeset_id: null, align: 'center' }),
    ]);
    db.abStarted.set(pid, run);
    db.abPreview = null;
    emitProj(pid, 'conversation.updated', { conversation: conv, deleted: false });
    emitProj(pid, 'autobuild.run.updated', run);
    return json(201, {
      ...run, conversation_id: conv.id, policy, checks: { errors: 0, warnings: 0 }, scenarios: { passed: 0, total: 0 }, fix_rounds: { used: 0, limit: policy.advanced.max_fix_rounds },
      matrix: included.map((s) => ({ slice_id: s.id, slice_name: s.name, state: 'queued', cells: {}, result: '排队' })), stages: [],
      tail_text: '', scope_lock_text: '', recovered_from_checkpoint: 0, report_available: false, base_version_label: cardOf(pid)?.version_label || null,
    });
  }); // AB-04
  on('GET', '/projects/([^/]+)/autobuild/runs', ({ m, query }) => {
    const pid = m[1];
    const items = [];
    if (db.abStarted.has(pid)) items.push(db.abStarted.get(pid));
    const st = storeOf(pid);
    if (st) items.push(summaryOf(st.run()));
    if (pid === RIDER_PID) {
      const s = riderSummary();
      items.push({ run_id: s.run_id, run_no: s.run_no, state: s.state, current: null, elapsed_ms: 112 * 60000, est_total_ms: null, tokens: { used: 3800000 }, cost: { used: s.cost }, slices: { done: 5, total: 6 }, decisions: 13, suspended: 2, needs_review: 2, started_at: s.started_at, ended_at: s.ended_at, acknowledged: false, pause_reason: null });
    }
    const want = query.get('state');
    const filtered = want === 'running' ? items.filter((r) => RUNNING.has(r.state)) : want ? items.filter((r) => r.state === want) : items;
    return json(200, { items: filtered, next_cursor: null });
  }); // AB-18
  on('GET', '/projects/([^/]+)/autobuild/runs/([^/]+)/ledger', ({ m, query }) => {
    const st = runOr404(m[1], m[2]);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    let items = st.ledger();
    const rs = query.get('review_state');
    if (rs) items = items.filter((l) => l.review_state === rs);
    if (query.get('below_threshold') === 'true') items = items.filter((l) => l.confidence < l.threshold);
    const all = st.ledger();
    return json(200, { items, next_cursor: null, counts: { total: all.length, high_confidence: all.filter((l) => l.confidence >= l.threshold).length, below_threshold: all.filter((l) => l.confidence < l.threshold).length, assumptions: all.filter((l) => l.assumption).length } });
  }); // AB-10
  on('GET', '/projects/([^/]+)/autobuild/runs/([^/]+)/suspensions', ({ m }) => {
    const st = runOr404(m[1], m[2]);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    return json(200, { items: st.suspensions() });
  }); // AB-14
  on('GET', '/projects/([^/]+)/autobuild/runs/([^/]+)/report', ({ m }) => {
    if (m[1] === RIDER_PID && m[2] === 'run-2') return json(200, riderReport());
    const st = runOr404(m[1], m[2]);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    if (!st.run().report_available) return err(409, 'autobuild_running', '运行还没结束，报告在结束后生成', { run_id: m[2] });
    return json(200, st.report());
  }); // AB-16
  on('POST', '/projects/([^/]+)/autobuild/runs/([^/]+)/ledger/accept-high-confidence', ({ m }) => {
    const st = runOr404(m[1], m[2]);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    const n = st.acceptHigh();
    emitRun(m[1], st);
    emitInbox(m[1]);
    return json(200, { accepted: n, threshold: R.THRESHOLD });
  }); // AB-13
  on('POST', '/projects/([^/]+)/autobuild/runs/([^/]+)/slices/([^/]+)/revert', ({ m }) => {
    const [pid, runId, sliceId] = m.slice(1);
    if (pid === RIDER_PID && runId === 'run-2') return json(200, { commit: `r2r${uid()}`, version_label: 'v0.10', reverted_to_version_label: 'v0.7' });
    const st = runOr404(pid, runId);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    if (RUNNING.has(st.state)) return err(409, 'autobuild_running', '本次运行还在跑', { run_id: runId });
    const row = st.report().slices.find((x) => x.slice_id === sliceId);
    if (!row || !row.revertible) return err(409, 'slice_not_revertible', '这一段不能撤回', { reason: row?.reverted ? 'already_reverted' : 'not_merged' });
    const r = st.revert(sliceId);
    const c = cardOf(pid);
    if (c) c.version_label = r.version_label;
    emitProj(pid, 'workspace.changed', { commit: r.commit, version_label: r.version_label, source: 'revert', changeset_id: null, by: 'zhangsan', entries: [], artifacts: [], paths: [] });
    emitConv(pid, cidOf(), 'card.updated', { ...R.reportCard(st.state === 'completed' ? 'completed' : 'stopped'), slices: st.report().slices });
    emitRun(pid, st);
    emitInbox(pid);
    return json(200, r);
  }); // AB-17
  on('GET', '/projects/([^/]+)/autobuild/runs/([^/]+)', ({ m }) => {
    const st = runOr404(m[1], m[2]);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    return json(200, st.run());
  }); // AB-05

  const control = (op) => ({ m }) => {
    const pid = m[1];
    const st = runOr404(pid, m[2]);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    if (op === 'resume') {
      if (!['paused', 'interrupted'].includes(st.state)) return err(409, 'autobuild_not_running', '运行不在可以续跑的状态', { state: st.state });
      st.state = 'running';
      emitConv(pid, cidOf(), 'card.updated', R.sliceCard('s4', 'running'));
      setSession(pid, cidOf(), 'running');
      notice(pid, '继续运行：从「临期与损耗检查 › 提取规则」接着做');
      emitRun(pid, st);
      return json(200, st.run());
    }
    if (!['running', 'queued'].includes(st.state) && !(op !== 'pause' && st.state === 'paused')) {
      return err(409, 'autobuild_not_running', '运行不在可以控制的状态', { state: st.state });
    }
    if (op === 'pause') {
      st.state = 'pausing';
      emitRun(pid, st);
      after(pid, 1200, () => {
        st.state = 'paused';
        emitConv(pid, cidOf(), 'card.updated', R.sliceCard('s4', 'paused'));
        notice(pid, '已暂停：停在「临期与损耗检查 › 提取规则」，随时可以续跑');
        setSession(pid, cidOf(), 'idle');
        emitRun(pid, st);
      });
    } else {
      const next = op === 'stop' ? 'stopped' : 'converted';
      st.state = op === 'stop' ? 'stopping' : 'converting';
      emitRun(pid, st);
      after(pid, 1800, () => finishAtSafePoint(pid, st, next));
    }
    return json(200, st.run());
  };
  on('POST', '/projects/([^/]+)/autobuild/runs/([^/]+)/pause', control('pause')); // AB-06
  on('POST', '/projects/([^/]+)/autobuild/runs/([^/]+)/stop-at-safe-point', control('stop')); // AB-07
  on('POST', '/projects/([^/]+)/autobuild/runs/([^/]+)/convert-to-l2', control('convert')); // AB-08
  on('POST', '/projects/([^/]+)/autobuild/runs/([^/]+)/resume', control('resume')); // AB-09
  // 确认结束的运行：运行条收起；没有别的运行在跑时项目回到陪伴建模
  on('POST', '/projects/([^/]+)/autobuild/runs/([^/]+)/acknowledge', ({ m }) => {
    const pid = m[1];
    const st = runOr404(pid, m[2]);
    if (!st) return err(404, 'run_not_found', '运行不存在');
    if (!R.ENDED.has(st.state)) return err(409, 'autobuild_not_running', '运行还没结束', { state: st.state });
    st.acknowledged = true;
    const c = cardOf(pid);
    if (c && !db.autobuildActiveOf(pid)) c.build_mode = 'L2';
    emitRun(pid, st);
    if (c) emitProj(pid, 'project.updated', { fields: ['build_mode'], meta: F.projectMeta(c) });
    return json(200, st.run());
  }); // AB-19

  on('POST', '/projects/([^/]+)/autobuild/ledger/([^/]+)/accept', ({ m }) => {
    const st = storeOf(m[1]);
    const l = st?.entry(m[2]);
    if (!l) return err(404, 'ledger_entry_not_found', '台账条目不存在');
    st.accept(m[2]);
    emitRun(m[1], st);
    emitInbox(m[1]);
    return json(200, l);
  }); // AB-11
  on('POST', '/projects/([^/]+)/autobuild/ledger/([^/]+)/rejudge', ({ m, body }) => {
    const st = storeOf(m[1]);
    const l = st?.entry(m[2]);
    if (!l) return err(404, 'ledger_entry_not_found', '台账条目不存在');
    if (!body.option_id && !body.custom_text) return err(400, 'answer_invalid', '没选');
    st.rejudge(m[2], body);
    emitRun(m[1], st);
    emitInbox(m[1]);
    const running = RUNNING.has(st.state);
    return json(202, { entry: l, command_task: { task_id: `task-${uid()}`, origin: { page: 'inbox', label: '改判' }, title: `改判：${l.summary}`, state: running ? 'queued' : 'running', percent: null, step_label: null, conversation_id: cidOf(), message_id: null, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null } });
  }); // AB-12
  on('POST', '/projects/([^/]+)/autobuild/suspensions/([^/]+)/answer', ({ m, body }) => {
    const st = storeOf(m[1]);
    const x = st?.suspension(m[2]);
    if (!x || x.state !== 'open') return err(404, 'suspension_not_found', '挂起问题不存在或已回答');
    if (!body.option_id && !body.custom_text && !body.action) return err(400, 'answer_invalid', '没选');
    st.answer(m[2], body);
    emitRun(m[1], st);
    emitInbox(m[1]);
    return json(200, { suspension: x, command_task: null });
  }); // AB-15
}
