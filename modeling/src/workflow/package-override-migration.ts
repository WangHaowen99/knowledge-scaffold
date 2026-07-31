import { createHash, randomUUID } from 'node:crypto';
import { getPackageArtifactStore } from '../artifacts/index.js';
import { getDb, nowIso } from '../db/index.js';
import { listDocuments } from '../documents/repo.js';
import {
  getActivePackageDraft,
  isPublishablePackageDraftPath,
  PackageDraftError,
  publishPackageDraft,
  type PackageDraftView,
} from './package-draft.js';
import { latestPackageVersion } from './m2-repo.js';

interface DraftRow {
  id: string;
  project_id: string;
  base_package_version_id: string;
  base_version: string;
  base_revision_oid: string | null;
  revision: number;
}

interface DraftFileRow {
  path: string;
  content: string;
}

interface LegacyOverrideRow {
  project_id: string;
  path: string;
  content: string;
  updated_at: string;
}

interface LegacyOverrideDraftMigration {
  projectId: string;
  draft: PackageDraftView | null;
  createdDraft: boolean;
  importedPaths: string[];
  unsupportedPaths: string[];
  obsoletePaths: string[];
  noOpPaths: string[];
  supersededPaths: string[];
  archivedCount: number;
}

export interface LegacyOverrideMigrationProjectResult {
  projectId: string;
  status: 'published' | 'pending-review' | 'archived' | 'failed';
  draftId?: string;
  version?: string;
  paths: string[];
  unsupportedPaths?: string[];
  error?: string;
}

export interface LegacyOverrideMigrationSummary {
  projectCount: number;
  publishedCount: number;
  pendingReviewCount: number;
  archivedCount: number;
  failedCount: number;
  projects: LegacyOverrideMigrationProjectResult[];
}

const hashContent = (content: string) => createHash('sha256').update(content).digest('hex');

