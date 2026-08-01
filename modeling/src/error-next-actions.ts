/**
 * 按错误码集中派生「接下来能做什么」。
 *
 * 全仓有 300 多处错误响应、其中 70 多处是「当前状态不允许」的 409，逐个补出口
 * 既改不动也守不住。改成在响应层按 code 统一挂上默认出口：新增错误码只要在这里
 * 登记一次，前端就能把它渲染成可点的卡片，而不是又一条无路可走的红字。
 *
 * 只覆盖「有明确下一步」的码；纯输入校验（改表单即可）不在此列。
 */
export interface ErrorNextAction {
  id: string;
  label: string;
  style: 'primary' | 'secondary';
  description?: string;
  path?: string;
}

const RUN_CHECK: ErrorNextAction = {
  id: 'run_package_check', label: '立即执行知识包校验', style: 'primary',
};
const VIEW_VALIDATION: ErrorNextAction = {
  id: 'view_validation', label: '查看校验结果', style: 'secondary',
};
const REVIEW_KNOWLEDGE: ErrorNextAction = {
  id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary',
};
const GENERATE: ErrorNextAction = {
  id: 'generate_knowledge_structure', label: '生成知识结构', style: 'primary',
};
const EXPLAIN_GOAL: ErrorNextAction = {
  id: 'explain_goal', label: '填写建模目标', style: 'primary',
};

const BY_CODE: Record<string, ErrorNextAction[]> = {
  PACKAGE_CHECK_REQUIRED: [RUN_CHECK, VIEW_VALIDATION],
  PACKAGE_CHECK_FAILED: [
    { id: 'repair_knowledge_structure', label: '逐项修复校验错误', style: 'primary' },
    { id: 'defer_validation_errors', label: '暂缓错误并继续验证', style: 'secondary' },
    VIEW_VALIDATION,
  ],
  NO_BLOCKING_ERRORS: [{ ...VIEW_VALIDATION, style: 'primary' }],
  MODELING_GOAL_NOT_CONFIRMED: [EXPLAIN_GOAL],
  MODELING_BRIEF_NOT_FOUND: [EXPLAIN_GOAL],
  PACKAGE_NOT_FOUND: [GENERATE, REVIEW_KNOWLEDGE],
  PACKAGE_REVISION_REQUIRED: [GENERATE],
  GENERATION_RUN_NOT_RESUMABLE: [GENERATE],
  // 出口取决于「是有未发布草稿，还是建模没确认完」，按 code 静态判不了。
  // 抛错现场（routes.ts 发布分支）有上下文，会自己写 nextActions；这里只留兜底导航。
  RELEASE_CANDIDATE_REQUIRED: [{ ...REVIEW_KNOWLEDGE, style: 'primary' }, VIEW_VALIDATION],
  // 「重试」不能无条件重发：expectedRevision 的作用就是保证「确认的是你看到的那一版」。
  // reload_and_retry 只在知识包内容没变时才真的重发（任务图 revision 常因无关的
  // 后台任务往前走，那类冲突重发是安全的）；内容变了就退回让人重新看过。
  // 判据在 frontend/src/confirmReplay.js。
  REVISION_CONFLICT: [
    { id: 'reload_and_retry', label: '刷新并重试确认', style: 'primary',
      description: '内容没变就直接替你重发，变了会让你重新看过再确认' },
    { id: 'refresh_task_graph', label: '只刷新，我自己看', style: 'secondary' },
  ],
  PACKAGE_DRAFT_REQUIRES_PUBLISH: [
    { id: 'publish_package_draft', label: '发布草稿', style: 'primary',
      description: '把人工编辑的内容并入知识包，然后继续' },
    { id: 'discard_package_draft', label: '放弃草稿', style: 'secondary',
      description: '丢弃这些人工编辑，回到草稿之前的版本' },
    REVIEW_KNOWLEDGE,
  ],
  PACKAGE_REPAIR_IN_PROGRESS: [{ ...VIEW_VALIDATION, style: 'primary' }],
};

export function errorNextActions(code: unknown): ErrorNextAction[] | undefined {
  const key = typeof code === 'string' ? code : '';
  const actions = BY_CODE[key];
  return actions ? actions.map((item) => ({ ...item })) : undefined;
}

/** 已登记出口的错误码，供测试断言覆盖面 */
export function errorCodesWithNextActions(): string[] {
  return Object.keys(BY_CODE).sort();
}
