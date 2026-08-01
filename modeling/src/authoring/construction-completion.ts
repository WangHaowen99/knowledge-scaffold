import type { AuthoringInteraction } from './types.js';

/**
 * 「确认完成 AI 辅助构建」被拦下时，除了说明原因，还要把「接下来怎么做」一起返回。
 * 否则前端只能渲染一句纯文本，用户被拦住却没有可点的出口。
 */
export interface CompletionBlockerAction {
  id: string;
  label: string;
  style: 'primary' | 'secondary';
  path?: string;
}

/** 待办所在位置：决定前端提示用户去哪里处理 */
export type PendingInteractionLocation = 'assistant' | 'scenario';

export interface BlockingPendingInteraction {
  id: string;
  type: string;
  purpose: string;
  title: string;
  location: PendingInteractionLocation;
}

const RESOURCE_FILES: Record<string, string> = {
  object: 'objects/index.md',
  relation: 'objects/index.md',
  operation: 'operations/index.md',
  rule: 'rules/index.md',
  metric: 'metrics/index.md',
  script: 'scripts/index.md',
};

const RESOURCE_LABELS: Record<string, string> = {
  object: '对象',
  relation: '对象',
  operation: '操作',
  rule: '规则',
  metric: '指标',
  script: '脚本',
};

/**
 * 把「object:xxx 缺少资料证据」这类问题项映射成可直接跳转的文件动作。
 * 按资源种类去重：同一份文件只给一个入口，避免十几个按钮糊在卡片上。
 */
export function evidenceProblemActions(problems: string[]): CompletionBlockerAction[] {
  // 按目标文件去重而不是按资源种类：object 与 relation 都落在 objects/index.md，
  // 按种类去重会给出两个指向同一份文件的按钮。
  const byPath = new Map<string, string>();
  for (const problem of problems) {
    const kind = String(problem).split(':')[0]?.trim();
    const path = kind ? RESOURCE_FILES[kind] : undefined;
    if (path && !byPath.has(path)) byPath.set(path, RESOURCE_LABELS[kind!]);
  }
  return [...byPath].map(([path, label], index) => ({
    id: 'open_package_file',
    label: `查看${label}文件`,
    style: index === 0 ? 'primary' : 'secondary',
    path,
  }));
}

function interactionTitle(interaction: AuthoringInteraction): string {
  const title = String(interaction.payload?.title ?? '').trim();
  if (title) return title.slice(0, 120);
  const purpose = String(interaction.payload?.purpose ?? '').trim();
  return purpose || interaction.type;
}

/**
 * 受控修复类待办一旦知识包修订变化就不再可处理，助手栏也不会渲染它
 * （见 frontend/src/constructionInteraction.js 的同名规则）。
 * 后端若仍把它算进阻塞项，用户会被拦住却在界面上找不到任何要处理的东西。
 */
export function isStaleRepairInteraction(
  interaction: AuthoringInteraction,
  currentRevision: string | undefined,
): boolean {
  const purpose = String(interaction.payload?.purpose ?? '');
  const controlledRepair = purpose === 'controlled_repair' || purpose === 'controlled_repair_expert';
  if (!controlledRepair) return false;
  if (!interaction.baseRevision || !currentRevision) return false;
  return interaction.baseRevision !== currentRevision;
}

export function blockingPendingInteractions(
  pending: AuthoringInteraction[],
  currentRevision: string | undefined,
): BlockingPendingInteraction[] {
  return pending
    .filter((interaction) => !isStaleRepairInteraction(interaction, currentRevision))
    .map((interaction) => ({
      id: interaction.id,
      type: interaction.type,
      purpose: String(interaction.payload?.purpose ?? ''),
      title: interactionTitle(interaction),
      location: interaction.type === 'scenario_review'
        ? 'scenario' as const
        : 'assistant' as const,
    }));
}

export function pendingInteractionActions(
  items: BlockingPendingInteraction[],
): CompletionBlockerAction[] {
  return items.some((item) => item.location === 'scenario')
    ? [{ id: 'open_scenario_review', label: '去场景推演处理', style: 'primary' }]
    : [];
}
