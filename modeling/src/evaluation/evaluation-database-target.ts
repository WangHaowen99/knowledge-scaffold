import { config } from '../config.js';
import { normalizeNl2SqlScene } from './data-source.js';

export interface EvaluationDatabaseTarget {
  connectionUrl: string;
  database: string;
}

export interface EvaluationDatabaseUrlSummary {
  configured: boolean;
  databaseUrlPreview: string;
}

const SENSITIVE_URL_PARAMETER = /(?:password|pass|token|secret|key)/i;

function parseEvaluationDatabaseUrl(databaseUrl: unknown): URL {
  const rawUrl = String(databaseUrl ?? '').trim();
  if (!rawUrl) throw new Error('缺少 EVAL_DB_URL 或自定义数据库连接 URL');

  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error('数据库连接 URL 格式非法');
  }
  if (parsed.protocol !== 'mysql:') throw new Error('数据库连接 URL 仅支持 mysql 协议');
  if (!parsed.hostname) throw new Error('数据库连接 URL 缺少数据库主机');
  return parsed;
}

export function evaluationDatabaseUrlSummary(
  databaseUrl = config.evalDbUrl,
): EvaluationDatabaseUrlSummary {
  if (!String(databaseUrl ?? '').trim()) {
    return { configured: false, databaseUrlPreview: '' };
  }
  const parsed = parseEvaluationDatabaseUrl(databaseUrl);
  if (parsed.password) parsed.password = '******';
  for (const key of parsed.searchParams.keys()) {
    if (SENSITIVE_URL_PARAMETER.test(key)) {
      parsed.searchParams.set(key, '******');
    }
  }
  return { configured: true, databaseUrlPreview: parsed.toString() };
}

export function selectEvaluationDatabaseUrlOverride(
  mode: unknown,
  customUrl: unknown,
): string | undefined {
  const selectedMode = String(mode ?? '').trim();
  if (selectedMode === 'default') return undefined;
  if (selectedMode !== 'custom') {
    throw new Error('请确认使用服务端 EVAL_DB_URL，或选择自定义 MySQL URL');
  }
  const selectedUrl = String(customUrl ?? '').trim();
  if (!selectedUrl) throw new Error('自定义数据库 URL 不能为空');
  if (selectedUrl.length > 2_048) throw new Error('自定义数据库 URL 不能超过 2048 个字符');
  return selectedUrl;
}

export function resolveEvaluationDatabaseTarget(
  scene: unknown,
  databaseUrl = config.evalDbUrl,
): EvaluationDatabaseTarget {
  const database = normalizeNl2SqlScene(scene);
  const parsed = parseEvaluationDatabaseUrl(databaseUrl);

  // Scene 决定目标数据库，忽略 URI 中可能存在的默认数据库路径。
  parsed.pathname = '/';
  return { connectionUrl: parsed.toString(), database };
}

export function redactEvaluationDatabaseError(
  error: unknown,
  target: EvaluationDatabaseTarget,
): string {
  let message = error instanceof Error ? error.message : String(error);
  const secrets = new Set<string>([target.connectionUrl]);
  try {
    const parsed = new URL(target.connectionUrl);
    if (parsed.password) {
      secrets.add(parsed.password);
      secrets.add(decodeURIComponent(parsed.password));
    }
    for (const [key, value] of parsed.searchParams) {
      if (!value || !SENSITIVE_URL_PARAMETER.test(key)) continue;
      secrets.add(value);
      secrets.add(encodeURIComponent(value));
    }
  } catch {
    // target 在构造时已经过 URL 校验；这里只保证错误处理本身不会抛错。
  }
  for (const secret of secrets) {
    if (secret) message = message.split(secret).join('[REDACTED]');
  }
  return message;
}
