import { getDb, nowIso } from '../db/index.js';
import type { PackageDataSourceType } from '../types.js';
import { parsePackageDataSourceType } from './package-data-source.js';

export type SelectablePackageDataSourceType = Extract<PackageDataSourceType, 'nl2sql' | 'neo4j'>;

export interface ProjectPackageDataSourceSelection {
  projectId: string;
  dataSourceType: SelectablePackageDataSourceType;
  createdAt?: string;
  updatedAt?: string;
}

function selectable(value: unknown): SelectablePackageDataSourceType {
  const parsed = parsePackageDataSourceType(value);
  if (parsed !== 'nl2sql' && parsed !== 'neo4j') {
    throw new Error('知识包数据源类型仅支持 NL2SQL 或 Neo4j');
  }
  return parsed;
}

function rowToSelection(row: any): ProjectPackageDataSourceSelection {
  return {
    projectId: String(row.project_id),
    dataSourceType: selectable(row.data_source_type),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export function defaultPackageDataSourceType(): SelectablePackageDataSourceType {
  return 'neo4j';
}

export function normalizePackageDataSourceSelection(value: unknown): SelectablePackageDataSourceType {
  return selectable(value ?? defaultPackageDataSourceType());
}

export function getProjectPackageDataSourceSelection(projectId: string): ProjectPackageDataSourceSelection {
  const row = getDb().prepare('SELECT * FROM project_package_data_sources WHERE project_id = ?').get(projectId);
  return row ? rowToSelection(row) : { projectId, dataSourceType: defaultPackageDataSourceType() };
}

export function saveProjectPackageDataSourceSelection(
  projectId: string,
  value: unknown,
): ProjectPackageDataSourceSelection {
  const dataSourceType = normalizePackageDataSourceSelection(value);
  const now = nowIso();
  getDb().prepare(`INSERT INTO project_package_data_sources
      (project_id, data_source_type, created_at, updated_at)
    VALUES (?,?,?,?)
    ON CONFLICT(project_id) DO UPDATE SET
      data_source_type = excluded.data_source_type,
      updated_at = excluded.updated_at`)
    .run(projectId, dataSourceType, now, now);
  return getProjectPackageDataSourceSelection(projectId);
}
