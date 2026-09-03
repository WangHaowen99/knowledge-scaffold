import {
  buildLlmRequestPreviewBody,
  normalizeLlmAdvancedSettings,
  type LlmAdvancedSettingsInput,
} from './llm-extra-params.js';

export interface LlmConnectionTestInput {
  baseUrl: string;
  model: string;
  apiKey?: string;
  thinkingMode?: unknown;
  reasoningEffort?: unknown;
  extraBodyJson?: unknown;
}

export interface LlmConnectionTestResult {
  ok: boolean;
  status?: number;
  latencyMs?: number;
  detail?: string;
  error?: string;
  requestPreview?: Record<string, unknown>;
}

function redactSecret(text: string, secret: string | undefined): string {
  if (!secret) return text;
  return text.split(secret).join('[REDACTED]');
}

export async function testLlmConnection(input: LlmConnectionTestInput): Promise<LlmConnectionTestResult> {
  const baseUrl = input.baseUrl.trim().replace(/\/+$/, '');
  const model = input.model.trim();
  let advanced;
  try {
    advanced = normalizeLlmAdvancedSettings(input as LlmAdvancedSettingsInput);
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
  const requestPreview = buildLlmRequestPreviewBody({ model, ...advanced });
  if (baseUrl === 'faux') {
    return { ok: true, detail: 'faux demo mode; no remote endpoint is called', requestPreview };
  }
  if (!baseUrl || !model) {
    return { ok: false, error: 'Base URL and model are required', requestPreview };
  }

  const startedAt = Date.now();
  try {
    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(input.apiKey ? { Authorization: `Bearer ${input.apiKey}` } : {}),
      },
      body: JSON.stringify(requestPreview),
      signal: AbortSignal.timeout(20_000),
    });
    const raw = redactSecret(await response.text(), input.apiKey).slice(0, 1000);
    if (!response.ok) {
      return { ok: false, status: response.status, latencyMs: Date.now() - startedAt, error: raw, requestPreview };
    }
    return { ok: true, status: response.status, latencyMs: Date.now() - startedAt, requestPreview };
  } catch (error) {
    const detail = error instanceof Error
      ? `${error.name}: ${error.message}${error.cause ? ` (${String(error.cause)})` : ''}`
      : String(error);
    return {
      ok: false,
      latencyMs: Date.now() - startedAt,
      error: redactSecret(detail, input.apiKey),
      requestPreview,
    };
  }
}
