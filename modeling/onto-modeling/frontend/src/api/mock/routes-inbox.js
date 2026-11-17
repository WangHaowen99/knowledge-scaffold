// 3.13 待处理（IN-01～IN-04）：计数和外壳角标、项目卡「待处理 N」同源；处理一项计数就少一项，经项目流 inbox.updated 推送
import * as D from './inbox-data.js';

const kind = (k) => (it) => it.kind === k;
const FILTERS = {
  all: () => true,
  autobuild_review: (it) => (it.kind === 'review' && it.source === 'autobuild') || it.kind === 'suspension',
  decision: kind('decision'),
  change_list: kind('change_list'),
  stale: kind('stale'),
  resumable: kind('resumable'),
};
const RESOLVED_TEXT = {
  answer: '已回答', execute: '清单已确认，开始修改', accept: '已接受', rejudge: '已改判，助手会重做受影响的部分',
  revert_slice: '已回退到运行前', use_generic: '先按通用要求处理', refresh: '已刷新', keep_stale: '保持过期', resume: '已续跑', abandon: '已放弃',
  dismiss: '已忽略',
};

export function register(ctx) {
  const { on, json, err, db, F, emitProj, emitUser, emitConv, cardOf, convsOf, uid, sc = {} } = ctx;
  db.inbox = new Map();
  const stateOf = (pid) => {
    if (!db.inbox.has(pid)) {
      let head = [];
      let tail = [];
      let handled = 0;
      if (pid === F.MAIN_PID) ({ head, tail } = D.mainItems());
      if (pid === F.MAIN_PID && sc.inbox === 'human') head = [D.humanNodeItem(), ...head];
      if (pid === F.MAIN_PID) handled = 5;
      else if (pid === D.RIDER_PID) head = D.riderItems();
      else if (pid === D.ESTATE_PID) head = D.estateItems();
      db.inbox.set(pid, { head, tail, handled, gone: new Set() });
    }
    return db.inbox.get(pid);
  };
  const storeOf = (pid) => db.autobuildOf?.(pid) || null;
  const allOf = (pid) => {
    const s = stateOf(pid);
    const store = storeOf(pid);
    const run = store ? D.runItems(store) : [];
    return [...s.head, ...run, ...s.tail].filter(([it]) => !s.gone.has(it.id));
  };
  const countsOf = (items) => {
    const by_filter = Object.fromEntries(Object.entries(FILTERS).map(([k, fn]) => [k, items.filter(fn).length]));
    const by_pack = {};
    items.forEach((it) => { if (it.pack) by_pack[it.pack] = (by_pack[it.pack] || 0) + 1; });
    return { total: items.length, by_filter, by_pack };
  };
  db.inboxCountOf = (pid) => allOf(pid).map(([it]) => it).length;
  db.emitInbox = (pid, removed = [], changed = []) => {
    const counts = countsOf(allOf(pid).map(([it]) => it));
    emitProj(pid, 'inbox.updated', { counts, added: [], removed, changed });
    const c = cardOf(pid);
    if (c && c.inbox_count !== counts.total) {
      c.inbox_count = counts.total;
      emitUser('project.card.updated', { project_id: pid, card: c });
    }
  };

  function summaryOf(pid) {
    if (pid === D.RIDER_PID) return { ...D.riderSummary(), high_confidence_pending: 0 };
    const store = storeOf(pid);
    if (!store) return null;
    const run = store.run();
    const led = store.ledger();
    return {
      run_id: run.run_id, run_no: run.run_no, started_at: run.started_at, ended_at: run.ended_at, state: run.state, cost: run.cost.used,
      decisions: led.length, high_confidence: led.filter((l) => l.confidence >= l.threshold).length, needs_look: led.filter((l) => l.confidence < l.threshold).length,
      suspended: store.suspensions().length, assumptions: led.filter((l) => l.assumption).length, high_confidence_pending: store.pendingHigh().length,
    };
  }

  on('GET', '/projects/([^/]+)/inbox', ({ m, query }) => {
    const pid = m[1];
    const all = allOf(pid).map(([it]) => it);
    const filter = query.get('filter') || 'all';
    if (!FILTERS[filter]) return err(400, 'invalid_argument', '筛选不合法', { fields: { filter: 'invalid' } });
    let items = all.filter(FILTERS[filter]);
    const kinds = query.getAll('kind').flatMap((x) => x.split(',')).filter(Boolean);
    if (kinds.length) items = items.filter((it) => kinds.includes(it.kind));
    if (query.get('run_id')) items = items.filter((it) => it.run?.run_id === query.get('run_id'));
    if (query.get('pack')) items = items.filter((it) => it.pack === query.get('pack'));
    const limit = Number(query.get('limit') || 50);
    const counts = countsOf(all);
    const s = stateOf(pid);
    return json(200, {
      items: items.slice(0, limit),
      next_cursor: null,
      counts,
      today: { handled: s.handled, total: s.handled + counts.total, eta_minutes: Math.max(1, Math.round(counts.total * 1.1)), remaining_by_filter: counts.by_filter },
      autobuild_summary: filter === 'autobuild_review' ? summaryOf(pid) : null,
    });
  }); // IN-01

  on('GET', '/projects/([^/]+)/inbox/([^/]+)', ({ m }) => {
    const id = decodeURIComponent(m[2]);
    const hit = allOf(m[1]).find(([it]) => it.id === id);
    if (!hit) return err(404, 'not_found', '事项已不存在');
    return json(200, { ...hit[0], detail: hit[1] });
  }); // IN-02

  const commandTask = (pid, title, page = 'inbox') => ({
    task_id: `task-${uid()}`, origin: { page, label: '待处理' }, title, state: 'running', percent: null, step_label: null,
    conversation_id: convsOf(pid)[0]?.id || null, message_id: null, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null,
  });

  function act(pid, it, detail, body) {
    const store = storeOf(pid);
    const a = body.action;
    const need = () => (!body.option_id && !body.custom_text ? err(400, 'answer_invalid', '先选一个选项，或写下你的方向') : null);
    if (!it.actions.includes(a)) return err(400, 'invalid_argument', '这个动作不适用于这一项', { fields: { action: 'not_allowed' } });
    switch (it.kind) {
      case 'decision': {
        if (a !== 'dismiss') { const bad = need(); if (bad) return bad; }
        // 人工节点的决策卡只收选项（B5c 合并：后端回 400 answer_invalid）
        if (it.source === 'plugin' && a === 'answer' && !body.option_id) return err(400, 'answer_invalid', '人工节点要选一个选项');
        if (it.id === `decision:${F.QUESTION_ID}` && a === 'answer') {
          // 同一个决策卡在 wb-l2 对话里也收起（等价于 S-11）
          const cid = convsOf(pid)[0]?.id;
          const q = F.decisionQuestion();
          const o = q.options.find((x) => x.id === body.option_id) || q.options[0];
          if (cid) emitConv(pid, cid, 'question.answered', { question_id: q.question_id, answer: { option_id: o.id, custom_text: body.custom_text }, by: 'user', summary: `步骤 2 选了 ${o.key}：${body.custom_text || o.label}`, answered_at: new Date().toISOString() });
        }
        return { resolved: true, result: {} };
      }
      case 'change_list': {
        const pts = detail.points;
        if (a === 'accept_points') {
          const ids = body.point_ids || [];
          const sel = pts.filter((p) => ids.includes(p.id));
          if (sel.length > 1 && sel.some((p) => p.confidence === 'low')) return err(422, 'low_confidence_batch', '把握低的条目只能单独确认', { point_ids: sel.filter((p) => p.confidence === 'low').map((p) => p.id) });
          sel.forEach((p) => { p.state = 'accepted'; });
          detail.revision = `r${uid()}`;
          return { resolved: false, result: { change_list: detail } };
        }
        if (a === 'rewrite_point') {
          const p = pts.find((x) => x.id === body.point_id);
          if (!p || !body.text?.trim()) return err(400, 'invalid_argument', '改写内容为空', { fields: { text: 'required' } });
          Object.assign(p, { state: 'rewritten', rewritten_text: body.text.trim() });
          detail.revision = `r${uid()}`;
          return { resolved: false, result: { change_list: detail } };
        }
        if (a === 'execute') {
          const unconfirmed = pts.filter((p) => p.state === 'pending').map((p) => p.id);
          if (unconfirmed.length) return err(400, 'invalid_argument', '还有变更点没确认', { unconfirmed });
          detail.state = 'executed';
          return { resolved: true, result: { command_task: commandTask(pid, '按 09 修订通知的清单修改'), changeset_id: `cs-${uid()}` } };
        }
        return { resolved: true, result: {} };
      }
      case 'review': {
        if (detail.group === 'high_confidence') {
          if (a === 'accept') return { resolved: true, result: { accepted: store.acceptHigh() } };
          return { resolved: true, result: {} };
        }
        if (it.run?.run_id === 'run-2' || !store) {
          if (a === 'rejudge') { const bad = need(); if (bad) return bad; }
          return { resolved: true, result: {} };
        }
        if (a === 'accept') return { resolved: true, result: { entry: store.accept(detail.id) } };
        if (a === 'rejudge') {
          const bad = need();
          if (bad) return bad;
          store.rejudge(detail.id, body);
          return { resolved: true, result: { entry: detail, command_task: commandTask(pid, `改判：${detail.summary}`) } };
        }
        if (a === 'revert_slice') {
          if (['running', 'pausing', 'stopping', 'converting', 'queued'].includes(store.state)) return err(409, 'autobuild_running', '本次运行还在跑', { run_id: detail.run_id });
          if (store.isReverted(detail.slice.id)) return err(409, 'slice_not_revertible', '这一段已经回退过了', { reason: 'already_reverted' });
          const r = store.revert(detail.slice.id);
          const c = cardOf(pid);
          if (c) c.version_label = r.version_label;
          emitProj(pid, 'workspace.changed', { commit: r.commit, version_label: r.version_label, source: 'revert', changeset_id: null, by: 'zhangsan', entries: [], artifacts: [], paths: [] });
          return { resolved: true, result: r };
        }
        return { resolved: true, result: {} };
      }
      case 'suspension': {
        if (a === 'answer') { const bad = need(); if (bad) return bad; }
        if (store && it.run?.run_id === store.run().run_id) store.answer(detail.id, a === 'use_generic' ? { action: 'use_generic' } : body);
        return { resolved: true, result: { command_task: a === 'answer' ? commandTask(pid, `补上：${it.title}`) : null } };
      }
      case 'resumable':
        return { resolved: true, result: a === 'resume' ? { command_task: commandTask(pid, `续跑：${detail.command_label}`) } : {} };
      default:
        return { resolved: true, result: {} };
    }
  }

  on('POST', '/projects/([^/]+)/inbox/([^/]+)/actions', ({ m, body }) => {
    const pid = m[1];
    const list = allOf(pid);
    const id = decodeURIComponent(m[2]);
    const i = list.findIndex(([it]) => it.id === id);
    if (i < 0) return err(404, 'not_found', '事项已不存在');
    const [it, detail] = list[i];
    const out = act(pid, it, detail, body);
    if (out instanceof Response) return out;
    const s = stateOf(pid);
    if (out.resolved) {
      s.gone.add(it.id);
      s.handled += 1;
    }
    // revert_slice 会连带去掉这一段的其他复核项
    const after = allOf(pid).map(([x]) => x.id);
    const next = list.slice(i + 1).map(([x]) => x.id).find((id) => after.includes(id)) || after[0] || null;
    db.emitInbox(pid, out.resolved ? [it.id] : [], out.resolved ? [] : [it.id]);
    return json(200, { item_id: it.id, resolved: out.resolved, result: { ...out.result, text: RESOLVED_TEXT[body.action] || '' }, next_item_id: out.resolved ? next : it.id });
  }); // IN-03

  on('POST', '/projects/([^/]+)/inbox/batch', ({ m, body }) => {
    const pid = m[1];
    const s = stateOf(pid);
    const store = storeOf(pid);
    if (body.action === 'accept_high_confidence') {
      if (!store || (body.run_id && store.run().run_id !== body.run_id)) return err(404, 'run_not_found', '运行不存在');
      const n = store.acceptHigh();
      s.handled += n ? 1 : 0;
      db.emitInbox(pid, [`review:${store.run().run_id}:high`]);
      return json(200, { accepted: n, skipped: [], command_task: null });
    }
    if (body.action === 'refresh_stale') {
      const stale = allOf(pid).filter(([it]) => it.kind === 'stale' && (!body.item_ids || body.item_ids.includes(it.id))).map(([it]) => it.id);
      stale.forEach((id) => s.gone.add(id));
      s.handled += stale.length;
      db.emitInbox(pid, stale);
      return json(200, { accepted: stale.length, skipped: [], command_task: stale.length ? commandTask(pid, `刷新过期的 ${stale.length} 项`) : null });
    }
    if (body.action === 'accept_change_points') {
      const hit = allOf(pid).find(([it]) => it.kind === 'change_list' && (!body.change_list_id || it.id === `change_list:${body.change_list_id}`));
      if (!hit) return err(400, 'invalid_argument', '变更清单不存在');
      const pts = hit[1].points.filter((p) => (body.point_ids || []).includes(p.id));
      const skipped = pts.filter((p) => p.confidence !== 'high').map((p) => ({ id: p.id, reason: 'low_confidence' }));
      const ok = pts.filter((p) => p.confidence === 'high');
      ok.forEach((p) => { p.state = 'accepted'; });
      db.emitInbox(pid, [], [hit[0].id]);
      return json(200, { accepted: ok.length, skipped, command_task: null });
    }
    return err(400, 'invalid_argument', '批量动作不合法', { fields: { action: 'invalid' } });
  }); // IN-04
}
