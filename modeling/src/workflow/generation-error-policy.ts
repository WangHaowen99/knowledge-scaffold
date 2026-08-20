export type GenerationErrorClass =
  | 'quota_exhausted'
  | 'rate_limited'
  | 'transient_provider'
  | 'agent_output'
  | 'unknown';

export interface GenerationErrorPolicy {
  kind: GenerationErrorClass;
  retry: boolean;
  retryAfterMs?: number;
  message: string;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 只处理知识结构生成阶段的模型调用错误分类。
 * 资料分析有自己的重试/全文读取策略，不复用本策略。
 */
export function classifyGenerationError(error: unknown): GenerationErrorPolicy {
  const message = errorText(error);
  const normalized = message.toLocaleLowerCase('en-US');
  if (/insufficient[_ -]?quota|quota.*exhaust|token-plan quota|余额不足|额度耗尽/u.test(normalized)) {
    return { kind: 'quota_exhausted', retry: false, message };
  }
  const retryAfterMatch = message.match(/retry[- ]after\s*[:=]?\s*(\d+)/iu);
  if (/\b429\b|rate.?limit|too many requests|限流/u.test(normalized)) {
    const seconds = retryAfterMatch ? Number(retryAfterMatch[1]) : undefined;
    return {
      kind: 'rate_limited',
      retry: true,
      ...(seconds && Number.isFinite(seconds) ? { retryAfterMs: seconds * 1000 } : {}),
      message,
    };
  }
  if (/\b5\d{2}\b|timeout|timed out|fetch failed|econnreset|连接失败|网关/u.test(normalized)) {
    return { kind: 'transient_provider', retry: true, message };
  }
  if (/schema|invalid|validation|tool|structured|json|参数/u.test(normalized)) {
    return { kind: 'agent_output', retry: true, message };
  }
  return { kind: 'unknown', retry: true, message };
}

export function shouldRetryGenerationError(error: unknown): boolean {
  return classifyGenerationError(error).retry;
}
