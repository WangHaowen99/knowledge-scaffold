export const DEFAULT_ADVANCED_LLM = {
  thinkingMode: 'default',
  reasoningEffort: '',
  extraBodyJson: '{}',
};

const BLOCKED_EXTRA_BODY_KEYS = new Set([
  'api_key',
  'apikey',
  'api-key',
  'authorization',
  'cookie',
  'password',
  'secret',
  'token',
  'access_token',
  'accesstoken',
  'refresh_token',
  'refreshtoken',
  'secretkey',
  'model',
  'messages',
  'tools',
  'tool_choice',
  'stream',
  'temperature',
  'top_p',
  'max_tokens',
  'max_completion_tokens',
  'base_url',
  'baseurl',
  'api_base',
  'apikeyset',
  'api_key_set',
  'thinking',
  'reasoning_effort',
]);

function normalizeKey(key) {
  return String(key || '').trim().replace(/[\s-]+/g, '_').toLowerCase();
}

function assertNoBlockedKeys(value, path = '') {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoBlockedKeys(item, `${path}[${index}]`));
    return;
  }
  if (!value || typeof value !== 'object') return;
  for (const [key, nested] of Object.entries(value)) {
    if (BLOCKED_EXTRA_BODY_KEYS.has(normalizeKey(key))) {
      throw new Error(`extraBodyJson contains blocked key: ${path}${key}`);
    }
    assertNoBlockedKeys(nested, `${path}${key}.`);
  }
}

export function prettyJsonText(value) {
  try {
    return JSON.stringify(JSON.parse(String(value || '{}')), null, 2);
  } catch {
    return String(value || '');
  }
}

export function buildLlmPreviewBody({
  model = '',
  thinkingMode = 'default',
  reasoningEffort = '',
  extraBodyJson = '{}',
} = {}) {
  let extras = {};
  try {
    const parsed = JSON.parse(String(extraBodyJson || '{}'));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { error: 'extraBodyJson must be a JSON object' };
    }
    assertNoBlockedKeys(parsed);
    extras = parsed;
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
  const mode = String(thinkingMode || 'default').trim().toLowerCase();
  const body = {
    model: String(model || '').trim() || '<model>',
    messages: [{ role: 'user', content: 'ping' }],
    max_tokens: 1,
    ...extras,
  };
  if (mode === 'enabled' || mode === 'disabled') {
    body.thinking = { type: mode };
  }
  if (mode === 'enabled' && reasoningEffort) {
    body.reasoning_effort = reasoningEffort;
  }
  return { body };
}

export function previewJsonText(input) {
  const preview = buildLlmPreviewBody(input);
  if (preview.error) return `Preview error: ${preview.error}`;
  return JSON.stringify(preview.body, null, 2);
}
