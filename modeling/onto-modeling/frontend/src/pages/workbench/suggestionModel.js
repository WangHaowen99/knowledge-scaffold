// 建议条（详设 6.10，F7）：规则表在后端（flow_state.suggest），这里只管拿哪一份、什么时候不显示、
// 点下去之前情况变了怎么办。
//
// - 对话流推来的和 FL-02 刷新取到的，取版本号（rev）大的那份；F7 之前的历史记录没有版本号，按 0 算
// - 助手在跑（排队、连接中）时不显示，自主构建的运行控制照常显示
// - 建议条发起的请求都带 from_suggestion；接口回 409 suggestion_stale 时带回重算后的一份，直接换上

const BUSY = ['running', 'connecting', 'queued'];

export function pickSuggestions(pushed, fetched) {
  if (!pushed) return fetched || null;
  if (!fetched) return pushed;
  return (fetched.rev || 0) > (pushed.rev || 0) ? fetched : pushed;
}

export function suggestionBarHidden(sug, sessionState) {
  if (sug?.mode === 'autobuild_controls') return false;
  return BUSY.includes(sessionState) || sug?.mode === 'running';
}

// 会话已经停下，手里那份却还是「运行中」（推送丢了）：要重新取一份
export function needsRefetch(sug, sessionState) {
  return sug?.mode === 'running' && !BUSY.includes(sessionState);
}

export function staleSuggestions(error) {
  return error?.code === 'suggestion_stale' ? error.details?.suggestions || null : null;
}
