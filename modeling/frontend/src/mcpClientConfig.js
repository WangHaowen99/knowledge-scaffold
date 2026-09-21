function streamableUrl(runtime, toolset) {
  return (runtime?.urls || []).find(
    (item) => item?.toolset === toolset && item?.transport === 'streamable-http',
  )?.url || '';
}

function serverConfig(url, token) {
  return {
    type: 'http',
    url,
    headers: { Authorization: `Bearer ${token || '<MCP_TOKEN>'}` },
  };
}

function stringifyConfig(mcpServers) {
  return JSON.stringify({ mcpServers }, null, 2);
}

export function buildModelingMcpClientConfigs(runtime, token = '<MCP_TOKEN>', tokenScopes) {
  const readUrl = streamableUrl(runtime, 'read');
  const changeUrl = streamableUrl(runtime, 'change');
  const filterByScopes = Array.isArray(tokenScopes);
  const canRead = !filterByScopes || tokenScopes.includes('ontology.read');
  const canChange = !filterByScopes || tokenScopes.some(
    (scope) => scope === 'ontology.change.validate' || scope === 'ontology.change.apply',
  );
  const readServer = readUrl && canRead ? serverConfig(readUrl, token) : null;
  const changeServer = changeUrl && canChange ? serverConfig(changeUrl, token) : null;
  const combined = readServer && changeServer
    ? {
        'modeling-read': readServer,
        'modeling-change': changeServer,
      }
    : null;

  return {
    read: readServer ? stringifyConfig({ 'modeling-read': readServer }) : '',
    change: changeServer ? stringifyConfig({ 'modeling-change': changeServer }) : '',
    combined: combined ? stringifyConfig(combined) : '',
  };
}
