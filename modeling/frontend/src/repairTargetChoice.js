/**
 * 「这个校验问题涉及多个知识资源，先说改哪一个」的兜底出口。
 *
 * 后端在 expert_decision 提问时就会带上选择题，但那是**建题那一刻**的判断：
 * 早于本次修复创建的待办没有这道题，知识包变动后原本唯一的目标也可能变成多个。
 * 这两种情况下提交必然撞回同一条错误，而界面上没有任何地方能指定目标——死路。
 *
 * 所以在**提交失败时**再兜一次：后端 409 里已经带了 candidates，把它就地渲染成
 * 可选项，选完带着 repair_target 重新提交即可。不依赖待办创建时写进去的题目。
 */
export function repairTargetCandidatesFrom(error) {
  if (error?.data?.code !== 'REPAIR_TARGET_UNRESOLVED') return [];
  const list = Array.isArray(error.data.candidates) ? error.data.candidates : [];
  return list
    .filter((item) => item && item.targetKind && item.targetKey)
    .map((item) => ({
      targetKind: String(item.targetKind),
      targetKey: String(item.targetKey),
      value: `${item.targetKind}:${item.targetKey}`,
    }));
}

/** 把选中的目标并进回答；重复提交时覆盖而不是叠加 */
export function withRepairTarget(response, value) {
  if (!value) return response;
  const answers = Array.isArray(response?.answers) ? response.answers : [];
  return {
    ...response,
    answers: [
      ...answers.filter((item) => item?.questionId !== 'repair_target'),
      { questionId: 'repair_target', selected: [value] },
    ],
  };
}
