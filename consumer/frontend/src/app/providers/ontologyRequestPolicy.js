export function ontologyRequestPolicy({
  surface,
  configScope = '',
  configSection = '',
} = {}) {
  const knowledgeSubscriber = surface === 'chat' || surface === 'kb';
  const mcpSubscriber =
    surface === 'config' &&
    configScope === 'workspace' &&
    configSection === 'mcp';
  return {
    listEnabled: knowledgeSubscriber || mcpSubscriber,
    treeEnabled: surface === 'kb',
  };
}
