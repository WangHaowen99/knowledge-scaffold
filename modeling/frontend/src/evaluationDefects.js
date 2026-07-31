const CLOSED_RESOLUTIONS = new Set(['resolved', 'irrelevant']);

/**
 * 只有归因到知识包本身、且尚未被专家关掉的失败原因才值得回流建模。
 * 运行环境、数据和用例问题改知识包解决不了，回流只会制造无效修缮任务。
 */
export function canPlanEvaluationRepair(diagnosis) {
  return diagnosis?.repairScope === 'package'
    && !CLOSED_RESOLUTIONS.has(String(diagnosis?.resolutionStatus ?? 'pending'));
}

export function selectableEvaluationDefects(diagnoses = []) {
  return diagnoses.filter(canPlanEvaluationRepair);
}

export function toggleEvaluationDefect(selected, id) {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** 选中集可能包含已被重新诊断替换掉的 id，提交前按当前可选项过滤 */
export function submittableEvaluationDefectIds(diagnoses = [], selected = new Set()) {
  return selectableEvaluationDefects(diagnoses)
    .filter((diagnosis) => selected.has(diagnosis.id))
    .map((diagnosis) => diagnosis.id);
}
