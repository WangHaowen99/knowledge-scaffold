import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { GraphImportMapping } from './types.js';

export type GraphImportRunStatus = 'running' | 'done' | 'failed' | 'cancelled';

export interface GraphImportRun {
  id: string;
  projectId: string;
  filename: string;
  fileHash: string;
  packageVersion: string;
  mapping: GraphImportMapping;
  status: GraphImportRunStatus;
  nodeCount: number;
  relationshipCount: number;
  skippedCount: number;
  error?: string;
  createdAt: string;
  finishedAt?: string;
}

function rowToRun(row: any): GraphImportRun {
  return {
    id: row.id,
    projectId: row.project_id,
    filename: row.filename,
    fileHash: row.file_hash,
    packageVersion: row.package_version,
    mapping: JSON.parse(row.mapping_json),
    status: row.status,
    nodeCount: row.node_count,
    relationshipCount: row.relationship_count,
    skippedCount: row.skipped_count,
    error: row.error || undefined,
    createdAt: row.created_at,
    finishedAt: row.finished_at || undefined,
  };
}

export function getGraphImportRun(id: string): GraphImportRun | null {
  const row = getDb().prepare('SELECT * FROM graph_import_runs WHERE id = ?').get(id);
  return row ? rowToRun(row) : null;
}

export function getRunningGraphImportRun(projectId: string): GraphImportRun | null {
  const row = getDb().prepare(`SELECT * FROM graph_import_runs
    WHERE project_id = ? AND status = 'running' ORDER BY created_at DESC LIMIT 1`).get(projectId);
  return row ? rowToRun(row) : null;
}

export function failRunningGraphImportRuns(error: string): number {
  const result = getDb().prepare(`UPDATE graph_import_runs
    SET status = 'failed', error = ?, finished_at = ? WHERE status = 'running'`)
    .run(error, nowIso());
  return Number(result.changes ?? 0);
}

export function deleteGraphImportRun(id: string): void {
  getDb().prepare('DELETE FROM graph_import_runs WHERE id = ?').run(id);
}

export function createGraphImportRun(input: {
  projectId: string;
  filename: string;
  fileHash: string;
  packageVersion: string;
  mapping: GraphImportMapping;
}): GraphImportRun {
  const id = `gir_${randomUUID().slice(0, 16)}`;
  getDb().prepare(`INSERT INTO graph_import_runs
      (id, project_id, filename, file_hash, package_version, mapping_json, status,
       node_count, relationship_count, skipped_count, created_at)
    VALUES (?,?,?,?,?,?,'running',0,0,0,?)`)
    .run(id, input.projectId, input.filename, input.fileHash, input.packageVersion,
      JSON.stringify(input.mapping), nowIso());
  return getGraphImportRun(id)!;
}

export function finishGraphImportRun(id: string, result: {
  status: Exclude<GraphImportRunStatus, 'running'>;
  nodeCount?: number;
  relationshipCount?: number;
  skippedCount?: number;
  error?: string;
}): GraphImportRun {
  getDb().prepare(`UPDATE graph_import_runs SET status = ?, node_count = ?,
      relationship_count = ?, skipped_count = ?, error = ?, finished_at = ? WHERE id = ?`)
    .run(result.status, result.nodeCount ?? 0, result.relationshipCount ?? 0,
      result.skippedCount ?? 0, result.error ?? null, nowIso(), id);
  return getGraphImportRun(id)!;
}

export function listGraphImportRuns(projectId: string): GraphImportRun[] {
  return (getDb().prepare(`SELECT * FROM graph_import_runs WHERE project_id = ?
    ORDER BY created_at DESC`).all(projectId) as any[]).map(rowToRun);
}
