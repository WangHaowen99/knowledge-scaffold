import type { AiConstructionTaskGraphSnapshot } from './task-graph.js';

/**
 * 「自动修复错误」是否可用，由后端声明而不是前端按错误数猜。
 *
 * 前端原先只看 counts.错误 > 0 就把按钮画出来，遇到未发布草稿时点击静默返回、
 * 毫无反馈；遇到没有可承载修复的里程碑时则报出内部文案。现在把判定和原因一起下发。
 */
export interface PackageRepairAvailability {
  available: boolean;
  reason?: string;
}

export interface PackageRepairAvailabilityInput {
  errorCount: number;
  hasActiveDraft: boolean;
  graph: Pick<AiConstructionTaskGraphSnapshot, 'milestones' | 'tasks'> | null;
}

export function packageRepairAvailability(
  input: PackageRepairAvailabilityInput,
): PackageRepairAvailability {
  if (input.errorCount < 1) {
    return { available: false, reason: '当前修订没有阻断错误，无需修复' };
  }
  if (input.hasActiveDraft) {
    return { available: false, reason: '存在未发布的人工编辑草稿，请先发布或放弃草稿' };
  }
  if (!input.graph) {
    return { available: false, reason: '项目尚未初始化 AI 辅助构建' };
  }
  const hasActiveMilestone = input.graph.milestones.some((item) => item.status === 'active');
  if (hasActiveMilestone) return { available: true };

  // 没有活动里程碑时，受控修复靠重做最近一次已完成的校验任务把阶段重新打开
  const reopenable = input.graph.tasks.some((item) => (
    item.kind === 'package_validation' && item.status === 'completed'
  ));
  return reopenable
    ? { available: true }
    : { available: false, reason: '当前没有可承载修复的建模阶段，请先完成知识结构生成' };
}
