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

export class GenerationModelCallError extends Error {
  constructor(public readonly policy: GenerationErrorPolicy) {
    const reason = policy.message.trim().slice(0, 1200);
    const summary = policy.kind === 'quota_exhausted'
      ? '模型额度不足，已保留当前知识生成断点。请在配置中心恢复额度或切换可用模型后重试当前步骤。'
      : policy.kind === 'rate_limited'
        ? '模型服务持续限流，已保留当前知识生成断点。请稍后重试当前步骤。'
        : policy.kind === 'transient_provider'
          ? '模型服务暂时不可用，已保留当前知识生成断点。请在服务恢复后重试当前步骤。'
          : policy.kind === 'agent_output'
            ? '模型连续未能提交有效的结构化结果，已保留当前知识生成断点。请重试当前步骤。'
            : '模型调用未完成，已保留当前知识生成断点。请重试当前步骤。';
    super(`${summary}${reason ? ` 技术原因：${reason}` : ''}`);
    this.name = 'GenerationModelCallError';
  }
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
  if (/insufficient[_ -]?quota|quota.*exhaust|token-plan quota|余额不足|额度不足|额度耗尽/u.test(normalized)) {
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

export function generationModelCallError(error: unknown): GenerationModelCallError {
  return error instanceof GenerationModelCallError
    ? error
    : new GenerationModelCallError(classifyGenerationError(error));
}

export function shouldRetryGenerationError(error: unknown): boolean {
  return classifyGenerationError(error).retry;
}
