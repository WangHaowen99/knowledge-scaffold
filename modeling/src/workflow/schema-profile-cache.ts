import type { DatabaseSync } from 'node:sqlite';
import { getDb, nowIso } from '../db/index.js';
import type { DataDictTable } from '../types.js';

export const SCHEMA_PROFILE_ANALYSIS_VERSION = 'schema-profile-v1';

export interface SchemaProfile {
  name: string;
  documentId?: string;
  rowCount: number;
  fieldCount: number;
  fields: Array<Record<string, unknown>>;
}

type CacheKey = {
  projectId: string;
  documentId: string;
  contentHash: string;
  sheetName: string;
  purposeRevision: string;
  analysisVersion?: string;
};

function profileFor(table: DataDictTable): SchemaProfile {
  return {
    name: table.name,
    ...(table.documentId ? { documentId: table.documentId } : {}),
    rowCount: table.rowCount,
    fieldCount: table.fields.length,
    fields: table.fields.map((field) => ({
      name: field.name,
      inferredType: field.inferredType,
      ...(field.note ? { note: field.note } : {}),
      ...(field.sourceColumn ? { sourceColumn: field.sourceColumn } : {}),
      ...(typeof field.nonEmptyRatio === 'number' ? { nonEmptyRatio: field.nonEmptyRatio } : {}),
      ...(typeof field.distinctRatio === 'number' ? { distinctRatio: field.distinctRatio } : {}),
      ...(field.primaryKey ? { primaryKey: true } : {}),
      sampleValues: field.sampleValues.slice(0, 3),
    })),
  };
}

export class SchemaProfileCache {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  get(key: CacheKey): SchemaProfile | null {
    const row = this.db.prepare(`SELECT profile_json FROM schema_profile_cache
      WHERE project_id = ? AND document_id = ? AND content_hash = ? AND sheet_name = ?
        AND purpose_revision = ? AND analysis_version = ?`).get(
      key.projectId, key.documentId, key.contentHash, key.sheetName,
      key.purposeRevision, key.analysisVersion ?? SCHEMA_PROFILE_ANALYSIS_VERSION,
    ) as { profile_json?: string } | undefined;
    if (!row?.profile_json) return null;
    try {
      return JSON.parse(row.profile_json) as SchemaProfile;
    } catch {
      return null;
    }
  }

  save(key: CacheKey, table: DataDictTable): SchemaProfile {
    const analysisVersion = key.analysisVersion ?? SCHEMA_PROFILE_ANALYSIS_VERSION;
    const profile = profileFor(table);
    const now = nowIso();
    this.db.prepare(`INSERT INTO schema_profile_cache (
      project_id, document_id, content_hash, sheet_name, purpose_revision,
      analysis_version, profile_json, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(project_id, document_id, content_hash, sheet_name, purpose_revision, analysis_version)
    DO UPDATE SET profile_json = excluded.profile_json, updated_at = excluded.updated_at`).run(
      key.projectId, key.documentId, key.contentHash, key.sheetName, key.purposeRevision,
      analysisVersion, JSON.stringify(profile), now, now,
    );
    return profile;
  }

  invalidateDocument(projectId: string, documentId: string, keep?: Partial<Pick<CacheKey, 'contentHash' | 'purposeRevision'>>) {
    if (!keep?.contentHash && !keep?.purposeRevision) {
      this.db.prepare('DELETE FROM schema_profile_cache WHERE project_id = ? AND document_id = ?')
        .run(projectId, documentId);
      return;
    }
    this.db.prepare(`DELETE FROM schema_profile_cache
      WHERE project_id = ? AND document_id = ?
        AND (content_hash <> ? OR purpose_revision <> ?)`).run(
      projectId, documentId, keep.contentHash ?? '', keep.purposeRevision ?? '',
    );
  }
}

export { profileFor as buildSchemaProfile };
