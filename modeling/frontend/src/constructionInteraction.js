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

export function isConstructionInteraction(interaction) {
  if (!interaction || typeof interaction !== 'object') return false;
  if (CONSTRUCTION_TYPES.has(interaction.type)) return true;
  return CONSTRUCTION_PURPOSES.has(interaction.payload?.purpose);
}

export function isCurrentConstructionInteraction(interaction, currentRevision) {
  if (!isConstructionInteraction(interaction)) return false;
  const purpose = interaction.payload?.purpose;
  const controlledRepair = purpose === 'controlled_repair'
    || purpose === 'controlled_repair_expert';
  if (!controlledRepair || !interaction.baseRevision || !currentRevision) return true;
  return interaction.baseRevision === currentRevision;
}

function interactionPriority(interaction) {
  const purpose = interaction?.payload?.purpose;
  if (purpose === 'controlled_repair' || purpose === 'controlled_repair_expert') return 2;
  if (purpose === 'check_finding_ignore') return 0;
  return 1;
}

/**
 * 助手栏只展示一个待办。受控修复确认必须优先于旧的「先不改」理由表单，
 * 同一优先级仍保持后端创建顺序，避免其他流程的交互顺序被意外改变。
 */
export function selectCurrentConstructionInteraction(interactions = [], currentRevision) {
  let selected = null;
  for (const interaction of interactions) {
    if (!isCurrentConstructionInteraction(interaction, currentRevision)) continue;
    if (!selected || interactionPriority(interaction) > interactionPriority(selected)) {
      selected = interaction;
    }
  }
  return selected;
}
