import { createHash, randomUUID } from 'node:crypto';
import { getPackageArtifactStore, PackageArtifactStoreError } from '../artifacts/index.js';
import { runProjectWriteExclusive } from '../artifacts/project-write-lock.js';
import { ArtifactRepositoryError } from '../artifacts/repository.js';
import { getDb, nowIso } from '../db/index.js';
import type { CoverageResult, DataDictTable, PackageSpec } from '../types.js';
import { assertObjectDataMappings } from './data-mapping.js';
import { checkCoverage } from './m2.js';
import { runFormatChecks, runRuleChecks, type CheckFinding } from './package-check.js';
import { applyPackageDraftFiles, PackageDraftParseError } from './package-draft-parser.js';
import { renderPackage } from './package-render.js';
import { nextValidationCandidateVersion } from './package-version-lifecycle.js';
import { packageIntentQuestions } from './spec-validation.js';

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
  baseRevisionOid: string;
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
  | 'PACKAGE_DRAFT_BASE_REVISION_MISSING'
  | 'PACKAGE_DRAFT_NOT_FOUND'
  | 'PACKAGE_DRAFT_FILE_NOT_FOUND'
  | 'PACKAGE_DRAFT_REVISION_CONFLICT'
  | 'PACKAGE_DRAFT_REQUIRES_PUBLISH'
  | 'PACKAGE_DRAFT_NO_CHANGES'
  | 'PACKAGE_DRAFT_BASE_VERSION_STALE'
  | 'PACKAGE_DRAFT_BASE_REVISION_STALE'
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
  base_revision_oid: string | null;
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

async function withBaseRevision(row: DraftRow): Promise<DraftRow> {
  if (row.base_revision_oid) return row;
  await getPackageArtifactStore().ensureProject(row.project_id);
  const version = getDb().prepare(`SELECT revision_oid FROM package_versions
    WHERE id = ? AND project_id = ?`).get(row.base_package_version_id, row.project_id) as {
      revision_oid: string | null;
    } | undefined;
  if (!version?.revision_oid) {
    throw new PackageDraftError(
      'PACKAGE_DRAFT_BASE_REVISION_MISSING',
      `草稿基线修订不存在，无法安全恢复 ${row.base_version} 的编辑内容`,
      409,
      { baseVersion: row.base_version },
    );
  }
  getDb().prepare(`UPDATE package_drafts SET base_revision_oid = ?
    WHERE id = ? AND base_revision_oid IS NULL`).run(version.revision_oid, row.id);
  return draftRowById(row.id)!;
}

async function baseSnapshot(row: DraftRow) {
  const hydrated = await withBaseRevision(row);
  try {
    return await getPackageArtifactStore().readPackageFiles(
      hydrated.project_id,
      hydrated.base_revision_oid!,
    );
  } catch (error) {
    if (error instanceof ArtifactRepositoryError && error.code === 'revision_not_found') {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_BASE_REVISION_MISSING',
        '人工草稿绑定的 Git 基线修订不存在，已拒绝继续编辑',
        409,
        { baseRevisionOid: hydrated.base_revision_oid },
      );
    }
    throw error;
  }
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

async function toDraftView(rawRow: DraftRow): Promise<PackageDraftView> {
  const row = await withBaseRevision(rawRow);
  const current = await getPackageArtifactStore().current(row.project_id);
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
    baseRevisionOid: row.base_revision_oid!,
    status: row.status,
    revision: row.revision,
    createdBy: row.created_by,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    changedFileCount: changedFiles.length,
    changedFiles,
    stale: current?.revision.oid !== row.base_revision_oid,
  };
}

