// 上下文用量（稳定引擎）：已用量取最近一次主代理模型调用的 context_tokens，
// 模型窗口与自动压缩触发点随每轮结束的 context_usage 事件下发。
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
  if (event.type === 'session_started' && !event.resumed) {
    return EMPTY_CONTEXT; // 未续接即全新上下文
  }
  return ctx;
}

export function formatTokens(count) {
  if (count < 1000) return `${count}`;
  return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
}

export function describeContext({ used, window, compactAt }) {
  if (!used || !window) return null;
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

// CLI 的自动压缩规则：有效窗口 = min(压缩窗口, 模型窗口) − min(最大输出, 20000)，
// 触发阈值 = 有效窗口 − 13000；设了触发百分比时取 min(有效窗口 × 百分比, 上式)。
const OUTPUT_RESERVE_CAP = 20000;
const AUTOCOMPACT_BUFFER = 13000;

// 配置页估算：values 为上下文配置表单值，未填模型真实窗口时按 defaultWindow（服务端默认值），
// 两者都没有时返回 null
export function estimateCompaction(values, defaultWindow) {
  const real = values?.max_context_tokens || defaultWindow;
  if (!real) return null;
  const window = Math.min(values?.auto_compact_window || real, real);
  const output = values?.max_output_tokens;
  const effective = window - Math.min(output || OUTPUT_RESERVE_CAP, OUTPUT_RESERVE_CAP);
  let threshold = effective - AUTOCOMPACT_BUFFER;
  if (values?.autocompact_pct) {
    threshold = Math.min(threshold, Math.floor((effective * values.autocompact_pct) / 100));
  }
  if (!output) return { threshold };
  const margin = real - threshold - output;
  return { threshold, output, margin, ratio: margin / real };
}
