// 对话状态机：对话流事件（契约 4.2）→ 时间线与会话状态。纯函数，历史（S-06）和实时流用同一套归并。
// 卡片、问题、进展组按各自的 id 原位替换；持久事件按 seq 去重，瞬时事件（message.delta、
// context.usage）不带 seq，直接生效。

export function initialConversation() {
  return {
    lastSeq: 0,
    order: [],
    items: {},
    session: { state: 'idle', since: null, turn_started_at: null, reason: null },
    queue: null,
    next: null, // 平台代存的下一条命令（运行中发的命令，这一轮结束后开始）
    context: null,
    suggestions: null,
    usage: { input_tokens: 0, output_tokens: 0, cost: null, turns: 0 },
    configPending: null,
    engineLabel: null,
    thinking: '', // 这一次模型调用到目前的思考片段（瞬时，运行状态行展开看）
  };
}

const THINKING_MAX = 2000;

const seqOf = (env) => {
  const n = Number(env?.id);
  return Number.isFinite(n) && env?.id !== null && env?.id !== undefined && env?.id !== '' ? n : null;
};

function upsert(state, key, item) {
  const exists = Object.prototype.hasOwnProperty.call(state.items, key);
  return {
    ...state,
    order: exists ? state.order : [...state.order, key],
    items: { ...state.items, [key]: exists ? { ...state.items[key], ...item } : item },
  };
}

function patch(state, key, fn) {
  const cur = state.items[key];
  if (!cur) return state;
  return { ...state, items: { ...state.items, [key]: fn(cur) } };
}

// 子任务卡在本页上收到的逐步明细只留最近这么多步（后端每个子任务最多 200 步）
export const SUB_STEPS_MAX = 200;

// 子任务的一步并进明细：同一步后到的只改状态（说法为 null 时沿用先到的），记下开始、结束的时刻
export function mergeSubSteps(steps, step, ts) {
  const list = steps || [];
  if (!step?.id) return list;
  const i = list.findIndex((x) => x.id === step.id);
  const over = step.state !== 'running';
  if (i < 0) {
    const next = [...list, { ...step, started_at: ts, ended_at: over && step.kind === 'tool' ? ts : null }];
    return next.length > SUB_STEPS_MAX ? next.slice(-SUB_STEPS_MAX) : next;
  }
  const cur = list[i];
  const next = list.slice();
  next[i] = { ...cur, state: step.state, text: step.text || cur.text, ended_at: cur.ended_at || (over ? ts : null) };
  return next;
}

// 同一个子任务的更新按到达顺序合并（后到的字段盖住先到的）；空的进展说法、任务说明不盖掉先到的。
// 「思考中」只在最新这条是思考心跳时成立，有了新的一步就清掉
function mergeSubagent(prev, sub, ts) {
  const { step, ...rest } = sub;
  const over = sub.status === 'done' || sub.status === 'failed';
  return {
    ...prev,
    ...rest,
    progress_text: sub.progress_text || prev?.progress_text || '',
    brief: sub.brief || prev?.brief || null,
    step_count: Math.max(prev?.step_count || 0, sub.step_count || 0),
    thinking: over ? null : sub.thinking || null,
    started_at: prev?.started_at || ts,
    ended_at: prev?.ended_at || (over ? ts : null),
    steps: mergeSubSteps(prev?.steps, step, ts),
  };
}

