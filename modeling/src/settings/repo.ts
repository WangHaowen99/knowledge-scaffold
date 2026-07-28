import { getDb, nowIso } from '../db/index.js';
import { config } from '../config.js';

// 服务设置：页面可配置的键值（存 SQLite），env 只作默认值/兜底。
export function getSetting(key: string): string | null {
  const r = getDb().prepare('SELECT value FROM settings WHERE key = ?').get(key) as { value: string } | undefined;
  return r?.value ?? null;
}

export function setSetting(key: string, value: string): void {
  getDb().prepare(`INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`)
    .run(key, value, nowIso());
}

export function deleteSetting(key: string): void {
  getDb().prepare('DELETE FROM settings WHERE key = ?').run(key);
}

// 评测用 consumer 业务 API 地址：页面设置优先，其次 env CONSUMER_API_URL（默认 consumer 本机地址）。
// 注意：这是 consumer 的业务 API，与登录 issuer（auth-server）是两个不同服务，不再互为回退。
export function getConsumerApiUrl(): string {
  return (getSetting('consumer_api_url') || config.consumerApiUrl).replace(/\/+$/, '');
}

// LLM 配置：页面设置优先，env 兜底（faux 演示模式同样可在页面配 base url = faux）。
export interface LlmSettings { baseUrl: string; model: string; apiKey: string; maxTokens: number; }
export function getLlmSettings(): LlmSettings {
  return {
    baseUrl: (getSetting('llm_base_url') || config.llm.baseUrl || '').trim(),
    model: (getSetting('llm_model') || config.llm.model || '').trim(),
    apiKey: (getSetting('llm_api_key') || config.llm.apiKey || '').trim(),
    maxTokens: Number(getSetting('llm_max_tokens') || process.env.MODELING_LLM_MAX_TOKENS || 8192),
  };
}

// 评测数据源默认值（评测运行面板预填，减少每轮手输）。
export interface EvalBindingDefaults { provider: string; url: string; token: string; scene: string; }
export function getEvalBindingDefaults(): EvalBindingDefaults {
  return {
    provider: getSetting('eval_binding_provider') || 'nl2sql_template',
    url: getSetting('eval_binding_url') || '',
    token: getSetting('eval_binding_token') || '',
    scene: getSetting('eval_binding_scene') || '',
  };
}
