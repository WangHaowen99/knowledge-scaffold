/**
 * 建模助手动作目录：动作的语义在这里声明，前端按 kind 分发而不是按 id 猜。
 *
 * - command  ：调用后端写接口，执行期间助手进入忙碌态
 * - dialog   ：打开本地交互界面（确认弹窗、任务图编辑、文件选择器）
 * - navigate ：切换视图或定位内容，不产生写操作
 *
 * 新增动作必须在这里登记：消息写入时会校验 id，拼错或漏登记会当场失败，
 * 而不是等到用户点击按钮时毫无反应（历史上 open_optimization 就是这样成为孤儿的）。
 */
export type AssistantActionKind = 'command' | 'dialog' | 'navigate';

export const ASSISTANT_ACTION_KINDS: Record<string, AssistantActionKind> = {
  // ---- command：调后端 ----
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

  // ---- dialog：本地交互界面 ----
  defer_validation_errors: 'dialog',
  upload_materials: 'dialog',
  open_task_add: 'dialog',
  open_task_reorder: 'dialog',
  open_task_split: 'dialog',
  open_task_merge: 'dialog',

  // ---- navigate：切视图/定位 ----
  open_package_file: 'navigate',
  review_process_files: 'navigate',
  review_knowledge_files: 'navigate',
  review_materials: 'navigate',
  review_package: 'navigate',
  view_validation: 'navigate',
  open_optimization: 'navigate',
  open_scenario_review: 'navigate',
  explain_goal: 'navigate',
};

export function assistantActionKind(id: string): AssistantActionKind | undefined {
  return ASSISTANT_ACTION_KINDS[id];
}
