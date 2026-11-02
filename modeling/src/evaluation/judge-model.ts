import { streamSimple as streamAnthropic } from '@earendil-works/pi-ai/api/anthropic-messages';
import { streamSimple as streamOpenAi } from '@earendil-works/pi-ai/api/openai-completions';
import type { LlmDeps } from '../llm/provider.js';

/** Per-round credentials supplied by Onto's backend, never persisted. */
export interface JudgeModelConfig {
  api: 'anthropic-messages' | 'openai-completions';
  base_url: string;
  api_key: string;
  model: string;
  max_tokens: number;
  context_window: number;
}

export class JudgeModelConfigError extends Error {
  readonly code = 'EVALUATION_JUDGE_CONFIG_INVALID';
  readonly statusCode = 400;

  constructor() {
    super('onto-modeling 判分模型配置不完整或无效，请检查「我的模型配置」');
    this.name = 'JudgeModelConfigError';
  }
}

export function parseJudgeModelConfig(value: unknown): JudgeModelConfig {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new JudgeModelConfigError();
  const raw = value as Record<string, unknown>;
  if (raw.api !== 'anthropic-messages' && raw.api !== 'openai-completions') throw new JudgeModelConfigError();
  if (['base_url', 'api_key', 'model'].some((key) => typeof raw[key] !== 'string' || !raw[key].trim())) {
    throw new JudgeModelConfigError();
  }
  const baseUrl = (raw.base_url as string).trim();
  try {
    const url = new URL(baseUrl);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error();
  } catch {
    throw new JudgeModelConfigError();
  }
  if (
    typeof raw.max_tokens !== 'number' || !Number.isSafeInteger(raw.max_tokens) || raw.max_tokens < 1
    || typeof raw.context_window !== 'number' || !Number.isSafeInteger(raw.context_window)
    || raw.max_tokens >= raw.context_window
  ) throw new JudgeModelConfigError();
  return {
    api: raw.api,
    base_url: baseUrl,
    api_key: (raw.api_key as string).trim(),
    model: (raw.model as string).trim(),
    max_tokens: raw.max_tokens,
    context_window: raw.context_window,
  };
}

export function buildJudgeLlm(settings: JudgeModelConfig): LlmDeps {
  const stream = settings.api === 'anthropic-messages' ? streamAnthropic : streamOpenAi;
  return {
    model: {
      id: settings.model,
      name: settings.model,
      api: settings.api,
      provider: 'onto-evaluation-judge',
      baseUrl: settings.base_url,
      reasoning: false,
      input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      maxTokens: settings.max_tokens,
      contextWindow: settings.context_window,
    },
    // Keep credentials in closures so serializing model metadata cannot expose them.
    streamFn: (model: any, context: any, options: Record<string, any> | undefined) => stream(model, context, {
      ...(options ?? {}),
      apiKey: settings.api_key,
      ...(settings.api === 'anthropic-messages' ? {
        headers: { ...(options?.headers ?? {}), Authorization: `Bearer ${settings.api_key}` },
      } : {}),
    }),
    getApiKey: () => settings.api_key,
  };
}

/** Model gateways can echo headers and URLs; only expose a fixed failure category. */
export function judgeModelFailure(error: unknown): Error {
  const message = error instanceof Error ? error.message : String(error);
  if (message === '评测判分服务未按协议提交结论') return new Error(message);
  if (/context_length_exceeded/iu.test(message)) {
    return new Error('onto-modeling 判分模型上下文超出上限，请检查配置中心的模型窗口和最大输出');
  }
  if (/执行超时|timeout|timed out/iu.test(message)) {
    return new Error('onto-modeling 判分模型调用超时，请稍后重试');
  }
  if (/\b429\b|\b503\b|rate[\s_-]?limit|too many requests|限流|请求过于频繁/iu.test(message)) {
    return new Error('onto-modeling 判分模型请求被限流（429/503），请稍后重试');
  }
  if (/\b401\b|\b403\b|authentication|unauthorized|invalid.*key/iu.test(message)) {
    return new Error('onto-modeling 判分模型认证失败，请检查「我的模型配置」的 API Key');
  }
  return new Error('onto-modeling 判分模型调用失败，请检查「我的模型配置」的网关、模型和 API Key');
}
