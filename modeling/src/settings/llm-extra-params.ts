export const LLM_THINKING_MODES = ['default', 'enabled', 'disabled'] as const;
export type LlmThinkingMode = typeof LLM_THINKING_MODES[number];

export const LLM_REASONING_EFFORTS = ['', 'low', 'medium', 'high', 'max'] as const;
export type LlmReasoningEffort = typeof LLM_REASONING_EFFORTS[number];

export interface LlmAdvancedSettings {
  thinkingMode: LlmThinkingMode;
  reasoningEffort: LlmReasoningEffort;
  extraBodyJson: string;
}

export interface LlmAdvancedSettingsInput {
  thinkingMode?: unknown;
  reasoningEffort?: unknown;
  extraBodyJson?: unknown;
}

export class LlmExtraParamsValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'LlmExtraParamsValidationError';
  }
}

const BLOCKED_KEYS = new Set([
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function normalizeKey(key: string): string {
  return key.trim().replace(/[\s-]+/g, '_').toLowerCase();
}

function assertNoBlockedKeys(value: unknown, path = ''): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoBlockedKeys(item, `${path}[${index}]`));
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, nested] of Object.entries(value)) {
    const normalized = normalizeKey(key);
    if (BLOCKED_KEYS.has(normalized)) {
      throw new LlmExtraParamsValidationError(`extraBodyJson contains blocked key: ${path}${key}`);
    }
    assertNoBlockedKeys(nested, `${path}${key}.`);
  }
}

export function normalizeLlmThinkingMode(value: unknown): LlmThinkingMode {
  const mode = String(value ?? 'default').trim().toLowerCase();
  if (mode === '') return 'default';
  if ((LLM_THINKING_MODES as readonly string[]).includes(mode)) return mode as LlmThinkingMode;
  throw new LlmExtraParamsValidationError('thinkingMode must be default, enabled, or disabled');
}

export function normalizeLlmReasoningEffort(value: unknown): LlmReasoningEffort {
  const effort = String(value ?? '').trim().toLowerCase();
  if ((LLM_REASONING_EFFORTS as readonly string[]).includes(effort)) return effort as LlmReasoningEffort;
  throw new LlmExtraParamsValidationError('reasoningEffort must be empty, low, medium, high, or max');
}

export function normalizeLlmExtraBodyJson(value: unknown): string {
  const raw = String(value ?? '').trim();
  if (!raw) return '{}';
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new LlmExtraParamsValidationError('extraBodyJson must be valid JSON');
  }
  if (!isRecord(parsed)) {
    throw new LlmExtraParamsValidationError('extraBodyJson must be a JSON object');
  }
  assertNoBlockedKeys(parsed);
  return JSON.stringify(parsed);
}

export function normalizeLlmAdvancedSettings(input: LlmAdvancedSettingsInput): LlmAdvancedSettings {
  const thinkingMode = normalizeLlmThinkingMode(input.thinkingMode);
  const reasoningEffort = thinkingMode === 'enabled'
    ? normalizeLlmReasoningEffort(input.reasoningEffort)
    : '';
  return {
    thinkingMode,
    reasoningEffort,
    extraBodyJson: normalizeLlmExtraBodyJson(input.extraBodyJson),
  };
}

export function emptyLlmAdvancedSettings(): LlmAdvancedSettings {
  return {
    thinkingMode: 'default',
    reasoningEffort: '',
    extraBodyJson: '{}',
  };
}

export function parseLlmExtraBodyJson(extraBodyJson: string | null | undefined): Record<string, unknown> {
  const normalized = normalizeLlmExtraBodyJson(extraBodyJson ?? '{}');
  return JSON.parse(normalized) as Record<string, unknown>;
}

export function buildLlmRequestExtras(settings: Partial<LlmAdvancedSettings>): Record<string, unknown> {
  const normalized = normalizeLlmAdvancedSettings({
    thinkingMode: settings.thinkingMode ?? 'default',
    reasoningEffort: settings.reasoningEffort ?? '',
    extraBodyJson: settings.extraBodyJson ?? '{}',
  });
  const extras = parseLlmExtraBodyJson(normalized.extraBodyJson);
  if (normalized.thinkingMode !== 'default') {
    extras.thinking = { type: normalized.thinkingMode };
  }
  if (normalized.thinkingMode === 'enabled' && normalized.reasoningEffort) {
    extras.reasoning_effort = normalized.reasoningEffort;
  }
  return extras;
}

export function buildLlmRequestPreviewBody(input: {
  model: string;
  thinkingMode?: LlmThinkingMode;
  reasoningEffort?: LlmReasoningEffort;
  extraBodyJson?: string;
}): Record<string, unknown> {
  return {
    model: input.model.trim() || '<model>',
    messages: [{ role: 'user', content: 'ping' }],
    max_tokens: 1,
    ...buildLlmRequestExtras({
      thinkingMode: input.thinkingMode ?? 'default',
      reasoningEffort: input.reasoningEffort ?? '',
      extraBodyJson: input.extraBodyJson ?? '{}',
    }),
  };
}
