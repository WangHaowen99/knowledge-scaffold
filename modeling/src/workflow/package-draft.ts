import { createHash, randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { CoverageResult, DataDictTable, PackageSpec } from '../types.js';
import { assertObjectDataMappings } from './data-mapping.js';
import { checkCoverage } from './m2.js';
import {
  getPackageVersionById,
  insertPackageVersion,
  latestPackageVersion,
} from './m2-repo.js';
import { runFormatChecks, runRuleChecks, type CheckFinding } from './package-check.js';
import { applyPackageDraftFiles, PackageDraftParseError } from './package-draft-parser.js';
import { renderPackage } from './package-render.js';

export type PackageDraftStatus = 'active' | 'published' | 'discarded';

export interface PackageDraftFileSummary {
  path: string;
  baseContentHash: string;
  contentHash: string;
  updatedAt: string;
}

export interface PackageDraftView {
  id: string;
  projectId: string;
  basePackageVersionId: string;
  baseVersion: string;
  status: PackageDraftStatus;
  revision: number;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  changedFileCount: number;
  changedFiles: PackageDraftFileSummary[];
  stale: boolean;
}

export interface PackageDraftFileView {
  path: string;
  content: string;
  baseContent: string;
  baseContentHash: string;
  contentHash: string;
  changed: boolean;
  updatedAt?: string;
  draftRevision: number;
}

export type PackageDraftErrorCode =
  | 'PACKAGE_DRAFT_BASE_VERSION_REQUIRED'
  | 'PACKAGE_DRAFT_BASE_VERSION_MISSING'
  | 'PACKAGE_DRAFT_NOT_FOUND'
  | 'PACKAGE_DRAFT_FILE_NOT_FOUND'
  | 'PACKAGE_DRAFT_REVISION_CONFLICT'
  | 'PACKAGE_DRAFT_REQUIRES_PUBLISH'
  | 'PACKAGE_DRAFT_NO_CHANGES'
  | 'PACKAGE_DRAFT_BASE_VERSION_STALE'
  | 'PACKAGE_DRAFT_FILE_NOT_PUBLISHABLE'
  | 'PACKAGE_DRAFT_PARSE_FAILED'
  | 'PACKAGE_DRAFT_VALIDATION_FAILED';

export class PackageDraftError extends Error {
  constructor(
    readonly code: PackageDraftErrorCode,
    message: string,
    readonly statusCode: number,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'PackageDraftError';
  }
}

interface DraftRow {
  id: string;
  project_id: string;
  base_package_version_id: string;
  base_version: string;
  status: PackageDraftStatus;
  revision: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

interface DraftFileRow {
  draft_id: string;
  path: string;
  content: string;
  base_content_hash: string;
  created_at: string;
  updated_at: string;
}

export interface PublishedPackageDraft {
  draft: {
    id: string;
    status: 'published';
    revision: number;
    updatedAt: string;
  };
  packageVersionId: string;
  version: string;
  changedFiles: string[];
  objectCount: number;
  operationCount: number;
  ruleCount: number;
  metricCount: number;
  coverage: CoverageResult;
}

const hashContent = (content: string) => createHash('sha256').update(content).digest('hex');

export function isPublishablePackageDraftPath(path: string): boolean {
  const match = path.match(/^(objects|operations|rules|metrics)\/([^/]+)\.md$/);
  return !!match && match[2] !== 'index';
}

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
  return (getDb().prepare(`SELECT * FROM package_drafts
    WHERE project_id = ? AND status = 'active' LIMIT 1`).get(projectId) as DraftRow | undefined) ?? null;
}

function draftRowById(id: string): DraftRow | null {
  return (getDb().prepare('SELECT * FROM package_drafts WHERE id = ?').get(id) as DraftRow | undefined) ?? null;
}

function draftFileRows(draftId: string): DraftFileRow[] {
  return getDb().prepare(`SELECT * FROM package_draft_files
    WHERE draft_id = ? ORDER BY path`).all(draftId) as unknown as DraftFileRow[];
}

function draftFileRow(draftId: string, path: string): DraftFileRow | null {
  return (getDb().prepare(`SELECT * FROM package_draft_files
    WHERE draft_id = ? AND path = ?`).get(draftId, path) as DraftFileRow | undefined) ?? null;
}

function baseFiles(row: DraftRow): Record<string, string> {
  const version = getPackageVersionById(row.project_id, row.base_package_version_id);
  if (!version) {
    throw new PackageDraftError(
      'PACKAGE_DRAFT_BASE_VERSION_MISSING',
      `草稿基线版本 ${row.base_version} 不存在`,
      409,
      { baseVersion: row.base_version },
    );
  }
  return renderPackage(version.spec);
}

function requireActiveDraft(projectId: string): DraftRow {
  const row = activeDraftRow(projectId);
  if (!row) {
    throw new PackageDraftError('PACKAGE_DRAFT_NOT_FOUND', '当前项目不存在活动人工草稿', 404);
  }
  return row;
}

function assertRevision(row: DraftRow, expectedRevision: number): void {
  if (row.revision !== expectedRevision) {
    throw new PackageDraftError(
      'PACKAGE_DRAFT_REVISION_CONFLICT',
      '草稿已在其他页面或标签页中更新，请刷新后重试',
      409,
      { expectedRevision, currentRevision: row.revision },
    );
  }
}

function assertDraftIdentity(row: DraftRow, expectedDraftId?: string): void {
  if (!expectedDraftId || row.id === expectedDraftId) return;
  throw new PackageDraftError(
    'PACKAGE_DRAFT_REVISION_CONFLICT',
    '原人工编辑草稿已被放弃或发布，当前项目已创建另一份草稿',
    409,
    {
      conflictScope: 'draft',
      expectedDraftId,
      currentDraftId: row.id,
      currentRevision: row.revision,
    },
  );
}

function validationError(message: string, details: Record<string, unknown> = {}): PackageDraftError {
  return new PackageDraftError(
    'PACKAGE_DRAFT_VALIDATION_FAILED',
    message,
    400,
    details,
  );
}

function validatePublishedSpec(
  spec: PackageSpec,
  files: Record<string, string>,
  tables: DataDictTable[],
  selectedQuestions: string[],
): void {
  try {
    assertObjectDataMappings(spec, tables);
  } catch (error) {
    throw validationError(
      error instanceof Error ? error.message : String(error),
      { reason: error instanceof Error ? error.message : String(error) },
    );
  }
  const findings: CheckFinding[] = [
    ...runFormatChecks(spec, files),
    ...runRuleChecks(spec, tables, selectedQuestions),
  ].filter((finding) => finding.severity === '错误');
  if (findings.length) {
    throw validationError(
      `人工编辑草稿未通过知识包校验：${findings.map((finding) => finding.message).join('；')}`,
      { findings },
    );
  }
}

function toDraftView(row: DraftRow): PackageDraftView {
  const latest = latestPackageVersion(row.project_id);
  const changedFiles = draftFileRows(row.id).map((file) => ({
    path: file.path,
    baseContentHash: file.base_content_hash,
    contentHash: hashContent(file.content),
    updatedAt: file.updated_at,
  }));
  return {
    id: row.id,
    projectId: row.project_id,
    basePackageVersionId: row.base_package_version_id,
    baseVersion: row.base_version,
    status: row.status,
    revision: row.revision,
    createdBy: row.created_by,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    changedFileCount: changedFiles.length,
    changedFiles,
    stale: latest?.id !== row.base_package_version_id,
  };
}

function toDraftFileView(row: DraftRow, path: string): PackageDraftFileView {
  const files = baseFiles(row);
  if (!(path in files)) {
    throw new PackageDraftError(
      'PACKAGE_DRAFT_FILE_NOT_FOUND',
      `文件 ${path} 不存在于草稿基线版本`,
      404,
      { path, baseVersion: row.base_version },
    );
  }
  const baseContent = files[path];
  const edited = draftFileRow(row.id, path);
  const content = edited?.content ?? baseContent;
  return {
    path,
    content,
    baseContent,
    baseContentHash: hashContent(baseContent),
    contentHash: hashContent(content),
    changed: !!edited,
    updatedAt: edited?.updated_at,
    draftRevision: row.revision,
  };
}

function bumpDraftRevision(row: DraftRow, now: string): DraftRow {
  const result = getDb().prepare(`UPDATE package_drafts
    SET revision = revision + 1, updated_at = ?
    WHERE id = ? AND status = 'active' AND revision = ?`).run(now, row.id, row.revision);
  if (Number(result.changes ?? 0) !== 1) {
    const current = activeDraftRow(row.project_id);
    throw new PackageDraftError(
      'PACKAGE_DRAFT_REVISION_CONFLICT',
      '草稿已在其他页面或标签页中更新，请刷新后重试',
      409,
      { expectedRevision: row.revision, currentRevision: current?.revision },
    );
  }
  return draftRowById(row.id)!;
}

export function getActivePackageDraft(projectId: string): PackageDraftView | null {
  const row = activeDraftRow(projectId);
  return row ? toDraftView(row) : null;
}

export function assertNoActivePackageDraft(projectId: string, operation: string): void {
  const row = activeDraftRow(projectId);
  if (!row) return;
  const draft = toDraftView(row);
  throw new PackageDraftError(
    'PACKAGE_DRAFT_REQUIRES_PUBLISH',
    '存在未发布的人工编辑草稿，请先发布或放弃草稿后再继续',
    409,
    {
      operation,
      baseVersion: draft.baseVersion,
      revision: draft.revision,
      changedFileCount: draft.changedFileCount,
      changedFiles: draft.changedFiles.map((file) => file.path),
      stale: draft.stale,
    },
  );
}

export function createOrGetPackageDraft(
  projectId: string,
  createdBy: string,
): { draft: PackageDraftView; created: boolean } {
  return inImmediateTransaction(() => {
    const existing = activeDraftRow(projectId);
    if (existing) return { draft: toDraftView(existing), created: false };

    const latest = latestPackageVersion(projectId);
    if (!latest) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_BASE_VERSION_REQUIRED',
        '尚未生成知识包，无法开始人工编辑草稿',
        409,
      );
    }
    const now = nowIso();
    const id = `pd_${randomUUID().slice(0, 12)}`;
    getDb().prepare(`INSERT INTO package_drafts
      (id, project_id, base_package_version_id, base_version, status, revision, created_by, created_at, updated_at)
      VALUES (?,?,?,?, 'active', 0, ?,?,?)`)
      .run(id, projectId, latest.id, latest.version, createdBy, now, now);
    return { draft: toDraftView(draftRowById(id)!), created: true };
  });
}

