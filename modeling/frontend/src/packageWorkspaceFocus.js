const FOCUS = {
  ai_construction: { tab: 'edit' },
  scenario: { tab: 'scenario' },
  data_source: { tab: 'data' },
  evaluate: { tab: 'evaluate' },
  diagnose: { tab: 'improve', improveView: 'diagnose' },
  optimize: { tab: 'improve', improveView: 'optimize' },
  release: { tab: 'release' },
};

export function packageWorkspaceFocus(stepId) {
  return FOCUS[stepId] ?? FOCUS.ai_construction;
}
