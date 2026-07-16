import { getDb, nowIso } from '../db/index.js';
import {
  redactProviderConfig,
  type DataSourceMode,
  type EvaluationDataSourceProvider,
} from './data-source.js';

export interface ProjectDataSource {
  projectId: string;
  mode: DataSourceMode;
  provider?: EvaluationDataSourceProvider;
  workspaceId: string;
  ontologyId?: string;
  config: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface SaveProjectDataSourceInput {
  projectId: string;
  mode: DataSourceMode;
  provider?: EvaluationDataSourceProvider;
  workspaceId: string;
  ontologyId?: string;
  config?: Record<string, unknown>;
}

function rowToDataSource(row: any): ProjectDataSource {
  return {
    projectId: row.project_id,
    mode: row.mode,
    provider: row.provider || undefined,
    workspaceId: row.workspace_id,
    ontologyId: row.ontology_id || undefined,
    config: row.public_config_json ? JSON.parse(row.public_config_json) : {},
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export function getProjectDataSource(projectId: string): ProjectDataSource | null {
  const row = getDb().prepare('SELECT * FROM project_data_sources WHERE project_id = ?').get(projectId);
  return row ? rowToDataSource(row) : null;
}

export function saveProjectDataSource(input: SaveProjectDataSourceInput): ProjectDataSource {
  const now = nowIso();
  const provider = input.mode === 'connection' ? input.provider : undefined;
  const config = provider ? redactProviderConfig(provider, input.config ?? {}) : {};
  getDb().prepare(`INSERT INTO project_data_sources
      (project_id, mode, provider, workspace_id, ontology_id, public_config_json, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(project_id) DO UPDATE SET
      mode = excluded.mode,
      provider = excluded.provider,
      workspace_id = excluded.workspace_id,
      ontology_id = excluded.ontology_id,
      public_config_json = excluded.public_config_json,
      updated_at = excluded.updated_at`)
    .run(
      input.projectId,
      input.mode,
      provider ?? null,
      input.workspaceId,
      input.mode === 'connection' ? input.ontologyId ?? null : null,
      JSON.stringify(config),
      now,
      now,
    );
  return getProjectDataSource(input.projectId)!;
}
