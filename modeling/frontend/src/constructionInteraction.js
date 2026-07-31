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
