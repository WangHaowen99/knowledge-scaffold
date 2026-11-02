// mock · 3.14 增量更新（INC-01～INC-05）与工作台的增量更新阶段
// 变更清单就是待处理里的 cl-09（routes-inbox.js 的同一个对象）：这里接受、改写、执行，待处理那边同步变化，反之亦然。
// 阶段（主示例项目）：
//   ?mock=stage:incremental                 「09 修订通知」对话在第一位，等你确认变更点清单（设计稿默认态）
//   ?mock=stage:incremental,inc:executed    已按清单执行，回归 5 / 6，这一轮待确认生效
//   ?mock=stage:incremental,inc:merged      已确认生效
// 其他阶段打开 incremental 路由：页面经 IN-01 找到 cl-09、经 INC-01 取到它所在的对话；这条对话第一次被问到时才放进对话列表。
// 这一轮的变更集记在 db.changesets（owner: 'incremental'），CS-02～CS-05 由这里处理（index.js 的同路径处理者让过来）。
import * as D from './incremental-data.js';

const RUN_STEP_MS = 1100;

const bump = (label) => {
  const m = /^v0\.(\d+)$/.exec(label || '');
  return m ? `v0.${Number(m[1]) + 1}` : 'v0.13';
};

// 每个 mock 服务端一套（开发环境的 StrictMode 会建两个服务端，状态不能放在模块级）
function helpers(ctx) {
  const { db } = ctx;
  const stateOf = (pid) => {
    if (!db.inc) db.inc = new Map();
    if (!db.inc.has(pid)) db.inc.set(pid, { phase: 'editing', csId: null, version: null, fixed: false, impactMs: 1800 });
    return db.inc.get(pid);
  };
  // 待处理里的变更清单（同一个对象）；第一次用时先让待处理初始化
  const shared = (pid) => {
    if (!db.inbox || !db.inboxCountOf) return null;
    db.inboxCountOf(pid);
    const s = db.inbox.get(pid);
    const hit = s && [...s.head, ...s.tail].find(([it]) => it.kind === 'change_list');
    return hit ? { item: hit[0], detail: hit[1], s } : null;
  };
  // 「其他待处理 N」：清单还在待处理里时不算它
  const othersOf = (pid) => {
    const sh = shared(pid);
    const total = db.inboxCountOf?.(pid) || 0;
    return sh && !sh.s.gone.has(sh.item.id) ? total - 1 : total;
  };
  const historyFor = (pid) => {
    const st = stateOf(pid);
    return D.incHistory({
      pid, cl: D.overlay(shared(pid).detail), impact: D.impactOf(st.impactMs), others: othersOf(pid),
      phase: st.phase === 'running' ? 'editing' : st.phase, csId: st.csId, version: st.version,
    });
  };
  return { stateOf, shared, othersOf, historyFor };
}

// 阶段的对话历史在 register 里就生成（见下面 convsOf 的预取），用的是正在登记的那个服务端
let current = null;
export const stages = {
  incremental: {
    conversations: () => [D.incConversation(true), ...current.ctx.F.conversations().map((c) => ({ ...c, live: false }))],
    history: () => current.h.historyFor(current.ctx.F.MAIN_PID),
  },
};

