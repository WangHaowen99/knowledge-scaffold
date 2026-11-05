function serverConfig(runtime, toolset, headers) {
  const endpoints = (runtime?.urls || []).filter((item) => (item.transport === 'streamable-http' || item.transport === 'sse') && item.toolset === toolset && item.url);
  const endpoint = endpoints.find((item) => item.transport === 'streamable-http')
    || endpoints.find((item) => item.transport === 'sse');
  if (!endpoint) return null;
  return {
    type: endpoint.transport === 'sse' ? 'sse' : 'http',
    url: endpoint.url,
    headers,
  };
}

export function buildClientConfigs(runtime, token, scopes) {
  const selected = Array.isArray(scopes) ? scopes : [
    'ontology.read', 'ontology.change.validate', 'ontology.change.apply',
  ];
  const auth = { Authorization: `Bearer ${token || '<MCP_TOKEN>'}` };
  const read = selected.includes('ontology.read')
    ? serverConfig(runtime, 'read', auth)
    : null;
  const change = selected.some((scope) => scope.startsWith('ontology.change.') || scope === 'package.publish')
    ? serverConfig(runtime, 'change', auth)
    : null;
  const json = (servers) => Object.keys(servers).length
    ? JSON.stringify({ mcpServers: servers }, null, 2)
    : '';
  return {
    read: read ? json({ 'modeling-read': read }) : '',
    change: change ? json({ 'modeling-change': change }) : '',
    combined: json({
      ...(read ? { 'modeling-read': read } : {}),
      ...(change ? { 'modeling-change': change } : {}),
    }),
  };
}
