// 决策卡（契约 S-11、EC-33、EC-34）的纯逻辑：答复体、回答后的只读回显、失效时保留的草稿。界面在 questions.jsx

// 单选传 option_id，多选题（q.multi）传 option_ids；选了「其他，我来说」要带上文字，没填不能提交
export function decisionAnswer(q, picks, custom) {
  const chosen = q.options.filter((o) => picks.includes(o.id));
  const needsText = chosen.some((o) => o.custom);
  if (!chosen.length || (needsText && !custom.trim())) return null;
  const custom_text = needsText ? custom.trim() : undefined;
  return q.multi ? { option_ids: chosen.map((o) => o.id), custom_text } : { option_id: chosen[0].id, custom_text };
}

// 回答记录里有选项或文字（旧记录、一次答完的只有一句「已回答」，回显不了）
export const hasStructuredAnswer = (answer) => !!(answer && (answer.option_id || answer.option_ids?.length || answer.custom_text));

// 按回答记录还原勾选：只认 option_id / option_ids，只写了文字时勾「其他，我来说」；不从文字猜选项
export function answerPicks(q, answer) {
  if (!answer) return [];
  const ids = answer.option_ids?.length ? answer.option_ids : answer.option_id ? [answer.option_id] : [];
  const custom = q.options.find((o) => o.custom);
  const picks = ids.length ? ids : answer.custom_text && custom ? [custom.id] : [];
  return picks.filter((id) => q.options.some((o) => o.id === id));
}

// 新卡的草稿：只剩「其他，我来说」（文字题，或模型给的选项都是占位）时直接勾上
export function initialDraft(q) {
  const only = q.options.length === 1 && q.options[0].custom ? [q.options[0].id] : [];
  return { picks: only, custom: '', expired: false };
}

// 草稿里有选过的正式选项或写过的字
export function draftFilled(q, draft) {
  if (!draft) return false;
  return !!draft.custom.trim() || draft.picks.some((id) => q.options.some((o) => o.id === id && !o.custom));
}

// 卡片显示哪种样子：answered 已回答（只读回显）、expired 已失效（只读显示没交上去的草稿）、sent 已提交等回执、open 可作答
export function decisionView(q, { answer = null, draft = null, withdrawn = false, reason = '', sent = false } = {}) {
  if (answer) {
    return { mode: 'answered', picks: answerPicks(q, answer), custom: answer.custom_text || '', locked: true, notice: '', body: null };
  }
  const d = draft || initialDraft(q);
  if (withdrawn || d.expired) {
    return {
      mode: 'expired', picks: d.picks, custom: d.custom, locked: true, body: null,
      notice: `${reason || '这个问题已失效'}：填写的内容没有提交，请让助手重新提问`,
    };
  }
  const body = decisionAnswer(q, d.picks, d.custom);
  return { mode: sent ? 'sent' : 'open', picks: d.picks, custom: d.custom, locked: sent, notice: '', body };
}
