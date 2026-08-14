export function currentFindingIgnoreInteraction(interactions = [], currentRevision) {
  return interactions.find((interaction) => {
    if (interaction?.payload?.purpose !== 'check_finding_ignore') return false;
    if (interaction.status && interaction.status !== 'pending') return false;
    if (currentRevision && interaction.baseRevision
      && interaction.baseRevision !== currentRevision) return false;
    return true;
  }) ?? null;
}

export function findingIgnoreInteraction(interactions = [], finding, currentRevision) {
  if (!finding?.id) return null;
  const interaction = currentFindingIgnoreInteraction(interactions, currentRevision);
  // 错误放行表单提交时仍会整批处理全部错误，但“前往助手处理”只属于最初
  // 点开的那条发现。否则所有错误按钮会一起变文案，看起来像同时开了多张表单。
  return interaction?.payload?.findingId === finding.id ? interaction : null;
}