export function getPackageDraftFile(projectId: string, path: string): PackageDraftFileView {
  return toDraftFileView(requireActiveDraft(projectId), path);
}

export function savePackageDraftFile(input: {
  projectId: string;
  path: string;
  content: string;
  expectedRevision: number;
  expectedContentHash?: string;
  expectedDraftId?: string;
}): { draft: PackageDraftView; file: PackageDraftFileView } {
  return inImmediateTransaction(() => {
    let row = requireActiveDraft(input.projectId);
    assertDraftIdentity(row, input.expectedDraftId);
    const files = baseFiles(row);
    if (!(input.path in files)) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_FILE_NOT_FOUND',
        `文件 ${input.path} 不存在于草稿基线版本`,
        404,
        { path: input.path, baseVersion: row.base_version },
      );
    }

    const baseContent = files[input.path];
    const existing = draftFileRow(row.id, input.path);
    const currentContentHash = hashContent(existing?.content ?? baseContent);
    if (row.revision !== input.expectedRevision) {
      if (!input.expectedContentHash) assertRevision(row, input.expectedRevision);
      if (input.expectedContentHash !== currentContentHash) {
        throw new PackageDraftError(
          'PACKAGE_DRAFT_REVISION_CONFLICT',
          '当前文件已在其他页面或标签页中修改，请选择保留当前内容或加载最新版本',
          409,
          {
            expectedRevision: input.expectedRevision,
            currentRevision: row.revision,
            path: input.path,
            conflictScope: 'file',
            expectedContentHash: input.expectedContentHash,
            currentContentHash,
            currentUpdatedAt: existing?.updated_at,
          },
        );
      }
    }
    const shouldRemove = input.content === baseContent;
    const unchanged = shouldRemove ? !existing : existing?.content === input.content;
    if (!unchanged) {
      const now = nowIso();
      if (shouldRemove) {
        getDb().prepare('DELETE FROM package_draft_files WHERE draft_id = ? AND path = ?')
          .run(row.id, input.path);
      } else {
        getDb().prepare(`INSERT INTO package_draft_files
          (draft_id, path, content, base_content_hash, created_at, updated_at)
          VALUES (?,?,?,?,?,?)
          ON CONFLICT(draft_id, path) DO UPDATE SET
            content = excluded.content,
            base_content_hash = excluded.base_content_hash,
            updated_at = excluded.updated_at`)
          .run(row.id, input.path, input.content, hashContent(baseContent), now, now);
      }
      row = bumpDraftRevision(row, now);
    }

    return { draft: toDraftView(row), file: toDraftFileView(row, input.path) };
  });
}

