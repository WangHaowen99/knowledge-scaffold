// Publication is available after the required modeling stages through validation.
export const PUBLISH_REQUIRED_STAGES = ['research', 'object', 'operation', 'validate'];

export function isDirectoryIndex(path) {
  return path.toLowerCase().endsWith('/index.md');
}

export function missingPublishStages(stages, statuses) {
  return stages.filter((stage, index) =>
    PUBLISH_REQUIRED_STAGES.includes(stage.key) && statuses[index] !== 'finish');
}

// 发布检查中不能降级绕过的问题数：求解契约静态检查与结构自检（consumer 导入会拒绝的包）
export function hardBlockers(check) {
  return (check?.solver?.blocking || 0) + (check?.structure?.blocking || 0);
}

// 求解知识包总要打开发布检查：须确认求解能力尚未经端到端验证（验证在发布后进行）
export function needsPublishCheck(check) {
  return Boolean(check.issues.length || hardBlockers(check) || needsSolverAcknowledgement(check));
}

export function needsSolverAcknowledgement(check) {
  return Boolean(check?.solver?.requires_validation_after_publish);
}

// 检查之后才出现的问题（并发修改）：回到发布检查弹窗看最新结论
export function reopensPublishCheck(error) {
  const code = error?.code || '';
  return code === 'binding_problems' || code === 'package_structure_invalid' || code.startsWith('solver_');
}

export const STRUCTURE_TITLES = {
  package_archive_invalid: '压缩包',
  package_manifest_invalid: '清单',
  package_revision_invalid: '修订包',
  package_limit_exceeded: '数量上限',
  object_aliases_invalid: '对象别名',
  field_aliases_invalid: '字段别名',
  solver_package_unsafe: '求解包安全',
  solver_layout_invalid: '求解布局',
  package_export_failed: '导出失败',
};
