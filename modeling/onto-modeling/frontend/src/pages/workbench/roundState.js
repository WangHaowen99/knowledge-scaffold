// 「这一轮改了什么」卡的状态（F3：一轮结束就合入成版本，没有「确认生效」）：
// merged 已存成版本（可撤回这一轮）/ reverted 已撤回 / conflict 和你的修改冲突（去待处理）；
// 升级前留下的卡（pending_confirm 等）按服务端给的 status 显示，不再出确认按钮
export function roundState(card) {
  if (card?.state === 'reverted' || card?.state === 'discarded') return 'reverted';
  if (card?.state === 'merged' || card?.state === 'conflict') return card.state;
  return 'pending';
}

// 「查看改动」（CS-03 items）→ 差异行：按条目分段，段首是后端下发的业务名（target.label）；
// 认不出的文件合成最后一段「另有 N 处改动」，不出文件名和路径
export function roundDiffLines(items = []) {
  const body = (list) => list.flatMap((it) => (it.sections || []).flatMap((s) => s.lines || []));
  const known = items.filter((it) => it.target?.label);
  const rest = items.filter((it) => !it.target?.label);
  return [
    ...known.flatMap((it) => [{ kind: 'header', text: it.target.label }, ...body([it])]),
    ...(rest.length ? [{ kind: 'header', text: `另有 ${rest.length} 处改动` }, ...body(rest)] : []),
  ];
}