export function revertPackageDraftFile(input: {
  projectId: string;
  path: string;
  expectedRevision: number;
  expectedDraftId?: string;
}): { draft: PackageDraftView; file: PackageDraftFileView } {
  return inImmediateTransaction(() => {
    let row = requireActiveDraft(input.projectId);
    assertDraftIdentity(row, input.expectedDraftId);
    assertRevision(row, input.expectedRevision);
    const files = baseFiles(row);
    if (!(input.path in files)) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_FILE_NOT_FOUND',
        `文件 ${input.path} 不存在于草稿基线版本`,
        404,
        { path: input.path, baseVersion: row.base_version },
      );
    }
    if (draftFileRow(row.id, input.path)) {
      const now = nowIso();
      getDb().prepare('DELETE FROM package_draft_files WHERE draft_id = ? AND path = ?')
        .run(row.id, input.path);
      row = bumpDraftRevision(row, now);
    }
    return { draft: toDraftView(row), file: toDraftFileView(row, input.path) };
  });
}

export function discardPackageDraft(input: {
  projectId: string;
  expectedRevision: number;
  expectedDraftId?: string;
}): { id: string; status: 'discarded'; revision: number; updatedAt: string } {
  return inImmediateTransaction(() => {
    const row = requireActiveDraft(input.projectId);
    assertDraftIdentity(row, input.expectedDraftId);
    assertRevision(row, input.expectedRevision);
    const now = nowIso();
    const result = getDb().prepare(`UPDATE package_drafts
      SET status = 'discarded', revision = revision + 1, updated_at = ?
      WHERE id = ? AND status = 'active' AND revision = ?`).run(now, row.id, row.revision);
    if (Number(result.changes ?? 0) !== 1) {
      const current = activeDraftRow(input.projectId);
      throw new PackageDraftError(
        'PACKAGE_DRAFT_REVISION_CONFLICT',
        '草稿已在其他页面或标签页中更新，请刷新后重试',
        409,
        { expectedRevision: row.revision, currentRevision: current?.revision },
      );
    }
    const discarded = draftRowById(row.id)!;
    return { id: discarded.id, status: 'discarded', revision: discarded.revision, updatedAt: discarded.updated_at };
  });
}

