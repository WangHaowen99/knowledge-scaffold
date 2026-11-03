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
    context: null,
    suggestions: null,
    usage: { input_tokens: 0, output_tokens: 0, cost: null, turns: 0 },
    configPending: null,
    engineLabel: null,
    thinking: '', // 这一次模型调用到目前的思考片段（瞬时，只给「思考中」看）
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

// 同一个子任务的更新按到达顺序合并（后到的字段盖住先到的）；空的进展说法不盖掉先到的
function mergeSubagent(prev, sub) {
  const { step: _step, ...rest } = sub;
  return { ...prev, ...rest, progress_text: sub.progress_text || prev?.progress_text || '' };
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
      return {
        ...state,
        thinking: d.state === 'running' ? state.thinking : '',
        session: { state: d.state, since: d.since || ts, turn_started_at: d.turn_started_at ?? null, reason: d.reason ?? null },
        queue: d.state === 'queued' ? state.queue : null,
      };
    case 'session.queued':
      return { ...state, queue: { ...d } };
    case 'session.started':
      return { ...state, engineLabel: d.engine_label || state.engineLabel };
    case 'session.error':
      return upsert(state, `err:${d.error_id || seq}`, { ...at, ...d, kind: 'error' });
    case 'session.interrupted':
      return upsert(state, `int:${seq ?? ts}`, { ...at, ...d, kind: 'interrupted' });
    case 'session.config_pending':
      return upsert({ ...state, configPending: d }, `cfg:${seq ?? ts}`, { ...at, ...d, kind: 'config_pending' });
    case 'session.reconnected':
      return upsert({ ...state, configPending: null }, `recon:${seq ?? ts}`, { ...at, ...d, kind: 'reconnected' });

    case 'message.user':
      return upsert(state, `msg:${d.message_id}`, { ...at, ...d, kind: 'user' });
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
      return upsert(state, key, { ...mergeSubagent(cur, d), ts: cur?.ts || ts, kind: 'subagent' });
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
      const key = `card:${d.card_id}`;
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

/**
 * 助手在跑、却没有别的东西在动时（刚发出消息、两次工具调用之间模型在想），时间线末尾显示「思考中」：
 * 正文在流、有在跑的步骤或子任务、在等你回答时不显示。返回 null 或 {kind: 'waiting'|'thinking', thinking}。
 */
export function liveActivity(state) {
  if (state.session.state !== 'running') return null;
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const it = state.items[state.order[i]];
    if (!it || it.kind === 'notice' || it.kind === 'day') continue;
    if (it.kind === 'assistant' && it.streaming) return null;
    if (it.kind === 'question' && it.state === 'open') return null;
    if (it.kind === 'permission' && !it.resolved) return null;
    if (it.kind === 'subagent') {
      if (!['done', 'failed'].includes(subagentStatus(it, false))) return null; // 子任务卡自己在转
      continue;
    }
    if (it.kind === 'progress' && it.state === 'running' && (it.steps || []).some((s) => s.state === 'running')) return null;
    if (it.kind === 'progress' || it.kind === 'assistant' || it.kind === 'user' || it.kind === 'card') break;
  }
  return { kind: state.thinking ? 'thinking' : 'waiting', thinking: state.thinking };
}

// 等你决定的问题：输入框上方的建议条要禁用主推
export function openQuestions(state) {
  return state.order.map((k) => state.items[k]).filter((x) => x.kind === 'question' && x.state === 'open');
}

// 子任务行的状态：以生命周期终态为准（ADR-0009）。所在的工作过程已结束、或会话已停下，
// 还没收到终态的子任务不会再有结果，显示已中断，不再转圈
export function subagentStatus(sub, ended) {
  if (sub.status === 'done' || sub.status === 'failed') return sub.status;
  return ended ? 'interrupted' : sub.status;
}

export function isBusy(state) {
  return ['running', 'connecting', 'queued'].includes(state.session.state);
}

export function findCard(state, cardType) {
  const items = state.order.map((k) => state.items[k]).filter((x) => x.kind === 'card' && x.card.card_type === cardType);
  return items.length ? items[items.length - 1].card : null;
}
