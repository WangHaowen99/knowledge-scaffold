import { createHash } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';

export interface BuildProjectAuditOptions {
  exportedAt?: string;
  packageRevision?: string;
  packageFiles?: Record<string, string>;
}

type AuditFiles = Record<string, string>;

const SENSITIVE_KEY = /(?:password|passphrase|secret|credential|token|api[_-]?key|apikey)/iu;
const SENSITIVE_COLUMN = /^(?:stored_path|password|passphrase|secret|credential|token|access_token|refresh_token|api_key)$/iu;

function quoteIdentifier(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

function redactValue(value: unknown, key = ''): unknown {
  if (SENSITIVE_KEY.test(key)) return '[REDACTED]';
  if (Array.isArray(value)) return value.map((item) => redactValue(item));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([childKey, child]) => (
      [childKey, redactValue(child, childKey)]
    )));
  }
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!(trimmed.startsWith('{') || trimmed.startsWith('['))) return value;
  try {
    return JSON.stringify(redactValue(JSON.parse(value)));
  } catch {
    return value;
  }
}

function redactRow(row: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(row).map(([key, value]) => (
    [key, SENSITIVE_COLUMN.test(key) ? '[REDACTED]' : redactValue(value, key)]
  )));
}

function stableRows(rows: Record<string, unknown>[]): Record<string, unknown>[] {
  return rows.map(redactRow).sort((left, right) => (
    JSON.stringify(left).localeCompare(JSON.stringify(right), 'zh-CN')
  ));
}

function json(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function sha256(content: string): string {
  return createHash('sha256').update(content).digest('hex');
}

function projectTables(db: DatabaseSync): string[] {
  const tables = db.prepare(`SELECT name FROM sqlite_schema
    WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name`).all() as { name: string }[];
  return tables.map((item) => item.name).filter((name) => {
    const columns = db.prepare(`PRAGMA table_info(${quoteIdentifier(name)})`).all() as { name: string }[];
    return columns.some((column) => column.name === 'project_id');
  });
}

function projectRows(db: DatabaseSync, table: string, projectId: string): Record<string, unknown>[] {
  return stableRows(db.prepare(
    `SELECT * FROM ${quoteIdentifier(table)} WHERE project_id = ?`,
  ).all(projectId) as Record<string, unknown>[]);
}

function evaluationChildRows(
  db: DatabaseSync,
  table: 'eval_results' | 'eval_result_traces',
  projectId: string,
): Record<string, unknown>[] {
  if (table === 'eval_results') {
    return stableRows(db.prepare(`SELECT result.* FROM eval_results result
      JOIN eval_rounds round ON round.id = result.round_id
      WHERE round.project_id = ?`).all(projectId) as Record<string, unknown>[]);
  }
  return stableRows(db.prepare(`SELECT trace.* FROM eval_result_traces trace
    JOIN eval_results result ON result.id = trace.result_id
    JOIN eval_rounds round ON round.id = result.round_id
    WHERE round.project_id = ?`).all(projectId) as Record<string, unknown>[]);
}

/**
 * 导出项目级建模审计记录。只收集项目隔离数据，全局设置、认证数据和运行凭据不进入归档。
 */
export function buildProjectAuditFiles(
  db: DatabaseSync = getDb(),
  projectId: string,
  options: BuildProjectAuditOptions = {},
): AuditFiles {
  const project = db.prepare('SELECT * FROM projects WHERE id = ?').get(projectId) as
    Record<string, unknown> | undefined;
  if (!project) throw new Error('项目不存在');

  const files: AuditFiles = {};
  const recordCounts: Record<string, number> = {};
  files['audit/records/project.json'] = json(redactRow(project));

  for (const table of projectTables(db)) {
    if (table === 'projects') continue;
    const rows = projectRows(db, table, projectId);
    if (!rows.length) continue;
    files[`audit/records/${table}.json`] = json(rows);
    recordCounts[table] = rows.length;
  }
  for (const table of ['eval_results', 'eval_result_traces'] as const) {
    const rows = evaluationChildRows(db, table, projectId);
    if (!rows.length) continue;
    files[`audit/records/${table}.json`] = json(rows);
    recordCounts[table] = rows.length;
  }

  for (const [path, content] of Object.entries(options.packageFiles ?? {})) {
    files[`knowledge-package/${path}`] = content;
  }

  const sortedCounts = Object.fromEntries(Object.entries(recordCounts).sort(([left], [right]) => (
    left.localeCompare(right, 'zh-CN')
  )));
  files['audit/manifest.json'] = json({
    schemaVersion: 1,
    exportedAt: options.exportedAt ?? nowIso(),
    project: {
      id: project.id,
      name: project.name,
      owner: project.owner,
      kind: project.kind,
    },
    ...(options.packageRevision ? { packageRevision: options.packageRevision } : {}),
    recordCounts: sortedCounts,
    notes: [
      '该归档用于建模过程审计，不是 Consumer 运行包。',
      '连接凭据、认证信息和宿主机文件路径已脱敏或排除。',
    ],
  });

  const integrity = Object.fromEntries(Object.entries(files)
    .sort(([left], [right]) => left.localeCompare(right, 'zh-CN'))
    .map(([path, content]) => [path, sha256(content)]));
  files['audit/integrity.json'] = json({ algorithm: 'sha256', files: integrity });
  return files;
}