export function publishPackageDraft(input: {
  projectId: string;
  expectedRevision: number;
  expectedDraftId?: string;
  tables: DataDictTable[];
  selectedQuestions: string[];
  summary?: string;
}): PublishedPackageDraft {
  return inImmediateTransaction(() => {
    const row = requireActiveDraft(input.projectId);
    assertDraftIdentity(row, input.expectedDraftId);
    assertRevision(row, input.expectedRevision);
    const latest = latestPackageVersion(input.projectId);
    if (!latest || latest.id !== row.base_package_version_id) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_BASE_VERSION_STALE',
        '草稿基线已不是最新知识包版本，请放弃当前草稿并基于最新版重新编辑',
        409,
        {
          baseVersion: row.base_version,
          latestVersion: latest?.version,
          currentRevision: row.revision,
        },
      );
    }

    const changedRows = draftFileRows(row.id);
    if (!changedRows.length) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_NO_CHANGES',
        '草稿没有待发布的文件变更；如需退出编辑，请放弃草稿',
        400,
        { currentRevision: row.revision },
      );
    }
    const changedFiles = changedRows.map((file) => file.path);
    const unsupportedPath = changedFiles.find((path) => !isPublishablePackageDraftPath(path));
    if (unsupportedPath) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_FILE_NOT_PUBLISHABLE',
        `文件 ${unsupportedPath} 是派生文件，不能作为人工草稿发布`,
        400,
        { path: unsupportedPath },
      );
    }

    let parsedSpec: PackageSpec;
    try {
      parsedSpec = applyPackageDraftFiles(latest.spec, changedRows.map((file) => ({
        path: file.path,
        content: file.content,
      })));
    } catch (error) {
      if (!(error instanceof PackageDraftParseError)) throw error;
      throw new PackageDraftError(
        'PACKAGE_DRAFT_PARSE_FAILED',
        `人工编辑草稿无法解析：${error.message}`,
        400,
        { path: error.path, reason: error.message },
      );
    }

    const versionCount = (getDb().prepare(
      'SELECT COUNT(*) AS count FROM package_versions WHERE project_id = ?',
    ).get(input.projectId) as { count: number }).count;
    const version = `1.${versionCount}.0`;
    const spec = { ...parsedSpec, version };
    let files: Record<string, string>;
    try {
      files = renderPackage(spec);
    } catch (error) {
      throw validationError(
        `人工编辑草稿无法生成有效知识包：${error instanceof Error ? error.message : String(error)}`,
        { reason: error instanceof Error ? error.message : String(error) },
      );
    }
    validatePublishedSpec(spec, files, input.tables, input.selectedQuestions);
    const coverage = checkCoverage(spec, input.selectedQuestions);
    const summary = input.summary?.trim();
    const note = summary
      ? `人工编辑：${summary.slice(0, 200)}`
      : `人工编辑：基于 ${row.base_version} 发布 ${changedFiles.length} 个文件`;
    const packageVersionId = insertPackageVersion(input.projectId, version, spec, coverage, note);

    const now = nowIso();
    const updated = getDb().prepare(`UPDATE package_drafts
      SET status = 'published', revision = revision + 1, updated_at = ?
      WHERE id = ? AND status = 'active' AND revision = ?`).run(now, row.id, row.revision);
    if (Number(updated.changes ?? 0) !== 1) {
      const current = activeDraftRow(input.projectId);
      throw new PackageDraftError(
        'PACKAGE_DRAFT_REVISION_CONFLICT',
        '草稿已在其他页面或标签页中更新，请刷新后重试',
        409,
        { expectedRevision: row.revision, currentRevision: current?.revision },
      );
    }
    const published = draftRowById(row.id)!;
    return {
      draft: {
        id: published.id,
        status: 'published',
        revision: published.revision,
        updatedAt: published.updated_at,
      },
      packageVersionId,
      version,
      changedFiles,
      objectCount: spec.objects.length,
      operationCount: spec.operations.length,
      ruleCount: spec.rules?.length ?? 0,
      metricCount: spec.metrics?.length ?? 0,
      coverage,
    };
  });
}
