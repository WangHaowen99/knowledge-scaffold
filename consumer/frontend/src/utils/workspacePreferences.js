const VALID_AGENT_MODES = new Set(['quick', 'thinking', 'multi_agent']);

export const normalizeAgentMode = (mode) => {
  const normalized = mode === 'standard' ? 'thinking' : mode;
  return VALID_AGENT_MODES.has(normalized) ? normalized : 'quick';
};

export const getWorkspaceAgentMode = (storage, workspaceId) => {
  try {
    return normalizeAgentMode(storage?.getItem(`mw_ws_agentmode_${workspaceId}`));
  } catch {
    return 'quick';
  }
};