function toDraftFileView(
  row: DraftRow,
  path: string,
  files: Record<string, string>,
): PackageDraftFileView {
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

export async function getActivePackageDraft(projectId: string): Promise<PackageDraftView | null> {
  const row = activeDraftRow(projectId);
  return row ? toDraftView(row) : null;
}

export function assertNoActivePackageDraft(projectId: string, operation: string): void {
  const row = activeDraftRow(projectId);
  if (!row) return;
  throw new PackageDraftError(
    'PACKAGE_DRAFT_REQUIRES_PUBLISH',
    '存在未发布的人工编辑草稿，请先发布或放弃草稿后再继续',
    409,
    {
      operation,
      baseVersion: row.base_version,
      baseRevisionOid: row.base_revision_oid,
      revision: row.revision,
      changedFileCount: draftFileRows(row.id).length,
      changedFiles: draftFileRows(row.id).map((file) => file.path),
    },
  );
}

export async function createOrGetPackageDraft(
  projectId: string,
  createdBy: string,
): Promise<{ draft: PackageDraftView; created: boolean }> {
  return runProjectWriteExclusive(projectId, async () => {
    const existing = activeDraftRow(projectId);
    if (existing) return { draft: await toDraftView(existing), created: false };

    const current = await getPackageArtifactStore().current(projectId);
    if (!current) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_BASE_VERSION_REQUIRED',
        '尚未生成知识包，无法开始人工编辑草稿',
        409,
      );
    }
    // This ID only satisfies the legacy foreign key. The draft content and stale check
    // are bound exclusively to current.revision.oid below.
    const versionAnchor = getDb().prepare(`SELECT id FROM package_versions
      WHERE project_id = ?
      ORDER BY CASE WHEN revision_oid = ? THEN 0 ELSE 1 END, rowid DESC
      LIMIT 1`).get(projectId, current.revision.oid) as { id: string } | undefined;
    if (!versionAnchor) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_BASE_VERSION_REQUIRED',
        '当前 Git 工件尚未建立正式版本索引，无法开始人工编辑草稿',
        409,
      );
    }
    const created = inImmediateTransaction(() => {
      const raced = activeDraftRow(projectId);
      if (raced) return { row: raced, created: false };
      const now = nowIso();
      const id = `pd_${randomUUID().slice(0, 12)}`;
      getDb().prepare(`INSERT INTO package_drafts
        (id, project_id, base_package_version_id, base_version, base_revision_oid,
         status, revision, created_by, created_at, updated_at)
        VALUES (?,?,?,?,?, 'active', 0, ?,?,?)`)
        .run(
          id,
          projectId,
          versionAnchor.id,
          current.packageVersion,
          current.revision.oid,
          createdBy,
          now,
          now,
        );
      return { row: draftRowById(id)!, created: true };
    });
    return { draft: await toDraftView(created.row), created: created.created };
  });
}

export async function getPackageDraftFile(
  projectId: string,
  path: string,
): Promise<PackageDraftFileView> {
  const row = await withBaseRevision(requireActiveDraft(projectId));
  const snapshot = await baseSnapshot(row);
  return toDraftFileView(row, path, snapshot.files);
}

export async function savePackageDraftFile(input: {
  projectId: string;
  path: string;
  content: string;
  expectedRevision: number;
  expectedContentHash?: string;
  expectedDraftId?: string;
}): Promise<{ draft: PackageDraftView; file: PackageDraftFileView }> {
  const initial = await withBaseRevision(requireActiveDraft(input.projectId));
  const snapshot = await baseSnapshot(initial);
  const row = inImmediateTransaction(() => {
    let row = requireActiveDraft(input.projectId);
    assertDraftIdentity(row, input.expectedDraftId);
    if (row.id !== initial.id || row.base_revision_oid !== initial.base_revision_oid) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_REVISION_CONFLICT',
        '人工草稿已变化，请刷新后重试',
        409,
      );
    }
    const files = snapshot.files;
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

    return row;
  });
  return {
    draft: await toDraftView(row),
    file: toDraftFileView(row, input.path, snapshot.files),
  };
}