function inImmediateTransaction<T>(fn: () => T): T {
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    const result = fn();
    db.exec('COMMIT');
    return result;
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

function activeDraftRow(projectId: string): DraftRow | null {
  return (getDb().prepare(`SELECT id, project_id, base_package_version_id, base_version,
      base_revision_oid, revision
    FROM package_drafts WHERE project_id = ? AND status = 'active' LIMIT 1`)
    .get(projectId) as DraftRow | undefined) ?? null;
}

function draftRowById(id: string): DraftRow | null {
  return (getDb().prepare(`SELECT id, project_id, base_package_version_id, base_version,
      base_revision_oid, revision
    FROM package_drafts WHERE id = ?`).get(id) as DraftRow | undefined) ?? null;
}

function draftFileRows(draftId: string): DraftFileRow[] {
  return getDb().prepare(`SELECT path, content FROM package_draft_files
    WHERE draft_id = ? ORDER BY path`).all(draftId) as unknown as DraftFileRow[];
}

function draftFileRow(draftId: string, path: string): DraftFileRow | null {
  return (getDb().prepare(`SELECT path, content FROM package_draft_files
    WHERE draft_id = ? AND path = ?`).get(draftId, path) as DraftFileRow | undefined) ?? null;
}

function bumpDraftRevision(row: DraftRow, now: string): DraftRow {
  const result = getDb().prepare(`UPDATE package_drafts
    SET revision = revision + 1, updated_at = ?
    WHERE id = ? AND status = 'active' AND revision = ?`).run(now, row.id, row.revision);
  if (Number(result.changes ?? 0) !== 1) {
    const current = activeDraftRow(row.project_id);
    throw new PackageDraftError(
      'PACKAGE_DRAFT_REVISION_CONFLICT',
      '草稿已在迁移过程中发生变化，请刷新后重试',
      409,
      { expectedRevision: row.revision, currentRevision: current?.revision },
    );
  }
  return draftRowById(row.id)!;
}

function archiveLegacyOverride(input: {
  row: LegacyOverrideRow;
  basePackageVersionId?: string;
  baseVersion?: string;
  draftId?: string;
  disposition: string;
  detail?: string;
  migratedAt: string;
}): void {
  getDb().prepare(`INSERT INTO package_override_migration_archive
    (project_id, path, content, original_updated_at, base_package_version_id, base_version,
      draft_id, disposition, detail, migrated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(project_id, path, original_updated_at) DO UPDATE SET
      base_package_version_id = excluded.base_package_version_id,
      base_version = excluded.base_version,
      draft_id = excluded.draft_id,
      disposition = excluded.disposition,
      detail = excluded.detail,
      migrated_at = excluded.migrated_at`)
    .run(
      input.row.project_id,
      input.row.path,
      input.row.content,
      input.row.updated_at,
      input.basePackageVersionId ?? null,
      input.baseVersion ?? null,
      input.draftId ?? null,
      input.disposition,
      input.detail ?? null,
      input.migratedAt,
    );
}

// 升级迁移专用：先完整归档旧 override，再把仍对最新版本有效的内容批量转入一个版本草稿。
// 同一批迁移只递增一次 revision；若已有活动草稿，则以草稿中的同路径内容为准。
async function migrateLegacyOverridesToPackageDraft(
  projectId: string,
  createdBy = 'legacy-override-migration',
): Promise<LegacyOverrideDraftMigration> {
  const packages = getPackageArtifactStore();
  const current = await packages.current(projectId);
  const currentFiles = current
    ? (await packages.readPackageFiles(projectId, current.revision.oid)).files
    : null;
  const migrated = inImmediateTransaction(() => {
    const db = getDb();
    const overrides = db.prepare(
      'SELECT project_id, path, content, updated_at FROM package_overrides WHERE project_id = ? ORDER BY path',
    ).all(projectId) as unknown as LegacyOverrideRow[];
    const empty: LegacyOverrideDraftMigration = {
      projectId,
      draft: null,
      createdDraft: false,
      importedPaths: [],
      unsupportedPaths: [],
      obsoletePaths: [],
      noOpPaths: [],
      supersededPaths: [],
      archivedCount: overrides.length,
    };
    if (!overrides.length) return empty;

    const migratedAt = nowIso();
    const latest = latestPackageVersion(projectId);
    if (!latest || !current || !currentFiles) {
      for (const row of overrides) {
        archiveLegacyOverride({
          row,
          disposition: 'orphaned-no-package-version',
          detail: '项目没有可绑定的 Git 知识包修订，旧覆盖已归档且不再参与运行时消费',
          migratedAt,
        });
      }
      db.prepare('DELETE FROM package_overrides WHERE project_id = ?').run(projectId);
      return { ...empty, obsoletePaths: overrides.map((row) => row.path) };
    }

    let draftRow = activeDraftRow(projectId);
    if (draftRow && draftRow.base_revision_oid !== current.revision.oid) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_BASE_REVISION_STALE',
        '活动草稿 Git 基线已过时，无法自动合并旧版人工覆盖；请先处理当前草稿后重试迁移',
        409,
        {
          baseVersion: draftRow.base_version,
          baseRevisionOid: draftRow.base_revision_oid,
          currentRevisionOid: current.revision.oid,
        },
      );
    }

    const files = currentFiles;
    const effective = overrides.filter((row) => row.path in files && row.content !== files[row.path]);
    const obsoletePaths = overrides.filter((row) => !(row.path in files)).map((row) => row.path);
    const noOpPaths = overrides.filter((row) => row.path in files && row.content === files[row.path]).map((row) => row.path);
    let createdDraft = false;
    if (!draftRow && effective.length) {
      const id = `pd_${randomUUID().slice(0, 12)}`;
      db.prepare(`INSERT INTO package_drafts
        (id, project_id, base_package_version_id, base_version, base_revision_oid,
         status, revision, created_by, created_at, updated_at)
        VALUES (?,?,?,?,?, 'active', 0, ?,?,?)`)
        .run(
          id,
          projectId,
          latest.id,
          current.packageVersion,
          current.revision.oid,
          createdBy,
          migratedAt,
          migratedAt,
        );
      draftRow = draftRowById(id)!;
      createdDraft = true;
    }

    const importedPaths: string[] = [];
    const supersededPaths: string[] = [];
    let inserted = false;
    for (const row of overrides) {
      let disposition = 'obsolete-path';
      let detail = '路径已不存在于迁移时的最新知识包，原始内容仅归档保留';
      if (row.path in files && row.content === files[row.path]) {
        disposition = 'no-op';
        detail = '旧覆盖内容与迁移时的正式版本一致，无需生成草稿变更';
      } else if (row.path in files && draftRow) {
        const existing = draftFileRow(draftRow.id, row.path);
        if (existing && existing.content !== row.content) {
          disposition = 'superseded-by-active-draft';
          detail = '同路径已有活动草稿修改，保留活动草稿内容并归档旧覆盖';
          supersededPaths.push(row.path);
        } else {
          disposition = 'drafted';
          detail = isPublishablePackageDraftPath(row.path)
            ? '已转入版本草稿，等待自动发布或人工审阅'
            : '派生文件无法固化到规格，已转入草稿等待人工撤销或放弃';
          importedPaths.push(row.path);
          if (!existing) {
            db.prepare(`INSERT INTO package_draft_files
              (draft_id, path, content, base_content_hash, created_at, updated_at)
              VALUES (?,?,?,?,?,?)`)
              .run(draftRow.id, row.path, row.content, hashContent(files[row.path]), migratedAt, migratedAt);
            inserted = true;
          }
        }
      }
      archiveLegacyOverride({
        row,
        basePackageVersionId: latest.id,
        baseVersion: latest.version,
        draftId: disposition === 'drafted' ? draftRow?.id : undefined,
        disposition,
        detail,
        migratedAt,
      });
    }

    if (draftRow && inserted) draftRow = bumpDraftRevision(draftRow, migratedAt);
    db.prepare('DELETE FROM package_overrides WHERE project_id = ?').run(projectId);
    const unsupportedPaths = draftRow
      ? draftFileRows(draftRow.id).map((row) => row.path).filter((path) => !isPublishablePackageDraftPath(path))
      : [];
    return {
      projectId,
      draft: null,
      createdDraft,
      importedPaths,
      unsupportedPaths,
      obsoletePaths,
      noOpPaths,
      supersededPaths,
      archivedCount: overrides.length,
    };
  });
  return {
    ...migrated,
    draft: activeDraftRow(projectId) ? await getActivePackageDraft(projectId) : null,
  };
}

