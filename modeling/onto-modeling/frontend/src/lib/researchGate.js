// 资料分析验收对阶段展示的影响。纯函数、不依赖 React，便于 node --test。
//
// review 是后端项目进度里的 research_review：
//   status    none / pending / failed / passed
//   blocking  未验收是否还挡着后续阶段——只在对象尚未提取时为 true
// 挡不挡由后端判定，这里不自行推断：对象一旦提取，资料分析按已越过处理，只留角标
// 提醒，否则旧项目升级后进度会整体归零、导出入口消失。

export function isResearchBlocking(review) {
  return review?.blocking === true;
}

// 报告挡着后续阶段时，把核验入口排到最前；其余入口保持原有顺序。
// 这时最该做的是核验现有报告，而不是重新做资料分析。
export function orderResearchActions(actions, review) {
  if (!isResearchBlocking(review)) return actions;
  const verify = actions.filter((action) => action.verify);
  const others = actions.filter((action) => !action.verify);
  return [...verify, ...others];
}