export async function revertPackageDraftFile(input: {
  projectId: string;
  path: string;
  expectedRevision: number;
  expectedDraftId?: string;
}): Promise<{ draft: PackageDraftView; file: PackageDraftFileView }> {
  const initial = await withBaseRevision(requireActiveDraft(input.projectId));
  const snapshot = await baseSnapshot(initial);
  const row = inImmediateTransaction(() => {
    let row = requireActiveDraft(input.projectId);
    assertDraftIdentity(row, input.expectedDraftId);
    assertRevision(row, input.expectedRevision);
    if (row.id !== initial.id || row.base_revision_oid !== initial.base_revision_oid) {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_REVISION_CONFLICT',
        '人工草稿已变化，请刷新后重试',
        409,
      );
    }
    const files = snapshot.files;
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
    return row;
  });
  return {
    draft: await toDraftView(row),
    file: toDraftFileView(row, input.path, snapshot.files),
  };
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

export async function publishPackageDraft(input: {
  projectId: string;
  expectedRevision: number;
  expectedDraftId?: string;
  tables: DataDictTable[];
  summary?: string;
  actorUsername?: string;
}): Promise<PublishedPackageDraft> {
  const row = await withBaseRevision(requireActiveDraft(input.projectId));
  assertDraftIdentity(row, input.expectedDraftId);
  assertRevision(row, input.expectedRevision);
  const packages = getPackageArtifactStore();
  const [current, baseline] = await Promise.all([
    packages.current(input.projectId),
    baseSnapshot(row),
  ]);
  if (!current || current.revision.oid !== row.base_revision_oid) {
    throw new PackageDraftError(
      'PACKAGE_DRAFT_BASE_REVISION_STALE',
      '知识包 Git 修订已变化，请放弃当前草稿并基于最新修订重新编辑',
      409,
      {
        baseVersion: row.base_version,
        baseRevisionOid: row.base_revision_oid,
        currentRevisionOid: current?.revision.oid,
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
    parsedSpec = applyPackageDraftFiles(baseline.spec, changedRows.map((file) => ({
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

  let version: string;
  try {
    version = nextValidationCandidateVersion({
      currentVersion: baseline.spec.version,
      currentStatus: baseline.spec.status ?? 'draft',
    });
  } catch (error) {
    throw validationError(error instanceof Error ? error.message : String(error));
  }
  const spec: PackageSpec = { ...parsedSpec, version, status: 'review' };
  let files: Record<string, string>;
  try {
    files = renderPackage(spec);
  } catch (error) {
    throw validationError(
      `人工编辑草稿无法生成有效知识包：${error instanceof Error ? error.message : String(error)}`,
      { reason: error instanceof Error ? error.message : String(error) },
    );
  }
  const intentQuestions = packageIntentQuestions(spec);
  validatePublishedSpec(spec, files, input.tables, intentQuestions);
  const coverage = checkCoverage(spec, intentQuestions);
  const summary = input.summary?.trim();
  const note = summary
    ? `人工编辑：${summary.slice(0, 200)}`
    : `人工编辑：基于 ${row.base_version} 发布 ${changedFiles.length} 个文件`;

  try {
    const published = await packages.publishDraftPackageVersion(input.projectId, {
      expectedRevision: row.base_revision_oid!,
      draftId: row.id,
      expectedDraftRevision: row.revision,
      spec,
      coverage,
      note,
      action: {
        cause: 'manual_version',
        summary: note,
        actorKind: 'user',
        actorName: input.actorUsername?.trim() || row.created_by,
      },
    });
    return {
      draft: published.draft,
      packageVersionId: published.packageVersionId,
      version,
      changedFiles,
      objectCount: spec.objects.length,
      operationCount: spec.operations.length,
      ruleCount: spec.rules?.length ?? 0,
      metricCount: spec.metrics?.length ?? 0,
      coverage,
    };
  } catch (error) {
    if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
      throw new PackageDraftError(
        'PACKAGE_DRAFT_BASE_REVISION_STALE',
        '知识包 Git 修订已变化，请刷新后重新建立人工草稿',
        409,
        { baseRevisionOid: row.base_revision_oid },
      );
    }
    if (error instanceof PackageArtifactStoreError && error.code === 'invalid_input') {
      throw validationError(error.message);
    }
    throw error;
  }
}
