export const MCP_SCOPES = Object.freeze(['mcp:search', 'mcp:chat']);
export const MCP_DEFAULT_FORM = Object.freeze({
  name: '',
  expiresIn: '86400',
  scopes: MCP_SCOPES,
  ontologyId: '',
});
const MCP_GRAPH_PROVIDERS = Object.freeze(['neo4j', 'hugegraph']);

function text(value) {
  return String(value ?? '').trim();
}

export function mcpScopeKey(workspaceId = '') {
  return JSON.stringify([text(workspaceId)]);
}

export function acceptsMcpResponse({ currentScope, requestScope, currentSequence, requestSequence }) {
  return currentScope === requestScope && currentSequence === requestSequence;
}

function normalizeService(service) {
  return {
    id: text(service?.id),
    toolset: text(service?.toolset),
    title: text(service?.title) || text(service?.id),
    summary: text(service?.summary),
    description: text(service?.description),
    transport: text(service?.transport),
    client_config_type: text(service?.client_config_type),
    active_url: text(service?.active_url || service?.url),
    tags: Array.isArray(service?.tags) ? service.tags.map(text).filter(Boolean) : [],
    tools: (Array.isArray(service?.tools) ? service.tools : []).map((tool) => ({
      name: text(tool?.name),
      description: text(tool?.description),
    })).filter((tool) => tool.name),
  };
}

export function normalizeMcpStatus(payload = {}, expectedWorkspaceId = '') {
  const workspaceId = text(payload.workspace_id);
  const expected = text(expectedWorkspaceId);
  if (expected && workspaceId !== expected) {
    return {
      valid: false,
      error: `MCP 状态响应作用域不匹配：期望 ${expected}，收到 ${workspaceId || '空'}`,
      workspaceId,
      services: [],
    };
  }
  const mcpEndpointId = text(payload.mcp_endpoint_id);
  if (!/^[0-9a-f]{16}$/.test(mcpEndpointId)) {
    return {
      valid: false,
      error: 'MCP 状态响应缺少有效的工作空间 Endpoint ID',
      workspaceId,
      services: [],
    };
  }
  return {
    valid: true,
    error: '',
    workspaceId,
    mcpEndpointId,
    running: Boolean(payload.running),
    transport: text(payload.transport),
    publicBaseUrl: text(payload.public_base_url),
    detail: text(payload.detail),
    services: (Array.isArray(payload.services) ? payload.services : [])
      .map(normalizeService)
      .filter((service) => service.id),
  };
}

export function normalizeMcpTokens(payload = {}, expectedWorkspaceId = '') {
  const expected = text(expectedWorkspaceId);
  return (Array.isArray(payload.items) ? payload.items : [])
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      token_id: text(item.token_id),
      token_masked: text(item.token_masked),
      name: text(item.name),
      workspace_id: text(item.workspace_id),
      ontology_id: text(item.ontology_id),
      binding_mode: text(item.binding_mode) || (text(item.ontology_id) ? 'ontology' : 'workspace'),
      username: text(item.username),
      scopes: Array.isArray(item.scopes) ? item.scopes.map(text).filter((scope) => MCP_SCOPES.includes(scope)) : [],
      status: text(item.status),
      created_at: text(item.created_at),
      expires_at: item.expires_at == null ? null : text(item.expires_at),
      revoked_at: item.revoked_at == null ? null : text(item.revoked_at),
      last_used_at: item.last_used_at == null ? null : text(item.last_used_at),
    }))
    .filter((item) => item.token_id && (!expected || item.workspace_id === expected));
}

export function normalizeMcpOntologies(payload = {}) {
  return (Array.isArray(payload.ontologies) ? payload.ontologies : [])
    .filter((item) => item && typeof item === 'object')
    .map((item) => {
      const versions = Array.isArray(item.versions) ? item.versions.map(text).filter(Boolean) : [];
      const businessData = item.business_data && typeof item.business_data === 'object' ? item.business_data : {};
      return {
        id: text(item.id),
        name: text(item.name) || text(item.id),
        enabled: Boolean(item.enabled),
        active_version: text(item.active_version),
        versions,
        business_data: {
          configured: Boolean(businessData.configured),
          ready: Boolean(businessData.ready),
          provider: text(businessData.provider).toLowerCase(),
        },
      };
    })
    .filter((item) => item.id);
}

export function mcpOntologyEligibility(ontology, scopes = MCP_SCOPES) {
  if (!ontology?.enabled) return { eligible: false, reason: '知识包已禁用' };
  if (!ontology.active_version || !ontology.versions.includes(ontology.active_version)) {
    return { eligible: false, reason: '没有可用的激活版本' };
  }
  if ((Array.isArray(scopes) ? scopes : []).includes('mcp:search')) {
    const provider = text(ontology.business_data?.provider).toLowerCase();
    if (!ontology.business_data?.ready || !MCP_GRAPH_PROVIDERS.includes(provider)) {
      return { eligible: false, reason: 'Search 需要可用的 Neo4j 或 HugeGraph 数据源' };
    }
  }
  return { eligible: true, reason: '' };
}

export function resolveMcpOntologySelection({ ontologies = [], scopes = MCP_SCOPES, currentId = '' } = {}) {
  const eligible = ontologies.filter((ontology) => mcpOntologyEligibility(ontology, scopes).eligible);
  const normalizedCurrentId = text(currentId);
  if (eligible.some((ontology) => ontology.id === normalizedCurrentId)) return normalizedCurrentId;
  return eligible.length === 1 ? eligible[0].id : '';
}

export function createdMcpToken(payload = {}) {
  return text(payload.token);
}

export function buildMcpTokenRequest({ name = '', expiresIn = '86400', scopes = MCP_SCOPES, ontologyId = '' } = {}) {
  const normalizedScopes = (Array.isArray(scopes) ? scopes : [])
    .map(text)
    .filter((scope, index, values) => MCP_SCOPES.includes(scope) && values.indexOf(scope) === index);
  if (!normalizedScopes.length) throw new Error('至少选择一个 MCP scope');
  const normalizedOntologyId = text(ontologyId);
  if (!normalizedOntologyId) throw new Error('请选择 Token 绑定的知识包');
  const expires = expiresIn === 'never' ? null : Number(expiresIn);
  if (expires !== null && (!Number.isInteger(expires) || expires <= 0)) throw new Error('Token 有效期无效');
  return {
    name: text(name), expires_in: expires, scopes: normalizedScopes, ontology_id: normalizedOntologyId,
  };
}

function serviceConfigType(service) {
  if (service?.client_config_type) return service.client_config_type;
  return service?.transport === 'sse' ? 'sse' : 'http';
}

export function buildMcpClientConfig(service, token = '<MCP_TOKEN>') {
  return JSON.stringify({
    mcpServers: {
      [service?.id || 'meta-wise-mcp']: {
        type: serviceConfigType(service),
        url: text(service?.active_url),
        headers: { Authorization: `Bearer ${token || '<MCP_TOKEN>'}` },
      },
    },
  }, null, 2);
}

export function createMcpScopeState(scopeKey) {
  return {
    scopeKey,
    createdToken: '',
    createdOntologyId: '',
    createdOntologyVersion: '',
    copiedKey: '',
    selectedServiceId: '',
    revokeTokenId: '',
    form: { ...MCP_DEFAULT_FORM, scopes: [...MCP_SCOPES] },
  };
}

export function canUseMcpSelfService({ authenticated = false, capability = true } = {}) {
  return Boolean(authenticated && capability);
}
