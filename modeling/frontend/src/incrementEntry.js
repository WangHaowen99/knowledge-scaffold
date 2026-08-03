/**
 * 「补充新资料」入口该不该出现。
 *
 * 它不是对话里的某个下一步，而是一个**状态可供性**：知识包建完之后随时可能来一批新资料。
 * 所以不走助手消息的 next_actions，而是常驻在助手栏顶部——用户不必先说点什么才能找到它。
 *
 * 出现条件刻意收得很紧：建模确认完成、且当前没有进行中的增量。后端也会再判一次
 * （INCREMENT_REQUIRES_COMPLETED_PACKAGE / INCREMENT_ROUND_BLOCKED），
 * 这里只是不要摆一个点了必然被拒的按钮。
 */
export const INCREMENT_MILESTONE_ID = 'increment_confirmed';

export function incrementEntryState(aiConstruction) {
  const milestones = aiConstruction?.milestones ?? [];
  const completed = milestones.find((item) => item.id === 'construction_completed');
  if (completed?.status !== 'confirmed') return { visible: false };

  const increment = milestones.find((item) => item.id === INCREMENT_MILESTONE_ID);
  const inRound = increment?.status === 'active';
  const intake = (aiConstruction?.tasks ?? []).find((task) => (
    task.kind === 'incremental_intake' && !['completed', 'skipped', 'cancelled'].includes(task.status)
  ));
  if (inRound || intake) {
    // 已经在一轮里：入口换成「生成方案」，而不是再开一轮
    return { visible: true, action: 'propose_increment', label: '生成增量修改方案' };
  }
  return { visible: true, action: 'start_increment_round', label: '补充新资料' };
}
