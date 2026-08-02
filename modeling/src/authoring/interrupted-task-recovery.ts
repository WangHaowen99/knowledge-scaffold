/**
 * 服务重启后，被打断的任务该给用户看什么。
 *
 * 重启恢复本身早就有了（restart-recovery.ts 会删租约、把执行中的 run 和任务标为
 * failed），但只有资料分析会写一条「已停止 + 重试」的消息。其余 12 种任务——包括
 * 最贵的知识生成（15~45 分钟）、脚本生成（10~40 分钟）、逐项修复（5~30 分钟）——
 * 只是在库里变成 failed，助手栏里一个字都没有：用户看到进度条停在那儿，
 * 不知道发生了什么，也没有任何可点的东西。这跟「报错却不给出口」是同一种死路。
 *
 * 这里给每种任务配上对应的重试动作。动作 id 必须在助手动作目录里登记过，
 * 有测试守着——否则又会造出点了没反应的按钮。
 */
import type { AiConstructionMessageBlock } from '../ai-construction/messages.js';
import type { AiConstructionTaskKind } from '../ai-construction/task-graph.js';

interface RecoveryAction {
  id: string;
  label: string;
  style: 'primary' | 'secondary';
  description?: string;
}

interface RecoveryPresentation {
  title: string;
  actions: RecoveryAction[];
}

const REVIEW_KNOWLEDGE: RecoveryAction = {
  id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary',
};

/**
 * 任务种类 → 中断后的出口。
 *
 * 没有列出的种类（modeling_brief、script_decision 等）是等用户回答的交互式任务，
 * 重启不会打断它们——restart-recovery 只结束 running 状态的任务，
 * 而这些任务处于 waiting_user，会被原样保留。
 */
const BY_KIND: Partial<Record<AiConstructionTaskKind, RecoveryPresentation>> = {
  material_analysis: {
    title: '建模资料分析已停止',
    actions: [
      { id: 'analyze_materials', label: '重新分析资料', style: 'primary' },
      { id: 'upload_materials', label: '补充建模资料', style: 'secondary' },
    ],
  },
  package_baseline_review: {
    title: '知识包基线检查已停止',
    actions: [
      { id: 'analyze_materials', label: '重新检查基线', style: 'primary' },
      REVIEW_KNOWLEDGE,
    ],
  },
  operation_extraction: {
    title: '业务操作提取已停止',
    actions: [
      { id: 'generate_knowledge_structure', label: '重新生成知识结构', style: 'primary' },
      REVIEW_KNOWLEDGE,
    ],
  },
  knowledge_extraction: {
    title: '知识结构生成已停止',
    actions: [
      { id: 'generate_knowledge_structure', label: '重新生成知识结构', style: 'primary',
        description: '上一轮生成被服务重启打断，需要重新跑一遍' },
      REVIEW_KNOWLEDGE,
    ],
  },
  script_generation: {
    title: '确定性脚本生成已停止',
    actions: [
      { id: 'retry_script_generation', label: '重新生成脚本', style: 'primary' },
      { id: 'defer_script_generation', label: '暂缓脚本生成', style: 'secondary',
        description: '先跳过脚本，继续后面的建模步骤' },
    ],
  },
  package_validation: {
    title: '知识包校验已停止',
    actions: [
      { id: 'run_package_check', label: '重新执行校验', style: 'primary' },
      { id: 'view_validation', label: '查看上次校验结果', style: 'secondary' },
    ],
  },
  package_repair: {
    title: '知识包逐项修复已停止',
    actions: [
      { id: 'repair_knowledge_structure', label: '继续逐项修复', style: 'primary' },
      { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
    ],
  },
  evaluation_repair: {
    title: '评测缺陷修缮已停止',
    actions: [
      { id: 'repair_knowledge_structure', label: '继续修缮', style: 'primary' },
      { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
    ],
  },
  process_diagram: {
    title: '流程图生成已停止',
    actions: [
      { id: 'review_process_files', label: '查看流程文件', style: 'primary' },
    ],
  },
  process_review: {
    title: '流程复核已停止',
    actions: [
      { id: 'review_process_files', label: '查看流程文件', style: 'primary' },
    ],
  },
  process_interview: {
    title: '流程访谈已停止',
    actions: [
      { id: 'review_process_files', label: '查看流程文件', style: 'primary' },
    ],
  },
  completion_review: {
    title: '建模完成检查已停止',
    actions: [
      { id: 'run_package_check', label: '重新执行校验', style: 'primary' },
      { id: 'view_validation', label: '查看校验结果', style: 'secondary' },
    ],
  },
};

export function interruptedTaskPresentation(kind: string): RecoveryPresentation | null {
  const found = BY_KIND[kind as AiConstructionTaskKind];
  return found ? { title: found.title, actions: found.actions.map((item) => ({ ...item })) } : null;
}

/** 供测试断言覆盖面 */
export function interruptedTaskKinds(): string[] {
  return Object.keys(BY_KIND).sort();
}

export function buildInterruptedTaskBlocks(input: {
  kind: string;
  reason: string;
  milestoneId: string;
  taskId: string;
  taskTitle?: string;
}): AiConstructionMessageBlock[] | null {
  const presentation = interruptedTaskPresentation(input.kind);
  if (!presentation) return null;
  return [
    {
      type: 'status',
      title: presentation.title,
      summary: input.taskTitle ? `中断的步骤：${input.taskTitle}` : '已保留当前进度',
      milestoneId: input.milestoneId,
      taskId: input.taskId,
      eta: { minMinutes: null, maxMinutes: null, confidence: 'low' },
    },
    { type: 'text', text: input.reason.trim().slice(0, 3_000) || '任务意外中断，请重新执行当前步骤。' },
    { type: 'next_actions', title: '可以这样继续', actions: presentation.actions },
  ];
}