// 一个事件 → 新状态
export function applyEvent(state, env) {
  if (!env || !env.type) return state;
  const seq = seqOf(env);
  if (seq !== null) {
    if (seq <= state.lastSeq) return state;
    state = { ...state, lastSeq: seq };
  }
  const d = env.data || {};
  const ts = env.ts || null;
  const at = { ts, seq };
  switch (env.type) {
    case 'session.state':
      if (TURN_END_STATES.includes(d.state)) state = endOpenSubagents(state, 'interrupted');
      return {
        ...state,
        thinking: d.state === 'running' ? state.thinking : '',
        session: { state: d.state, since: d.since || ts, turn_started_at: d.turn_started_at ?? null, reason: d.reason ?? null },
        queue: d.state === 'queued' ? state.queue : null,
      };
    case 'session.queued':
      return { ...state, queue: { ...d } };
    case 'next.updated':
      return { ...state, next: d.next || null };
    case 'message.delivery': {
      // 运行中发的插话送到哪了：等助手看到 → 已看到 / 作为下一轮开始 / 打断时撤回。本地气泡还没换成服务端那条时按 message_id 找
      const key = state.items[`msg:${d.message_id}`] ? `msg:${d.message_id}`
        : state.order.find((k) => state.items[k]?.local && state.items[k].message_id === d.message_id);
      if (!key) return state;
      return patch(state, key, (m) => ({ ...m, delivery_state: { state: d.state, at: d.at || ts, waiting_on: d.waiting_on || null } }));
    }
    case 'session.started':
      return { ...state, engineLabel: d.engine_label || state.engineLabel };
    case 'session.error':
      state = endOpenSubagents(state, d.reason === 'stall_timeout' ? 'timeout' : 'interrupted');
      return upsert(state, `err:${d.error_id || seq}`, { ...at, ...d, kind: 'error' });
    case 'session.interrupted':
      return upsert(endOpenSubagents(state, 'interrupted'), `int:${seq ?? ts}`, { ...at, ...d, kind: 'interrupted' });
    case 'session.config_pending':
      return upsert({ ...state, configPending: d }, `cfg:${seq ?? ts}`, { ...at, ...d, kind: 'config_pending' });
    case 'session.reconnected':
      return upsert({ ...state, configPending: null }, `recon:${seq ?? ts}`, { ...at, ...d, kind: 'reconnected' });

    case 'message.user': {
      // 点发送时先显示的本地气泡：服务端的同一条到了就原地换掉；local_key 让界面沿用同一个元素，发送动画不被打断
      const local = state.order.find((k) => {
        const it = state.items[k];
        return it?.local && (it.message_id ? it.message_id === d.message_id : it.text === d.text);
      });
      if (!local) return upsert(state, `msg:${d.message_id}`, { ...at, ...d, kind: 'user' });
      const key = `msg:${d.message_id}`;
      const items = { ...state.items, [key]: { ...at, ...d, kind: 'user', local_key: local } };
      delete items[local];
      return { ...state, items, order: state.order.filter((k) => k !== key).map((k) => (k === local ? key : k)) };
    }
    case 'message.delta': {
      if (d.kind === 'thinking') return { ...state, thinking: `${state.thinking}${d.text || ''}`.slice(-THINKING_MAX) };
      if (d.kind && d.kind !== 'text') return state;
      const key = `asst:${d.message_id}`;
      const cur = state.items[key];
      if (cur && !cur.streaming) return state;
      return upsert({ ...state, thinking: '' }, key, {
        kind: 'assistant', ts: cur?.ts || ts, message_id: d.message_id,
        text_md: `${cur?.text_md || ''}${d.text || ''}`, anchors: cur?.anchors || [], streaming: true,
      });
    }
    case 'message.assistant': {
      const key = `asst:${d.message_id}`;
      const cur = state.items[key];
      return upsert({ ...state, thinking: '' }, key, { ...at, ...d, ts: cur?.ts || ts, streaming: false, kind: 'assistant' });
    }
    case 'notice':
      return upsert(state, `notice:${seq ?? ts}:${d.text}`, { ...at, ...d, notice_kind: d.kind, kind: 'notice' });
    case 'day.marker':
      return upsert(state, `day:${seq ?? d.label}`, { ...at, ...d, kind: 'day' });

    case 'progress':
    case 'progress.updated': {
      const key = `progress:${d.group_id}`;
      const cur = state.items[key];
      return upsert({ ...state, thinking: '' }, key, { ...d, ts: cur?.ts || ts, seq: cur?.seq ?? seq, kind: 'progress' });
    }
    case 'subagent.updated': {
      // 子任务在时间线上单独成卡，排在第一次出现的位置
      const key = `sub:${d.task_id}`;
      const cur = state.items[key];
      return upsert(state, key, { ...mergeSubagent(cur, d, ts), ts: cur?.ts || ts, kind: 'subagent' });
    }
    case 'tool.activity':
      return state; // 内部步骤只进执行日志，不渲染

    case 'permission.requested':
      return upsert({ ...state, thinking: '' }, `perm:${d.request_id}`, { ...at, ...d, resolved: null, kind: 'permission' });
    case 'permission.resolved':
      return patch(state, `perm:${d.request_id}`, (p) => ({ ...p, resolved: d }));
    case 'question.asked': {
      const key = `q:${d.question_id}`;
      const cur = state.items[key];
      return upsert({ ...state, thinking: '' }, key, {
        ts: cur?.ts || ts, seq: cur?.seq ?? seq, question_id: d.question_id, state: d.state || 'open',
        summary: d.state === 'answered' ? cur?.summary : undefined, question: d, kind: 'question',
      });
    }
    case 'question.answered':
      return patch(state, `q:${d.question_id}`, (q) => ({
        ...q, state: 'answered', answer: d.answer, summary: d.summary, answered_by: d.by, answered_at: d.answered_at,
      }));
    case 'question.withdrawn':
      return patch(state, `q:${d.question_id}`, (q) => ({ ...q, state: 'withdrawn', withdrawn_reason: d.reason }));

    case 'card':
    case 'card.updated': {
      // 插件草稿卡（F17）的 payload 没写 card_id：按这一轮的变更集算一张
      const key = `card:${d.card_id ?? (d.changeset_id ? `cs:${d.changeset_id}` : seq ?? ts)}`;
      // 一轮一张「这一轮改了什么」卡（F3）：升级前同一个变更集出过两张的，只留最新那张，旧的不再画、更新也不画
      if (d.card_type === 'round_changes' && d.changeset_id) {
        const other = state.order.find((k) => k !== key && state.items[k]?.kind === 'card'
          && state.items[k].card.card_type === 'round_changes' && state.items[k].card.changeset_id === d.changeset_id);
        if (other && env.type === 'card.updated' && !state.items[key]) return state;
        if (other && env.type === 'card') {
          const items = { ...state.items };
          delete items[other];
          state = { ...state, items, order: state.order.filter((k) => k !== other) };
        }
      }
      const cur = state.items[key];
      // 整体替换内容，位置和首次出现时间不变
      const next = { kind: 'card', ts: cur?.ts || ts, seq: cur?.seq ?? seq, card: d };
      if (!cur) return upsert(state, key, next);
      return { ...state, items: { ...state.items, [key]: next } };
    }

    case 'turn.completed':
      return {
        ...state,
        usage: {
          input_tokens: state.usage.input_tokens + (d.usage?.input_tokens || 0),
          output_tokens: state.usage.output_tokens + (d.usage?.output_tokens || 0),
          cost: d.cost ?? state.usage.cost,
          turns: state.usage.turns + 1,
        },
      };
    case 'context.usage':
      return { ...state, context: d };
    case 'context.compacted':
      return upsert(state, `compact:${seq ?? ts}`, { ...at, ...d, kind: 'compacted' });
    case 'suggestions.updated':
      return { ...state, suggestions: d };
    default:
      return state;
  }
}

