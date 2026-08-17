export interface LlmConnectionTestInput {
  baseUrl: string;
  model: string;
  apiKey?: string;
}

export interface LlmConnectionTestResult {
  ok: boolean;
  status?: number;
  latencyMs?: number;
  detail?: string;
  error?: string;
}

function redactSecret(text: string, secret: string | undefined): string {
  if (!secret) return text;
  return text.split(secret).join('[REDACTED]');
}

/** 最小 OpenAI-compatible 请求；响应和异常在返回前移除本次使用的 Key。 */
export async function testLlmConnection(input: LlmConnectionTestInput): Promise<LlmConnectionTestResult> {
  const baseUrl = input.baseUrl.trim().replace(/\/+$/, '');
  const model = input.model.trim();
  if (baseUrl === 'faux') return { ok: true, detail: 'faux 演示模式（不调用真实端点）' };
  if (!baseUrl || !model) return { ok: false, error: 'Base URL 与模型名必填' };

  const startedAt = Date.now();
  try {
    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(input.apiKey ? { Authorization: `Bearer ${input.apiKey}` } : {}),
      },
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: 'ping' }],
        max_tokens: 1,
      }),
      signal: AbortSignal.timeout(20_000),
    });
    const raw = redactSecret(await response.text(), input.apiKey).slice(0, 1000);
    if (!response.ok) {
      return { ok: false, status: response.status, latencyMs: Date.now() - startedAt, error: raw };
    }
    return { ok: true, status: response.status, latencyMs: Date.now() - startedAt };
  } catch (error) {
    const detail = error instanceof Error
      ? `${error.name}: ${error.message}${error.cause ? `（${String(error.cause)}）` : ''}`
      : String(error);
    return {
      ok: false,
      latencyMs: Date.now() - startedAt,
      error: redactSecret(detail, input.apiKey),
    };
  }
}
