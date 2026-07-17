export const DATA_SOURCE_MODE = Object.freeze({
  CONNECTION_REF: 'connection_ref',
  DIRECT_PROVIDER: 'direct_provider',
});

export function dataSourceScopeKey(workspaceId, ontologyId) {
  return `${String(workspaceId || '')}:${String(ontologyId || '')}`;
}

export function isCurrentDataSourceRequest(request, current) {
  return Boolean(
    request
    && current
    && request.scopeKey === current.scopeKey
    && request.sequence === current.sequence,
  );
}

export function providerDefaultConfig(schema) {
  const config = {};
  for (const field of schema?.fields || []) {
    if (field.secret) continue;
    if (field.default !== undefined && field.default !== null) config[field.name] = field.default;
  }
  return config;
}

export function publicProviderConfig(schema, sourceConfig = {}) {
  const config = {};
  for (const field of schema?.fields || []) {
    if (field.secret) {
      config[`has_${field.name}`] = Boolean(sourceConfig[`has_${field.name}`]);
      continue;
    }
    if (sourceConfig[field.name] !== undefined) config[field.name] = sourceConfig[field.name];
    else if (field.default !== undefined && field.default !== null) config[field.name] = field.default;
  }
  return config;
}

export function normalizeDataSourceDraft({ connectionsPayload = {}, providersPayload = {}, sourcePayload = {} } = {}) {
  const connections = Array.isArray(connectionsPayload.connections) ? connectionsPayload.connections : [];
  const providers = Array.isArray(providersPayload.providers) ? providersPayload.providers : [];
  const source = sourcePayload.source && typeof sourcePayload.source === 'object' ? sourcePayload.source : null;
  const mode = source?.mode === DATA_SOURCE_MODE.DIRECT_PROVIDER
    ? DATA_SOURCE_MODE.DIRECT_PROVIDER
    : DATA_SOURCE_MODE.CONNECTION_REF;
  const provider = String(source?.provider || providers[0]?.name || '');
  const schema = providers.find((item) => item.name === provider) || null;
  return {
    connections,
    providers,
    configured: Boolean(sourcePayload.configured),
    mode,
    connectionId: String(source?.connection_id || connectionsPayload.default_connection_id || ''),
    provider,
    config: mode === DATA_SOURCE_MODE.DIRECT_PROVIDER
      ? publicProviderConfig(schema, source?.config || {})
      : providerDefaultConfig(schema),
  };
}

export function validateDataSourceDraft({ mode, connectionId, provider, config = {}, schema }) {
  if (mode === DATA_SOURCE_MODE.CONNECTION_REF) {
    return connectionId ? '' : '请选择工作空间连接';
  }
  if (mode !== DATA_SOURCE_MODE.DIRECT_PROVIDER) return '不支持的数据源模式';
  if (!provider || !schema) return '请选择 Provider';
  for (const field of schema.fields || []) {
    if (!field.required) continue;
    const hasExistingSecret = field.secret && Boolean(config[`has_${field.name}`]);
    const value = config[field.name];
    if (!hasExistingSecret && (value === '' || value === null || value === undefined)) {
      return `请填写${field.label || field.name}`;
    }
  }
  return '';
}

export function buildDataSourcePayload({ mode, connectionId, provider, config = {}, schema }) {
  if (mode === DATA_SOURCE_MODE.CONNECTION_REF) {
    return { mode, connection_id: String(connectionId || '') };
  }
  const providerConfig = {};
  for (const field of schema?.fields || []) {
    const value = config[field.name];
    if (field.secret && (value === '' || value === null || value === undefined)) continue;
    if (value !== undefined && value !== null && value !== '') providerConfig[field.name] = value;
    if (field.type === 'bool' && value === false) providerConfig[field.name] = false;
  }
  return {
    mode: DATA_SOURCE_MODE.DIRECT_PROVIDER,
    provider: String(provider || ''),
    config: providerConfig,
  };
}