// 往前翻页（S-06 before_seq）：更早的一页单独归并，再接到现有时间线前面。两边都有的键（卡片、进展组
// 在旧页第一次出现、新页里更新过）用新的内容、放在旧页的位置——和整段一次性归并的结果一致。
// prependedKeys 给界面用：这些是翻出来的旧记录，不播入场动画
export function prependHistory(state, olderEvents) {
  const older = applyEvents(initialConversation(), olderEvents);
  const fresh = older.order.filter((k) => !Object.prototype.hasOwnProperty.call(state.items, k));
  const items = { ...state.items };
  fresh.forEach((k) => { items[k] = older.items[k]; });
  const moved = new Set(older.order);
  return {
    ...state,
    order: [...older.order, ...state.order.filter((k) => !moved.has(k))],
    items,
    prependedKeys: fresh,
    historyGen: (state.historyGen || 0) + 1,
  };
}

// 本地气泡：点发送立刻显示，不等服务端经对话流回传（S-07 返回 message_id 后用它对上）
export function addLocalUser(state, localId, text, now = Date.now() / 1000) {
  return upsert(state, `local:${localId}`, { kind: 'user', local: true, text, ts: now, attachments: [] });
}

export function ackLocalUser(state, localId, messageId) {
  const key = `local:${localId}`;
  if (!state.items[key]) return state;
  if (!state.items[`msg:${messageId}`]) return patch(state, key, (it) => ({ ...it, message_id: messageId }));
  return dropLocalUser(state, localId);
}

export function dropLocalUser(state, localId) {
  const key = `local:${localId}`;
  if (!state.items[key]) return state;
  const items = { ...state.items };
  delete items[key];
  return { ...state, items, order: state.order.filter((k) => k !== key) };
}

export function applyEvents(state, events) {
  return (events || []).reduce(applyEvent, state);
}

// 用 S-05 快照校正会话状态（打开页面、断档重连后）
export function applySnapshot(state, snap) {
  if (!snap) return state;
  return {
    ...state,
    session: {
      state: snap.state,
      since: state.session.since,
      turn_started_at: snap.turn_started_at ?? null,
      reason: state.session.reason,
    },
    queue: snap.state === 'queued' ? (state.queue || { position: snap.queue_position, ahead: Math.max(0, (snap.queue_position || 1) - 1), pool: snap.pool }) : null,
    next: snap.next === undefined ? state.next : snap.next,
    context: snap.context || state.context,
    configPending: snap.config_pending || state.configPending,
  };
}

const ASSISTANT_SIDE = new Set([
  'assistant', 'progress', 'card', 'permission', 'error', 'interrupted', 'config_pending',
  'reconnected', 'compacted', 'subagent',
]);