function updateArchiveAfterPublish(draftId: string, packageVersionId: string, version: string): void {
  getDb().prepare(`UPDATE package_override_migration_archive
    SET disposition = 'published', target_package_version_id = ?, target_version = ?,
      detail = '旧版人工覆盖已自动固化为正式知识包版本'
    WHERE draft_id = ? AND disposition = 'drafted'`)
    .run(packageVersionId, version, draftId);
}

function updateArchiveReviewReason(draftId: string, reason: string): void {
  getDb().prepare(`UPDATE package_override_migration_archive
    SET detail = ? WHERE draft_id = ? AND disposition = 'drafted'`)
    .run(`已转入版本草稿，需人工审阅：${reason.slice(0, 500)}`, draftId);
}

export async function migrateLegacyPackageOverrides(): Promise<LegacyOverrideMigrationSummary> {
  const projectIds = (getDb().prepare(
    'SELECT DISTINCT project_id FROM package_overrides ORDER BY project_id',
  ).all() as unknown as Array<{ project_id: string }>).map((row) => row.project_id);
  const projects: LegacyOverrideMigrationProjectResult[] = [];

  for (const projectId of projectIds) {
    try {
      const migrated = await migrateLegacyOverridesToPackageDraft(projectId);
      if (!migrated.draft) {
        projects.push({
          projectId,
          status: 'archived',
          paths: [...migrated.obsoletePaths, ...migrated.noOpPaths],
        });
        continue;
      }
      if (!migrated.createdDraft) {
        const reason = migrated.supersededPaths.length
          ? `已有活动草稿；${migrated.supersededPaths.length} 个同路径旧覆盖已归档`
          : '旧覆盖已合并到已有活动草稿';
        updateArchiveReviewReason(migrated.draft.id, reason);
        projects.push({
          projectId,
          status: 'pending-review',
          draftId: migrated.draft.id,
          paths: migrated.importedPaths,
          unsupportedPaths: migrated.unsupportedPaths,
          error: reason,
        });
        continue;
      }
      if (migrated.unsupportedPaths.length) {
        const reason = `包含不能发布的派生文件：${migrated.unsupportedPaths.join('、')}`;
        updateArchiveReviewReason(migrated.draft.id, reason);
        projects.push({
          projectId,
          status: 'pending-review',
          draftId: migrated.draft.id,
          paths: migrated.importedPaths,
          unsupportedPaths: migrated.unsupportedPaths,
          error: reason,
        });
        continue;
      }

      const tables = listDocuments(projectId).flatMap((document) => document.tables ?? []);
      try {
        const published = await publishPackageDraft({
          projectId,
          expectedRevision: migrated.draft.revision,
          tables,
          summary: '迁移旧版人工编辑内容',
        });
        updateArchiveAfterPublish(migrated.draft.id, published.packageVersionId, published.version);
        projects.push({
          projectId,
          status: 'published',
          draftId: migrated.draft.id,
          version: published.version,
          paths: published.changedFiles,
        });
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        updateArchiveReviewReason(migrated.draft.id, reason);
        projects.push({
          projectId,
          status: 'pending-review',
          draftId: migrated.draft.id,
          paths: migrated.importedPaths,
          error: reason,
        });
      }
    } catch (error) {
      projects.push({
        projectId,
        status: 'failed',
        paths: [],
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  return {
    projectCount: projects.length,
    publishedCount: projects.filter((project) => project.status === 'published').length,
    pendingReviewCount: projects.filter((project) => project.status === 'pending-review').length,
    archivedCount: projects.filter((project) => project.status === 'archived').length,
    failedCount: projects.filter((project) => project.status === 'failed').length,
    projects,
  };
}
