// 与后端 src/ai-construction/assistant-actions.ts 的目录一一对应。
// 有专门的测试比对两边，漏登记或拼错会在测试里暴露，而不是变成点了没反应的按钮。
export const ASSISTANT_ACTION_KINDS = {
  analyze_materials: 'command',
  confirm_materials: 'command',
  confirm_goal: 'command',
  confirm_process: 'command',
  confirm_knowledge: 'command',
  confirm_deterministic: 'command',
  confirm_construction: 'command',
  generate_knowledge_structure: 'command',
  repair_knowledge_structure: 'command',
  defer_script_generation: 'command',
  retry_script_generation: 'command',
  revise_goal: 'command',
  run_package_check: 'command',
  preview_task_adjustment: 'command',
  reload_and_retry: 'command',

  defer_validation_errors: 'dialog',
  publish_package_draft: 'dialog',
  discard_package_draft: 'dialog',
  upload_materials: 'dialog',
  open_task_add: 'dialog',
  open_task_reorder: 'dialog',
  open_task_split: 'dialog',
  open_task_merge: 'dialog',

  open_package_file: 'navigate',
  review_process_files: 'navigate',
  review_knowledge_files: 'navigate',
  review_materials: 'navigate',
  review_package: 'navigate',
  view_validation: 'navigate',
  open_optimization: 'navigate',
  open_scenario_review: 'navigate',
  explain_goal: 'navigate',
  refresh_task_graph: 'navigate',
};

/**
 * 导航动作的目标声明。原先这些散在一条 200 多行的 if 链里，
 * 其中 review_materials / review_package / view_validation 只是往输入框预填一句话，
 * 用户点了看不出发生什么——现在它们和其余导航一样真的切视图。
 */
export const NAVIGATION_TARGETS = {
  open_package_file: { file: (action) => action.path },
  review_process_files: { file: () => 'diagrams/index.md' },
  review_knowledge_files: { file: () => 'index.md' },
  review_package: { file: () => 'index.md' },
  review_materials: { step: 'ingest' },
  view_validation: { openPackageCheck: true },
  open_optimization: { evaluationView: 'optimize' },
  open_scenario_review: { step: 'scenario' },
  explain_goal: { focus: '[data-question-id="goal_text"]' },
  // 修订冲突后重拉快照。不自动重发原命令：expectedRevision 就是用来保证
  // 「确认的是你看到的那一版」，替用户换新 revision 重试等于把这道锁架空。
  refresh_task_graph: { reload: true },
};

export function assistantActionKind(action) {
  return action?.kind ?? ASSISTANT_ACTION_KINDS[action?.id] ?? null;
}

/** 导航类动作不改数据，流程图有未保存修改时也可以直接执行 */
export function isReadOnlyAction(action) {
  return assistantActionKind(action) === 'navigate';
}