export function register(ctx) {
  const h = helpers(ctx);
  current = { ctx, h };
  const { stateOf, shared, othersOf, historyFor } = h;
  const {
    on, json, err, db, sc, uid, F, hub, cardOf, convKey, convsOf, flowOf, stageOf,
    emitConv, emitProj, setSession, setScope, later, putTask, commandHooks,
  } = ctx;
  const cid = D.INC_CID;

  const lockOf = (state, csId) => ({
    state, changeset_id: csId, conversation_id: cid, run_id: null, slice: null, node: { id: 'core.incremental', version: 'core@1.4.2' },
    entries: D.impactOf().stale.map((s) => s.target), artifacts: D.SCENARIOS.affected, paths: [],
    reason_text: '助手正在按 09 修订通知修改 6 个产物', since: new Date().toISOString(),
  });
  const noLock = () => ({ state: 'none', changeset_id: null, conversation_id: null, run_id: null, slice: null, node: null, entries: [], artifacts: [], paths: [], reason_text: '', since: null });

  // 增量更新节点的状态跟着清单走（建模步骤抽屉）
  function setNode(pid, state, clearNotice = false) {
    const f = flowOf(pid);
    const n = (f.nodes || []).find((x) => /incremental|update$/.test(x.id));
    if (!n) return;
    Object.assign(n, { state, display_state: state === 'pending_confirm' ? 'pending' : state });
    if (clearNotice) f.notices = (f.notices || []).filter((x) => x.kind !== 'new_materials');
    emitProj(pid, 'flow.updated', { flow_hash: f.flow_hash, nodes: [{ id: n.id, state: n.state, display_state: n.display_state, flags: n.flags, progress: n.progress }], slices: [], suggestions_changed: true });
  }

  // 这条对话第一次被问到时放进对话列表（主示例项目的其他阶段）
  function ensureConversation(pid) {
    const list = convsOf(pid);
    if (list.some((c) => c.id === cid)) return;
    const conv = D.incConversation(false);
    list.splice(Math.min(1, list.length), 0, conv);
    hub.seed(convKey(pid, cid), 'conversation', historyFor(pid));
    emitProj(pid, 'conversation.updated', { conversation: conv, deleted: false });
  }

  const touched = (pid, sh, impactMs = null) => {
    sh.detail.revision = `r${uid()}`;
    emitProj(pid, 'change_list.updated', { change_list_id: sh.detail.id, revision: sh.detail.revision, impact_ms: impactMs });
    db.emitInbox?.(pid, [], [sh.item.id]);
    const cl = D.overlay(sh.detail);
    emitConv(pid, cid, 'card.updated', D.listCard(cl));
    return cl;
  };
  const conflict = (sh, base) => err(409, 'revision_conflict', '清单已被修改', {
    target: `change_list/${sh.detail.id}`, expected: base, actual: sh.detail.revision, changed_by: 'web', changed_at: new Date().toISOString(), version_label: null, kind: 'change_list',
  });
  const find = (pid, id) => {
    const sh = shared(pid);
    return sh && sh.detail.id === id ? sh : null;
  };

  // ?mock=stage:incremental,inc:executed|merged：一打开就在执行后 / 确认生效后
  if (stageOf(F.MAIN_PID) === 'incremental' && ['executed', 'merged'].includes(sc.inc)) {
    const pid = F.MAIN_PID;
    const sh = shared(pid);
    const st = stateOf(pid);
    const c = cardOf(pid);
    sh.detail.points.forEach((p) => {
      if (p.id === 'cp-4') Object.assign(p, { condition: '临期检查频次', value: '每批（同一到货批次、同一品类算一批）', scope: '临期与损耗检查', state: 'rewritten', rewritten_text: '临期检查频次 每批（同一到货批次、同一品类算一批） 临期与损耗检查' });
      else if (p.state === 'pending') p.state = 'accepted';
    });
    Object.assign(st, { phase: sc.inc, csId: 'cs-32', version: bump(c?.version_label) });
    Object.assign(sh.detail, { state: 'executed', changeset_id: st.csId });
    sh.s.gone.add(sh.item.id);
    sh.s.handled += 1;
    db.changesets.set(st.csId, { owner: 'incremental', state: sc.inc === 'merged' ? 'merged' : 'pending_confirm', pid, cid });
    if (sc.inc === 'executed') db.scope.set(pid, lockOf('pending_confirm', st.csId));
    else if (c) c.version_label = st.version;
    const f = flowOf(pid);
    const n = (f.nodes || []).find((x) => /incremental|update$/.test(x.id));
    if (n) Object.assign(n, sc.inc === 'merged' ? { state: 'confirmed', display_state: 'confirmed' } : { state: 'pending_confirm', display_state: 'pending' });
    if (sc.inc === 'merged') f.notices = (f.notices || []).filter((x) => x.kind !== 'new_materials');
  }
  // 增量更新阶段：现在就生成对话列表和历史（stages.history 读的是 current，也就是正在登记的这个服务端）
  if (stageOf(F.MAIN_PID) === 'incremental') convsOf(F.MAIN_PID);

  // ---------- INC-01 变更点清单 ----------
  on('GET', '/projects/([^/]+)/change-lists/([^/]+)', ({ m }) => {
    const sh = find(m[1], m[2]);
    if (!sh) return err(404, 'not_found', '变更清单不存在');
    ensureConversation(m[1]);
    return json(200, D.overlay(sh.detail));
  });

  // ---------- INC-02 保存清单：完整列表，新增条目不带 id；保存后重算影响面 ----------
  on('PUT', '/projects/([^/]+)/change-lists/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const sh = find(pid, m[2]);
    if (!sh) return err(404, 'not_found', '变更清单不存在');
    if (sh.detail.state === 'executed') return err(409, 'changeset_closed', '清单已经执行', { state: 'executed' });
    if (body.base_revision && body.base_revision !== sh.detail.revision) return conflict(sh, body.base_revision);
    const inputs = Array.isArray(body.points) ? body.points : [];
    const bad = inputs.findIndex((p) => !String(p.condition || '').trim());
    if (!inputs.length || bad >= 0) return err(400, 'invalid_argument', '条件不能为空', { fields: { [`points[${Math.max(bad, 0)}].condition`]: 'required' } });
    const cur = D.overlay(sh.detail).points;
    sh.detail.points = inputs.map((inp, i) => {
      const old = inp.id && cur.find((p) => p.id === inp.id);
      const fields = { condition: inp.condition.trim(), value: (inp.value || '').trim(), scope: (inp.scope || '').trim() };
      if (!old) {
        return {
          id: `cp-${uid()}`, n: i + 1, ...fields, anchor: inp.anchor || '', anchor_label: '手动补充', confidence: 'high', confidence_note: null,
          state: 'accepted', rewritten_text: null, impact: [],
        };
      }
      const next = { ...old, n: i + 1, ...fields, anchor: inp.anchor || old.anchor };
      return inp.rewritten_text ? { ...next, state: 'rewritten', rewritten_text: inp.rewritten_text } : next;
    });
    const st = stateOf(pid);
    st.impactMs = 1600;
    const cl = touched(pid, sh, st.impactMs);
    emitConv(pid, cid, 'card.updated', D.impactCard(cl, D.impactOf(st.impactMs)));
    return json(200, { change_list: cl, impact_ms: st.impactMs });
  });

  // ---------- INC-03 接受变更点：低把握的不能出现在多于一条的批量里 ----------
  on('POST', '/projects/([^/]+)/change-lists/([^/]+)/accept', ({ m, body }) => {
    const pid = m[1];
    const sh = find(pid, m[2]);
    if (!sh) return err(404, 'not_found', '变更清单不存在');
    if (sh.detail.state === 'executed') return err(409, 'changeset_closed', '清单已经执行', { state: 'executed' });
    const pts = sh.detail.points;
    let sel;
    if (body.high_confidence) sel = pts.filter((p) => p.confidence === 'high');
    else if (Array.isArray(body.point_ids) && body.point_ids.length) sel = pts.filter((p) => body.point_ids.includes(p.id));
    else return err(400, 'invalid_argument', 'point_ids 与 high_confidence 二选一', { fields: { point_ids: 'required' } });
    const low = sel.filter((p) => p.confidence === 'low');
    if (sel.length > 1 && low.length) return err(422, 'low_confidence_batch', '把握低的变更点不能批量接受，必须单独确认', { point_ids: low.map((p) => p.id) });
    sel.forEach((p) => { if (p.state === 'pending') p.state = 'accepted'; });
    return json(200, touched(pid, sh));
  });

  // ---------- INC-05 影响面 ----------
  on('GET', '/projects/([^/]+)/change-lists/([^/]+)/impact', ({ m }) => {
    if (!find(m[1], m[2])) return err(404, 'not_found', '变更清单不存在');
    return json(200, D.impactOf(stateOf(m[1]).impactMs));
  });

  // ---------- INC-04 确认清单并执行：往对话发执行命令，之后只改受影响部分 → 只查改动 → 自动回归 → 回归结果 → 这一轮 ----------
  on('POST', '/projects/([^/]+)/change-lists/([^/]+)/execute', ({ m, body }) => {
    const pid = m[1];
    const sh = find(pid, m[2]);
    if (!sh) return err(404, 'not_found', '变更清单不存在');
    if (sh.detail.state === 'executed') return err(409, 'changeset_closed', '清单已经执行', { state: 'executed' });
    if (body.base_revision && body.base_revision !== sh.detail.revision) return conflict(sh, body.base_revision);
    const unconfirmed = sh.detail.points.filter((p) => p.state === 'pending').map((p) => p.id);
    if (unconfirmed.length) return err(400, 'invalid_argument', '还有变更点没确认', { unconfirmed });
    if (convsOf(pid).some((c) => c.state === 'running' && c.id !== cid)) return err(409, 'session_busy', '另一条对话正在运行');
    ensureConversation(pid);
    const st = stateOf(pid);
    const csId = `cs-${uid()}`;
    Object.assign(st, { phase: 'running', csId, version: bump(cardOf(pid)?.version_label), fixed: false });
    Object.assign(sh.detail, { state: 'executed', changeset_id: csId });
    sh.s.gone.add(sh.item.id);
    sh.s.handled += 1;
    db.emitInbox?.(pid, [sh.item.id]);
    db.changesets.set(csId, { owner: 'incremental', state: 'open', pid, cid });
    const cl = D.overlay(sh.detail);
    const impact = D.impactOf(st.impactMs);
    const msgId = `m-${uid()}`;
    const task = putTask(pid, {
      task_id: `task-${uid()}`, origin: { page: 'incremental', label: '增量更新' }, title: '助手正在按 09 修订通知修改', state: 'running', percent: 0,
      step_label: '按清单修改', conversation_id: cid, message_id: msgId, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null,
    });
    emitConv(pid, cid, 'card.updated', D.listCard(cl));
    emitConv(pid, cid, 'card.updated', D.impactCard(cl, impact, true));
    emitConv(pid, cid, 'message.user', { message_id: msgId, text: D.executeText(cl), attachments: [], references: [], origin: null, delivery: 'immediate' });
    setSession(pid, cid, 'running');
    setScope(pid, lockOf('assistant_running', csId));
    emitProj(pid, 'changeset.updated', { changeset_id: csId, state: 'open', source: 'assistant', conversation_id: cid, version_label: null });
    setNode(pid, 'running');
    emitConv(pid, cid, 'progress', D.runProgress(0));
    emitConv(pid, cid, 'suggestions.updated', { mode: 'normal', primary: null, secondary: [], all_commands: true });
    const step = (n) => later(pid, cid, RUN_STEP_MS, () => {
      emitConv(pid, cid, 'progress.updated', D.runProgress(n));
      putTask(pid, { ...task, percent: Math.round((n / 3) * 100), step_label: ['按清单修改', '只查改动', '自动回归'][n] || '自动回归' }, { transient: true });
      if (n < 3) { step(n + 1); return; }
      st.phase = 'executed';
      db.changesets.get(csId).state = 'pending_confirm';
      emitConv(pid, cid, 'card', D.regressionCard());
      emitConv(pid, cid, 'card', D.roundCard({ csId, nextVersion: st.version }));
      emitConv(pid, cid, 'turn.completed', { turn_id: `t-${uid()}`, duration_ms: 211000, is_error: false, usage: { input_tokens: 26400, output_tokens: 3100, cache_read_tokens: 0 }, cost: { amount: 0.86, currency: 'CNY' } });
      setSession(pid, cid, 'idle');
      setScope(pid, lockOf('pending_confirm', csId));
      emitProj(pid, 'changeset.updated', { changeset_id: csId, state: 'pending_confirm', source: 'assistant', conversation_id: cid, version_label: null });
      setNode(pid, 'pending_confirm');
      putTask(pid, { ...task, state: 'done', percent: 100, step_label: '自动回归', finished_at: new Date().toISOString(), result: { summary: '回归 5 / 6 通过，这一轮待确认', refs: [] } });
      emitConv(pid, cid, 'suggestions.updated', D.postSuggestions(csId));
    });
    step(1);
    return json(202, { command_task: task, changeset_id: csId });
  });

  // ---------- 这一轮的变更集（CS-02～CS-05） ----------
  const mine = (id) => {
    const cs = db.changesets.get(id);
    return cs?.owner === 'incremental' ? cs : null;
  };
  on('GET', '/projects/([^/]+)/changesets/([^/]+)/diff', ({ m, query }) => {
    if (!mine(m[2])) return undefined;
    return json(200, { items: D.diffItems(query.get('target')) });
  }); // CS-03
  on('GET', '/projects/([^/]+)/changesets/([^/]+)', ({ m }) => {
    const cs = mine(m[2]);
    if (!cs) return undefined;
    const st = stateOf(m[1]);
    const card = D.roundCard({ csId: m[2], nextVersion: st.version, fixed: st.fixed });
    return json(200, {
      id: m[2], source: 'assistant', build_mode: 'L2', purpose: '按 09 修订通知增量更新', state: cs.state, base: cardOf(m[1])?.head || 'a71e03c9',
      conversation_id: cid, run_id: null, slice_id: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      merged: cs.state === 'merged' ? { commit: 'c0ffee14', version_label: st.version, at: new Date().toISOString() } : null, entity_count: 6,
      groups: card.groups.filter((g) => g.kind !== 'scenario').map((g) => ({ kind: g.kind, items: g.items.map((x) => ({ target: x.target, change: x.change, detail: x.text.replace(/\[\[|\]\]/g, ''), checks: 'passed' })) })),
      checks: { errors: 0, warnings: 1, findings: [] }, next_version_label: st.version,
    });
  }); // CS-02
  on('POST', '/projects/([^/]+)/changesets/([^/]+)/merge', ({ m }) => {
    const pid = m[1];
    const cs = mine(m[2]);
    if (!cs) return undefined;
    if (cs.state !== 'pending_confirm') return err(409, 'changeset_closed', '已关闭', { state: cs.state });
    if (db.running.has(`${pid}:${cs.cid}`)) return err(409, 'changeset_in_progress', '助手还在跑', { conversation_id: cs.cid });
    const st = stateOf(pid);
    cs.state = 'merged';
    st.phase = 'merged';
    const c = cardOf(pid);
    if (c) c.version_label = st.version;
    const merged = { state: 'merged', commit: 'c0ffee14', version_label: st.version, merged_at: new Date().toISOString() };
    emitConv(pid, cid, 'card.updated', D.roundCard({ csId: m[2], nextVersion: st.version, state: 'merged', mergedVersion: st.version, fixed: st.fixed }));
    emitConv(pid, cid, 'notice', { kind: 'merged', text: `这一轮已确认生效 · 版本 ${st.version}`, version_label: st.version, changeset_id: m[2], align: 'center' });
    emitConv(pid, cid, 'suggestions.updated', D.mergedSuggestions(st.version, othersOf(pid), st.fixed));
    setScope(pid, noLock());
    setNode(pid, 'confirmed', true);
    emitProj(pid, 'changeset.updated', { changeset_id: m[2], state: 'merged', source: 'assistant', conversation_id: cid, version_label: st.version });
    emitProj(pid, 'workspace.changed', {
      commit: merged.commit, version_label: st.version, source: 'assistant', changeset_id: m[2], by: 'zhangsan',
      entries: D.impactOf().stale.map((s) => s.target), artifacts: D.SCENARIOS.affected, paths: ['resource/others/09-修订通知.docx'],
    });
    if (c) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(c) });
    return json(200, merged);
  }); // CS-04
  on('POST', '/projects/([^/]+)/changesets/([^/]+)/discard', ({ m }) => {
    const pid = m[1];
    const cs = mine(m[2]);
    if (!cs) return undefined;
    if (cs.state !== 'pending_confirm') return err(409, 'changeset_closed', '已关闭', { state: cs.state });
    if (db.running.has(`${pid}:${cs.cid}`)) return err(409, 'changeset_in_progress', '助手还在跑', { conversation_id: cs.cid });
    const st = stateOf(pid);
    cs.state = 'discarded';
    Object.assign(st, { phase: 'editing', csId: null, fixed: false });
    // 撤回后清单回到可编辑，回到待处理里
    const sh = shared(pid);
    Object.assign(sh.detail, { state: 'editing', changeset_id: null });
    if (sh.s.gone.delete(sh.item.id)) sh.s.handled = Math.max(0, sh.s.handled - 1);
    db.emitInbox?.(pid, [], [sh.item.id]);
    const cl = D.overlay(sh.detail);
    emitConv(pid, cid, 'card.updated', D.roundCard({ csId: m[2], nextVersion: st.version, state: 'discarded' }));
    emitConv(pid, cid, 'card.updated', D.listCard(cl));
    emitConv(pid, cid, 'card.updated', D.impactCard(cl, D.impactOf(st.impactMs), false));
    emitConv(pid, cid, 'notice', { kind: 'discarded', text: '已撤回这一轮；清单可以改了再执行', version_label: null, changeset_id: m[2], align: 'center' });
    emitConv(pid, cid, 'suggestions.updated', D.preSuggestions(othersOf(pid)));
    setScope(pid, noLock());
    setNode(pid, 'ready');
    emitProj(pid, 'changeset.updated', { changeset_id: m[2], state: 'discarded', source: 'assistant', conversation_id: cid, version_label: null });
    return json(200, { state: 'discarded' });
  }); // CS-05

  // ---------- 「交给助手修」：S-20 /ontology-update 带 fix_scenarios，修完重跑这个场景 ----------
  commandHooks.push({
    match: (b) => b.command === '/ontology-update' && Array.isArray(b.args?.fix_scenarios),
    title: () => '助手正在修复不通过的场景',
    steps: ['补上步骤 4 的规则引用', '重跑场景 6'],
    start: (pid) => {
      const st = stateOf(pid);
      if (st.csId && db.changesets.get(st.csId)?.state === 'pending_confirm') setScope(pid, lockOf('assistant_running', st.csId));
    },
    doneText: () => '修好了：操作 [[超量与审批提示]] 的步骤 4 已引用改写后的 R-11，场景 6 重跑通过。',
    finish: (pid) => {
      const st = stateOf(pid);
      st.fixed = true;
      emitConv(pid, cid, 'card.updated', D.regressionCard(true));
      const cs = st.csId && db.changesets.get(st.csId);
      if (cs?.state === 'pending_confirm') {
        emitConv(pid, cid, 'card.updated', D.roundCard({ csId: st.csId, nextVersion: st.version, fixed: true }));
        setScope(pid, lockOf('pending_confirm', st.csId));
        emitConv(pid, cid, 'suggestions.updated', D.postSuggestions(st.csId, true));
      } else emitConv(pid, cid, 'suggestions.updated', D.mergedSuggestions(st.version || 'v0.13', othersOf(pid), true));
    },
  });
}
