// 「这一轮改了什么」卡的状态（F3：一轮结束就合入成版本，没有「确认生效」）：
// merged 已存成版本（可撤回这一轮）/ reverted 已撤回 / conflict 和你的修改冲突（去待处理）；
// 升级前留下的卡（pending_confirm 等）按服务端给的 status 显示，不再出确认按钮
export function roundState(card) {
  if (card?.state === 'reverted' || card?.state === 'discarded') return 'reverted';
  if (card?.state === 'merged' || card?.state === 'conflict') return card.state;
  return 'pending';
}
