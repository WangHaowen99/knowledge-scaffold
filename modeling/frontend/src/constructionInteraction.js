const CONSTRUCTION_TYPES = new Set(['change_plan', 'knowledge_conflict']);
const CONSTRUCTION_PURPOSES = new Set([
  'modeling_brief',
  'process_interview',
  'process_diagram_revision',
  'process_review',
  'script_decision',
  'script_contract',
  'script_generation',
  'generation_gate_skip',
  'controlled_repair_expert',
  'check_finding_ignore',
]);
const REVISION_BOUND_PURPOSES = new Set([
  'controlled_repair',
  'controlled_repair_expert',
  'check_finding_ignore',
]);

export function isConstructionInteraction(interaction) {
  if (!interaction || typeof interaction !== 'object') return false;
  if (CONSTRUCTION_TYPES.has(interaction.type)) return true;
  return CONSTRUCTION_PURPOSES.has(interaction.payload?.purpose);
}

export function isPendingQueueInteraction(interaction) {
  return isConstructionInteraction(interaction) || interaction?.type === 'scenario_review';
}

export function isCurrentConstructionInteraction(interaction, currentRevision) {
  if (!isConstructionInteraction(interaction)) return false;
  const purpose = interaction.payload?.purpose;
  if (!REVISION_BOUND_PURPOSES.has(purpose) || !interaction.baseRevision || !currentRevision) return true;
  return interaction.baseRevision === currentRevision;
}

function interactionPriority(interaction) {
  const purpose = interaction?.payload?.purpose;
  if (purpose === 'controlled_repair' || purpose === 'controlled_repair_expert') return 2;
  if (purpose === 'check_finding_ignore') return 0;
  return 1;
}

function interactionQueueOrder(interaction) {
  const order = Number(interaction?.payload?.queue?.order);
  return Number.isFinite(order) ? Math.max(0, Math.trunc(order)) : Number.MAX_SAFE_INTEGER;
}

function interactionCreatedAt(interaction) {
  const timestamp = Date.parse(interaction?.createdAt ?? interaction?.created_at ?? '');
  return Number.isFinite(timestamp) ? timestamp : 0;
}

/**
 * 所有当前待办都进入同一条可见队列。后端给出显式 order 时优先使用；旧数据没有
 * queue 元数据时沿用修复优先级和创建顺序，保证升级后不会改变既有处理语义。
 */
export function constructionInteractionQueue(interactions = [], currentRevision) {
  return interactions
    .filter((interaction) => isPendingQueueInteraction(interaction))
    .filter((interaction) => interaction?.type === 'scenario_review'
      || isCurrentConstructionInteraction(interaction, currentRevision))
    .map((interaction, sourceIndex) => ({ interaction, sourceIndex }))
    .sort((left, right) => (
      interactionQueueOrder(left.interaction) - interactionQueueOrder(right.interaction)
      || interactionPriority(right.interaction) - interactionPriority(left.interaction)
      || interactionCreatedAt(left.interaction) - interactionCreatedAt(right.interaction)
      || left.sourceIndex - right.sourceIndex
    ))
    .map(({ interaction }) => interaction);
}

/** 返回队列中当前可在助手栏处理的题卡；完整队列由 constructionInteractionQueue 展示。 */
export function selectCurrentConstructionInteraction(interactions = [], currentRevision) {
  return constructionInteractionQueue(interactions, currentRevision)
    .find(isConstructionInteraction) ?? null;
}
