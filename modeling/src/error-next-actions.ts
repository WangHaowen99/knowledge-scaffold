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
  CHECK_FINDING_REVIEW_STALE: [
    { ...VIEW_VALIDATION, style: 'primary' },
    RUN_CHECK,
  ],
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
  // 项目写租约被别的长耗时操作占着（生成脚本方案要跑十几分钟）。正确出口是等，
  // 但得让人看到「在等什么、等了多久」，而不是一条不知所措的红字。
  busy: [
    { id: 'refresh_task_graph', label: '刷新查看当前进度', style: 'primary',
      description: '后端正在处理上一个操作，完成后界面会自动更新' },
  ],
  expired: [
    { id: 'refresh_task_graph', label: '刷新后重试', style: 'primary',
      description: '上一个操作已超时释放，刷新后可以重新提交' },
  ],
  // 与 busy 是同一件事的两个码。没有合并成一个：busy 来自租约层且历史上就是小写，
  // 把大写的这个改成小写等于往错误方向统一。两边都登记，出口一致即可。
  PROJECT_BUSY: [
    { id: 'refresh_task_graph', label: '刷新查看当前进度', style: 'primary',
      description: '项目里还有任务在跑，完成后再试' },
  ],
  // 增量补充要有一个确认过的完整知识包做基准，否则「只改增量」没有参照物
  INCREMENT_REQUIRES_COMPLETED_PACKAGE: [
    { id: 'confirm_construction', label: '先完成建模确认', style: 'primary',
      description: '确认完成 AI 辅助构建之后才能补充增量资料' },
    REVIEW_KNOWLEDGE,
  ],
  INCREMENT_ROUND_NOT_STARTED: [
    { id: 'refresh_task_graph', label: '刷新查看当前状态', style: 'primary',
      description: '当前没有进行中的增量补充，请先开始一轮' },
  ],
  INCREMENT_MATERIALS_REQUIRED: [
    { id: 'upload_materials', label: '上传增量资料', style: 'primary',
      description: '先把这次要补充的资料传上来，再生成修改方案' },
  ],
  INCREMENT_ROUND_BLOCKED: [
    { id: 'refresh_task_graph', label: '刷新查看进行中的任务', style: 'primary',
      description: '当前还有任务没处理完，处理完再开始增量补充' },
  ],
  // 流程图确认被拒的四种情况。画布就在中间栏，用户此刻正在编辑——原先这些只回一行
  // 红字，人被卡在画到一半的图上没有任何可点的东西。
  FLOWCHART_DIRECT_EDIT_REQUIRED: [
    { id: 'open_flowchart_editor', label: '回到流程图画布', style: 'primary',
      description: '直接在画布上改完并保存，再提交确认' },
  ],
  FLOWCHART_DRAFT_OUTDATED: [
    { id: 'refresh_flowchart_draft', label: '刷新到最新草稿', style: 'primary',
      description: '草稿在别处被改过，刷新后确认内容再提交' },
  ],
  FLOWCHART_EMPTY_DOCUMENT: [
    { id: 'add_flowchart_process', label: '新建一个业务流程', style: 'primary',
      description: '至少要有一个流程才能提交确认' },
    { id: 'open_flowchart_editor', label: '回到流程图画布', style: 'secondary' },
  ],
  FLOWCHART_WARNINGS_NOT_ACKNOWLEDGED: [
    { id: 'acknowledge_flowchart_warnings', label: '查看并确认提醒', style: 'primary',
      description: '提醒不阻断提交，但需要你确认已经知悉' },
  ],
  FLOWCHART_CANDIDATE_UNAVAILABLE: [
    { id: 'refresh_flowchart_draft', label: '刷新候选列表', style: 'primary',
      description: '该候选可能已被添加或已失效' },
    { id: 'add_flowchart_process', label: '换一个流程来源', style: 'secondary' },
  ],
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