/**
 * 时间线分块：连续的助手侧内容合成一块（同一个署名），决策问题单独成块，
 * 已回答的问题收起成一行「步骤 2 选了 A：…」。
 */
export function timelineBlocks(state) {
  const blocks = [];
  const answeredSummaries = new Set(
    state.order.map((k) => state.items[k]).filter((x) => x.kind === 'question' && x.state === 'answered' && x.summary)
      .map((x) => x.summary),
  );
  for (const key of state.order) {
    const it = state.items[key];
    if (!it) continue;
    if (it.kind === 'notice' && it.notice_kind === 'decided' && answeredSummaries.has(it.text)) continue;
    if (ASSISTANT_SIDE.has(it.kind)) {
      const last = blocks[blocks.length - 1];
      if (last && last.type === 'assistant') last.items.push({ key, ...it });
      else blocks.push({ type: 'assistant', key: `b:${key}`, ts: it.ts, items: [{ key, ...it }] });
      continue;
    }
    if (it.kind === 'question') {
      if (it.state === 'answered') blocks.push({ type: 'decided', key, item: it });
      else if (it.state === 'withdrawn') blocks.push({ type: 'withdrawn', key, item: it });
      else blocks.push({ type: 'question', key, item: it });
      continue;
    }
    blocks.push({ type: it.kind, key, item: it });
  }
  return blocks;
}

// 输入框上方的运行状态行：这一轮在做哪一步（工作过程的标题）、此刻在等什么、从什么时候开始。
// 只看这一轮：往回找到开启这一轮的用户消息为止（运行中插的话不算开启新一轮）
export function runStatus(state) {
  if (!['running', 'connecting'].includes(state.session.state)) return null;
  let title = '';
  let doing = '';
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const it = state.items[state.order[i]];
    if (!it) continue;
    if (it.kind === 'user') {
      if (it.delivery === 'steer') continue;
      break;
    }
    if (!doing) {
      if (it.kind === 'question' && it.state === 'open') doing = '等你回答';
      else if (it.kind === 'permission' && !it.resolved) doing = '等你确认';
      else if (it.kind === 'subagent' && ['started', 'running'].includes(subagentStatus(it, false))) doing = `等子任务「${it.title || '子任务'}」`;
      else if (it.kind === 'progress' && it.state === 'running') {
        const step = [...(it.steps || [])].reverse().find((x) => x.state === 'running');
        if (step?.text) doing = step.text;
      }
    }
    if (!title && it.kind === 'progress' && it.title) title = it.title;
  }
  // 建模命令开始前平台在等资料转成文本（会话状态 reason=waiting_materials），还没交给助手
  if (!doing && state.session.reason === 'waiting_materials') doing = '等资料转成文本';
  if (!doing) doing = state.session.state === 'connecting' ? '正在连接' : '思考中';
  return { title, doing, since: state.session.turn_started_at || state.session.since || null, thinking: state.thinking || '' };
}

// 等你决定的问题：输入框上方的建议条要禁用主推
export function openQuestions(state) {
  return state.order.map((k) => state.items[k]).filter((x) => x.kind === 'question' && x.state === 'open');
}

// 子任务行的状态：以生命周期终态为准（ADR-0009）。所在的工作过程已结束、或会话已停下，
// 还没收到终态的子任务不会再有结果，显示已中断，不再转圈；那一轮判定执行超时的显示执行超时（ADR-0097）
export function subagentStatus(sub, ended) {
  if (sub.status === 'done' || sub.status === 'failed') return sub.status;
  if (sub.ended_by) return sub.ended_by;
  return ended ? 'interrupted' : sub.status;
}

// 一轮收尾（回到空闲、出错、关闭、被打断）时还没终态的子任务记下怎么结束的：子代理活不过它所在的一轮，
// 下一轮开始后这些卡也不再转圈；按历史回放同样成立，升级前留下的卡也能恢复
const TURN_END_STATES = ['idle', 'error', 'closed'];
function endOpenSubagents(state, how) {
  let items = null;
  state.order.forEach((k) => {
    const it = state.items[k];
    if (it?.kind !== 'subagent' || it.ended_by || ['done', 'failed'].includes(it.status)) return;
    items = items || { ...state.items };
    items[k] = { ...it, ended_by: how };
  });
  return items ? { ...state, items } : state;
}

export function isBusy(state) {
  return ['running', 'connecting', 'queued'].includes(state.session.state);
}

export function findCard(state, cardType) {
  const items = state.order.map((k) => state.items[k]).filter((x) => x.kind === 'card' && x.card.card_type === cardType);
  return items.length ? items[items.length - 1].card : null;
}
