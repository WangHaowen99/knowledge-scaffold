export const MODEL_CONTEXT_LENGTH_EXCEEDED_CODE = 'context_length_exceeded';

const CONTEXT_LENGTH_PATTERNS = [
  /context[_ -]?length[_ -]?exceeded/i,
  /context[_ -]?window[_ -]?exceeded/i,
  /(?:token|prompt|request)[_ -]?length[_ -]?exceeded/i,
  /(?:request|prompt)[_ -]?too[_ -]?large/i,
  /(?:maximum|max|limit(?:ed)?|exceed(?:ed|s)?)\s+(?:model\s+)?context(?:\s+window|\s+length)?/i,
  /context(?:\s+window|\s+length).{0,80}(?:too long|exceed|limit|maximum)/i,
  /(?:prompt|input|request).{0,80}(?:too long|too many tokens|token limit)/i,
  /too many tokens/i,
];

export class ModelContextLengthExceededError extends Error {
  readonly code = MODEL_CONTEXT_LENGTH_EXCEEDED_CODE;

  constructor(readonly contextWindow?: number) {
    super([
      `模型 API 返回 ${MODEL_CONTEXT_LENGTH_EXCEEDED_CODE}：输入与输出总上下文超过模型支持的最大窗口。`,
      contextWindow && Number.isSafeInteger(contextWindow)
        ? `当前应用配置的 contextWindow 为 ${contextWindow} tokens。`
        : '',
      '请将 contextWindow 设置为 API 实际支持值，或减少本次资料范围/单次输出上限后重试。',
    ].filter(Boolean).join(' '));
    this.name = 'ModelContextLengthExceededError';
  }
}

export { ModelContextLengthExceededError as ModelContextWindowExceededError };

function stringifyErrorDetail(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined || value === null) return '';
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function searchableErrorText(value: unknown): string {
  if (value instanceof Error) {
    const details = value as Error & { code?: unknown; error?: unknown; body?: unknown; cause?: unknown };
    return [
      value.name,
      value.message,
      details.code,
      details.error,
      details.body,
      details.cause,
    ].map(stringifyErrorDetail).filter(Boolean).join(' ');
  }
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

export function isModelContextLengthExceeded(value: unknown): boolean {
  const text = searchableErrorText(value);
  return CONTEXT_LENGTH_PATTERNS.some((pattern) => pattern.test(text));
}

export function normalizeModelError(value: unknown, contextWindow?: number): unknown {
  if (value instanceof ModelContextLengthExceededError) return value;
  return isModelContextLengthExceeded(value)
    ? new ModelContextLengthExceededError(contextWindow)
    : value;
}
