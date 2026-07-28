import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { EvaluationDatabaseImportResult } from './eval-db.js';

export type EvaluationDatabaseImportRunStatus = 'running' | 'done' | 'failed';

export interface EvaluationDatabaseImportRun {
  id: string;
  projectId: string;
  filename: string;
  database: string;
  status: EvaluationDatabaseImportRunStatus;
  tableCount: number;
  rowCount: number;
  tables: { name: string; rowCount: number }[];
  error?: string;
  createdAt: string;
  finishedAt?: string;
}

function rowToRun(row: any): EvaluationDatabaseImportRun {
  return {
    id: row.id,
    projectId: row.project_id,
    filename: row.filename,
    database: row.database_name,
    status: row.status,
    tableCount: row.table_count,
    rowCount: row.row_count,
    tables: JSON.parse(row.tables_json),
    error: row.error || undefined,
    createdAt: row.created_at,
    finishedAt: row.finished_at || undefined,
  };
}

export function getEvaluationDatabaseImportRun(
  id: string,
): EvaluationDatabaseImportRun | null {
  const row = getDb().prepare(
    'SELECT * FROM evaluation_database_import_runs WHERE id = ?',
  ).get(id);
  return row ? rowToRun(row) : null;
}

export function getRunningEvaluationDatabaseImportRun(
  projectId: string,
): EvaluationDatabaseImportRun | null {
  const row = getDb().prepare(`SELECT * FROM evaluation_database_import_runs
    WHERE project_id = ? AND status = 'running' ORDER BY created_at DESC LIMIT 1`)
    .get(projectId);
  return row ? rowToRun(row) : null;
}

export function getRunningEvaluationDatabaseImportCount(): number {
  const row = getDb().prepare(`SELECT COUNT(*) AS count
    FROM evaluation_database_import_runs WHERE status = 'running'`).get() as { count: number };
  return Number(row.count ?? 0);
}

export function createEvaluationDatabaseImportRun(input: {
  projectId: string;
  filename: string;
  database: string;
}): EvaluationDatabaseImportRun {
  const id = `edir_${randomUUID().slice(0, 16)}`;
  getDb().prepare(`INSERT INTO evaluation_database_import_runs
      (id, project_id, filename, database_name, status, created_at)
    VALUES (?,?,?,?,'running',?)`)
    .run(id, input.projectId, input.filename, input.database, nowIso());
  return getEvaluationDatabaseImportRun(id)!;
}

export function finishEvaluationDatabaseImportRun(
  id: string,
  outcome: { result: EvaluationDatabaseImportResult } | { error: string },
): EvaluationDatabaseImportRun {
  const result = 'result' in outcome ? outcome.result : undefined;
  getDb().prepare(`UPDATE evaluation_database_import_runs SET
      status = ?, table_count = ?, row_count = ?, tables_json = ?, error = ?, finished_at = ?
    WHERE id = ?`)
    .run(
      result ? 'done' : 'failed',
      result?.tableCount ?? 0,
      result?.rowCount ?? 0,
      JSON.stringify(result?.tables ?? []),
      'error' in outcome ? outcome.error : null,
      nowIso(),
      id,
    );
  return getEvaluationDatabaseImportRun(id)!;
}

export function listEvaluationDatabaseImportRuns(
  projectId: string,
): EvaluationDatabaseImportRun[] {
  return (getDb().prepare(`SELECT * FROM evaluation_database_import_runs
    WHERE project_id = ? ORDER BY created_at DESC`).all(projectId) as any[]).map(rowToRun);
}

export function failRunningEvaluationDatabaseImportRuns(error: string): number {
  const result = getDb().prepare(`UPDATE evaluation_database_import_runs
    SET status = 'failed', error = ?, finished_at = ? WHERE status = 'running'`)
    .run(error, nowIso());
  return Number(result.changes ?? 0);
}
