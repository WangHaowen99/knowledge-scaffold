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

// 评测用 consumer API 地址：设置优先，其次 env CONSUMER_API_URL，最后回退 auth issuer。
export function getConsumerApiUrl(): string {
  return (getSetting('consumer_api_url') || config.consumerApiUrl || config.auth.issuerUrl).replace(/\/+$/, '');
}
