// 上下文用量（两个引擎同一套事件）：已用量取最近一次主代理模型调用的 context_tokens，
// 模型窗口与自动压缩触发点在连接时已知的随 session_started 下发，并随每轮结束的 context_usage 更新。
// result 事件里的 usage 是一轮内多次调用的累加，不能拿来算上下文。

export const EMPTY_CONTEXT = { used: null, window: null, compactAt: null };

export function nextContext(ctx, event) {
  if (event.type === 'assistant_message' && !event.subagent && event.context_tokens) {
    return { ...ctx, used: event.context_tokens };
  }
  if (event.type === 'context_usage') {
    // 窗口未知（重连后引擎还没取到）时沿用上一轮的窗口与触发点
    const limits = event.window
      ? { window: event.window, compactAt: event.compact_at ?? null }
      : { window: ctx.window, compactAt: ctx.compactAt };
    return { used: event.used ?? null, ...limits };
  }
  if (event.type === 'context_compacted') {
    return { ...ctx, used: event.post_tokens ?? null };
  }
  if (event.type === 'session_started') {
    const base = event.resumed ? ctx : EMPTY_CONTEXT; // 未续接即全新上下文
    // 切换引擎或改配置后续接，旧窗口与触发点不再适用
    if (!event.window) return base;
    return { ...base, window: event.window, compactAt: event.compact_at ?? null };
  }
  return ctx;
}

export function formatTokens(count) {
  if (count < 1000) return `${count}`;
  return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
}

// 新会话还没调用过模型：显示空环，免得只看到旁边的项目累计用量，误以为这条会话已经用了那么多
const FRESH_CONTEXT = { pct: 0, level: '', text: '新会话，尚未调用模型；发出第一条消息后显示上下文用量' };

export function describeContext({ used, window, compactAt }, { fresh = false } = {}) {
  if (!used || !window) return fresh ? FRESH_CONTEXT : null;
  const pct = Math.min(100, Math.round((used / window) * 100));
  const remaining = Math.max(0, window - used);
  let text = `上下文已用 ${formatTokens(used)} / ${formatTokens(window)}（${pct}%），剩余 ${formatTokens(remaining)}`;
  let ratio = used / window;
  if (compactAt) {
    text +=
      used < compactAt
        ? `；再用约 ${formatTokens(compactAt - used)} 自动压缩`
        : '；已到自动压缩点，下次请求前会先压缩';
    ratio = used / compactAt;
  }
  const level = ratio >= 0.9 ? 'danger' : ratio >= 0.75 ? 'warn' : '';
  return { pct, level, text };
}

export function compactionNotice(event) {
  const label = event.trigger === 'manual' ? '上下文已压缩' : '上下文已自动压缩';
  return event.pre_tokens ? `${label}（压缩前约 ${formatTokens(event.pre_tokens)} tokens）` : label;
}
