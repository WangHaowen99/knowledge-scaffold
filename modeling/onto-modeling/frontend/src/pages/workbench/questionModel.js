// 决策卡的答复（契约 S-11）：单选传 option_id，多选题（q.multi）传 option_ids；选了「其他，我来说」要带上文字，没填不能提交
export function decisionAnswer(q, picks, custom) {
  const chosen = q.options.filter((o) => picks.includes(o.id));
  const needsText = chosen.some((o) => o.custom);
  if (!chosen.length || (needsText && !custom.trim())) return null;
  const custom_text = needsText ? custom.trim() : undefined;
  return q.multi ? { option_ids: chosen.map((o) => o.id), custom_text } : { option_id: chosen[0].id, custom_text };
}
