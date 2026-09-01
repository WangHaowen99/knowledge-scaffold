import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb } from '../db/index.js';
import type { CoverageResult, PackageSpec } from '../types.js';
import { updateManifestPlatformMetadata } from '../workflow/package-contract.js';
import { parseRenderedPackageFiles } from '../workflow/package-import.js';
import { renderPackage } from '../workflow/package-render.js';
import { canonicalizePackageSpec } from '../workflow/package-spec.js';
import { nextValidationCandidateVersion } from '../workflow/package-version-lifecycle.js';
import { validateGeneratedScriptSafety } from '../workflow/script-safety.js';
import { sourceLabelsFor } from '../workflow/source-references.js';
import {
  ArtifactRepositoryError,
  type ArtifactChange,
  type ArtifactDiff,
  type ArtifactFile,
  type ArtifactFileDiff,
  type ArtifactRepository,
  type ArtifactRevision,
  type ArtifactSnapshot,
} from './repository.js';
import {
  ArtifactRevisionRepo,
  type ArtifactRevisionActorKind,
  type ArtifactRevisionRecord,
} from './revision-repo.js';
import { runProjectWriteExclusive } from './project-write-lock.js';

const SPEC_PATH = 'model/package-spec.json';
const COVERAGE_PATH = 'model/coverage.json';
const PACKAGE_PREFIX = 'package/';
const CAUSE_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/;
const GROUP_ID_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/;
const ACTOR_KINDS = new Set<ArtifactRevisionActorKind>(['user', 'agent', 'system']);
const INTERRUPTED_COMMIT_GRACE_MS = 30_000;
const CANDIDATE_STATUSES = new Set<PackageCandidateRevisionStatus>([
  'pending', 'accepted', 'revision_requested', 'rejected', 'superseded',
]);
const MAX_CANDIDATE_GROUPS = 50;
const MAX_CANDIDATE_CHANGES = 500;
const MAX_CANDIDATE_BYTES = 10 * 1024 * 1024;

export interface PackageRevisionAction {
  cause: string;
  summary: string;
  actorKind: ArtifactRevisionActorKind;
  actorName: string;
  actorEmail?: string;
  createdAt?: Date;
}

export interface SavePackageVersionInput {
  expectedRevision: string;
  spec: PackageSpec;
  coverage: CoverageResult;
  note?: string;
  action: PackageRevisionAction;
  preserveUnchangedManagedFiles?: boolean;
  beforeCommit?: () => Promise<void>;
}

export interface PackageVersionChangePlan {
  revision: string;
  changedPaths: string[];
}

export interface InitializeDraftPackageInput {
  id: string;
  name: string;
  description: string;
  language?: string;
  defaultProviderAlias?: string;
}

export interface ImportPackageVersionInput extends SavePackageVersionInput {
  files: Readonly<Record<string, string | Uint8Array>>;
}

export interface SaveCurrentAsPackageVersionInput {
  expectedRevision: string;
  version: string;
  status?: PackageSpec['status'];
  note?: string;
  action: PackageRevisionAction;
}

export interface PublishDraftPackageVersionInput {
  expectedRevision: string;
  draftId: string;
  expectedDraftRevision: number;
  spec: PackageSpec;
  coverage: CoverageResult;
  note?: string;
  action: PackageRevisionAction;
}

export interface PublishedDraftPackageVersion {
  package: PackageArtifactView;
  packageVersionId: string;
  changedPaths: string[];
  draft: {
    id: string;
    status: 'published';
    revision: number;
    updatedAt: string;
  };
}

export interface FinalizeCurrentPackageVersionInput {
  expectedRevision: string;
  status: NonNullable<PackageSpec['status']>;
  note?: string;
  action: PackageRevisionAction;
}

export interface SavePackageFileInput {
  expectedRevision: string;
  path: string;
  content: string;
  action: PackageRevisionAction;
}

export interface SavePackageFilesInput {
  expectedRevision: string;
  files: Readonly<Record<string, string>>;
  deletePaths?: readonly string[];
  action: PackageRevisionAction;
}

export interface ResetPackageFileInput {
  expectedRevision: string;
  path: string;
  action: PackageRevisionAction;
}

export interface RestorePackageInput {
  expectedRevision: string;
  targetRevision: string;
  action: PackageRevisionAction;
}

export type PackageCandidateRevisionStatus =
  | 'pending'
  | 'accepted'
  | 'revision_requested'
  | 'rejected'
  | 'superseded';

export interface PackageCandidateValidationInput {
  errors?: readonly string[];
  warnings?: readonly string[];
}

export interface PackageCandidateValidation {
  errors: string[];
  warnings: string[];
}

export interface PackageCandidateChangeGroupInput {
  id: string;
  summary: string;
  dependsOn?: readonly string[];
  changes: readonly ArtifactChange[];
  validation?: PackageCandidateValidationInput;
}

export interface PackageCandidateChangeGroup {
  id: string;
  summary: string;
  dependsOn: string[];
  changes: ArtifactChange[];
  validation: PackageCandidateValidation;
}

export interface CreatePackageCandidateRevisionInput {
  expectedRevision: string;
  groups: readonly PackageCandidateChangeGroupInput[];
  action: PackageRevisionAction;
}

export interface DecidePackageCandidateRevisionInput {
  candidateId: string;
  action: PackageRevisionAction;
  note?: string;
}

export interface AcceptPackageCandidateRevisionInput extends DecidePackageCandidateRevisionInput {
  selectedGroupIds?: readonly string[];
  packageDataSourceType?: PackageSpec['dataSourceType'];
}

export interface ReturnPackageCandidateRevisionInput extends DecidePackageCandidateRevisionInput {
  selectedGroupIds?: readonly string[];
}

export interface PackageCandidateDecision {
  cause: string;
  summary: string;
  actorKind: ArtifactRevisionActorKind;
  actorName: string;
  note?: string;
  decidedAt: string;
}

export interface PackageCandidateRevision {
  id: string;
  projectId: string;
  baseRevision: string;
  status: PackageCandidateRevisionStatus;
  cause: string;
  summary: string;
  actorKind: ArtifactRevisionActorKind;
  actorName: string;
  groups: PackageCandidateChangeGroup[];
  selectedGroupIds: string[];
  acceptedRevision?: string;
  decision?: PackageCandidateDecision;
  createdAt: string;
}

export interface PackageCandidateSelection {
  requestedGroupIds: string[];
  selectedGroupIds: string[];
  autoIncludedGroupIds: string[];
}

export interface PackageCandidatePreview {
  candidate: PackageCandidateRevision;
  selection: PackageCandidateSelection;
  spec: PackageSpec;
  coverage: CoverageResult;
  files: Record<string, string>;
  binaryFiles: string[];
  diff: ArtifactFileDiff[];
}

export interface PackageCandidateRevisionOverview {
  candidates: PackageCandidateRevision[];
  pendingCount: number;
}

export interface AcceptedPackageCandidateRevision {
  candidate: PackageCandidateRevision;
  selection: PackageCandidateSelection;
  revision: ArtifactRevision;
  packageVersion: string;
}

export interface PackageArtifactView {
  revision: ArtifactRevision;
  packageVersion: string;
  spec: PackageSpec;
  coverage: CoverageResult;
  files: string[];
}

export interface PackageArtifactFile {
  path: string;
  content: string;
  revision: ArtifactRevision;
}

export interface PackageArtifactFiles {
  revision: ArtifactRevision;
  spec: PackageSpec;
  coverage: CoverageResult;
  files: Record<string, string>;
  binaryFiles: string[];
}

export interface PackageArtifactArchiveFiles {
  revision: ArtifactRevision;
  spec: PackageSpec;
  coverage: CoverageResult;
  files: Record<string, Uint8Array>;
}

export interface PackageArtifactStoreOptions {
  repository: ArtifactRepository;
  db?: DatabaseSync;
  revisions?: ArtifactRevisionRepo;
}

export type PackageArtifactStoreErrorCode =
  | 'invalid_input'
  | 'project_not_found'
  | 'package_not_found'
  | 'file_not_found'
  | 'revision_has_no_package'
  | 'invalid_package'
  | 'commit_in_progress'
  | 'candidate_not_found'
  | 'candidate_not_pending'
  | 'candidate_validation_failed';

export class PackageArtifactStoreError extends Error {
  constructor(
    public readonly code: PackageArtifactStoreErrorCode,
    message: string,
  ) {
    super(message);
    this.name = 'PackageArtifactStoreError';
  }
}

/**
 * 知识包的唯一读写入口。Git 文件布局、旧数据迁移和修订索引都封装在这里。
 */
export class PackageArtifactStore {
  private readonly db: DatabaseSync;
  private readonly repository: ArtifactRepository;
  private readonly revisions: ArtifactRevisionRepo;
  constructor(options: PackageArtifactStoreOptions) {
    this.db = options.db ?? getDb();
    this.repository = options.repository;
    this.revisions = options.revisions ?? new ArtifactRevisionRepo(this.db);
  }

  async ensureProject(projectId: string): Promise<ArtifactRevision> {
    return this.runExclusive(projectId, () => this.ensureProjectUnlocked(projectId));
  }

  /** 首次进入统一建模工作台时创建最小知识包草稿；重复调用保持幂等。 */
  async initializeDraftPackage(
    projectId: string,
    input: InitializeDraftPackageInput,
  ): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const current = await this.repository.snapshot(projectId);
      const existing = this.optionalViewFromSnapshot(current);
      if (existing) return existing;

      const spec: PackageSpec = {
        id: requiredText(input.id, '知识包 ID', 200),
        name: requiredText(input.name, '知识包名称', 200),
        description: requiredText(input.description, '知识包说明', 2_000),
        version: '0.1.0',
        language: optionalText(input.language, '知识包语言', 50) ?? 'zh-CN',
        status: 'draft',
        objects: [],
        operations: [],
        rules: [],
        metrics: [],
        scripts: [],
        defaultProviderAlias: optionalText(
          input.defaultProviderAlias,
          '默认数据源别名',
          100,
        ) ?? 'data',
      };
      const coverage: CoverageResult = { covered: [], uncovered: [] };
      validatePackageSpec(spec);
      const target = packageFiles(spec, coverage, true);
      const changes = changesForManagedFiles(current.files, target);
      const action: NormalizedAction = {
        cause: 'draft_initialized',
        summary: '初始化知识包草稿',
        actorKind: 'system',
        actorName: '建模系统',
      };
      const id = `pv_${randomUUID().slice(0, 12)}`;
      const revision = await this.commitAndIndex(
        projectId,
        current.revision.oid,
        () => this.repository.commit(projectId, current.revision.oid, changes, gitMetadata(action)),
        (savedRevision) => {
          this.db.prepare(`INSERT INTO package_versions
            (id, project_id, version, spec_json, coverage_json, note, revision_oid, created_at)
            VALUES (?,?,?,?,?,?,?,?)`)
            .run(
              id,
              projectId,
              spec.version,
              JSON.stringify(spec),
              JSON.stringify(coverage),
              '统一建模工作台初始化草稿',
              savedRevision.oid,
              savedRevision.createdAt,
            );
          this.recordRevision(
            projectId,
            savedRevision,
            action,
            changes.map((change) => change.path),
            spec.version,
          );
        },
      );
      return this.viewFromSnapshot(
        await this.repository.snapshot(projectId, revision.oid),
        spec.version,
      );
    });
  }

  async removeProjectArtifacts(projectId: string): Promise<void> {
    return this.runExclusive(projectId, async () => {
      await this.repository.removeProject?.(projectId);
    });
  }

  async savePackageVersion(projectId: string, input: SavePackageVersionInput): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      validatePackageSpec(input.spec);
      validateCoverage(input.coverage);
      const packageVersion = requiredText(input.spec.version, '包版本', 100);
      const note = optionalText(input.note, '版本备注', 1000);
      const current = await this.repository.snapshot(projectId);
      const target = packageFiles(input.spec, input.coverage);
      if (input.preserveUnchangedManagedFiles) {
        preserveUnchangedManagedFiles(current, target, this.viewFromSnapshot(current));
      } else {
        preserveImportedExtras(current.files, target);
      }
      const changes = changesForManagedFiles(current.files, target);
      await input.beforeCommit?.();
      const id = `pv_${randomUUID().slice(0, 12)}`;
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.commit(
        projectId,
        input.expectedRevision,
        changes,
        gitMetadata(action),
      ), (savedRevision) => {
        this.db.prepare(`INSERT INTO package_versions
          (id, project_id, version, spec_json, coverage_json, note, revision_oid, created_at)
          VALUES (?,?,?,?,?,?,?,?)`)
          .run(
            id,
            projectId,
            packageVersion,
            JSON.stringify(input.spec),
            JSON.stringify(input.coverage),
            note,
            savedRevision.oid,
            savedRevision.createdAt,
          );
        this.recordRevision(
          projectId,
          savedRevision,
          action,
          changes.map((change) => change.path),
          packageVersion,
        );
      });
      return this.viewFromSnapshot(await this.repository.snapshot(projectId, revision.oid), packageVersion);
    });
  }

  async planPackageVersionChanges(
    projectId: string,
    input: Pick<SavePackageVersionInput, 'expectedRevision' | 'spec' | 'coverage' | 'preserveUnchangedManagedFiles'>,
  ): Promise<PackageVersionChangePlan> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      validatePackageSpec(input.spec);
      validateCoverage(input.coverage);
      const current = await this.repository.snapshot(projectId);
      assertExpectedRevision(input.expectedRevision, current.revision.oid);
      const target = packageFiles(input.spec, input.coverage);
      if (input.preserveUnchangedManagedFiles) {
        preserveUnchangedManagedFiles(current, target, this.viewFromSnapshot(current));
      } else {
        preserveImportedExtras(current.files, target);
      }
      const changes = changesForManagedFiles(current.files, target);
      return {
        revision: current.revision.oid,
        changedPaths: changes.map((change) => displayManagedPath(change.path)).sort(compareNames),
      };
    });
  }

  /** 导入已有知识包，保留包内原始文件，同时建立结构化规格与版本索引。 */
  async importPackageVersion(
    projectId: string,
    input: ImportPackageVersionInput,
  ): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      validatePackageSpec(input.spec);
      validateCoverage(input.coverage);
      const packageVersion = requiredText(input.spec.version, '包版本', 100);
      const note = optionalText(input.note, '版本备注', 1000);
      const current = await this.repository.snapshot(projectId);
      const target = importedPackageFiles(input.spec, input.coverage, input.files);
      const changes = changesForManagedFiles(current.files, target);
      const id = `pv_${randomUUID().slice(0, 12)}`;
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.commit(
        projectId,
        input.expectedRevision,
        changes,
        gitMetadata(action),
      ), (savedRevision) => {
        this.db.prepare(`INSERT INTO package_versions
          (id, project_id, version, spec_json, coverage_json, note, revision_oid, created_at)
          VALUES (?,?,?,?,?,?,?,?)`)
          .run(
            id,
            projectId,
            packageVersion,
            JSON.stringify(input.spec),
            JSON.stringify(input.coverage),
            note,
            savedRevision.oid,
            savedRevision.createdAt,
          );
        this.recordRevision(
          projectId,
          savedRevision,
          action,
          changes.map((change) => change.path),
          packageVersion,
        );
      });
      return this.viewFromSnapshot(await this.repository.snapshot(projectId, revision.oid), packageVersion);
    });
  }

  /**
   * 把当前人工修订快照提升为知识包版本。
   * 只更新结构化规格与 manifest 的版本元数据，其他最终包文件保持原样。
   */
  async saveCurrentAsPackageVersion(
    projectId: string,
    input: SaveCurrentAsPackageVersionInput,
  ): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const packageVersion = requiredText(input.version, '包版本', 100);
      const note = optionalText(input.note, '版本备注', 1000);
      const current = await this.repository.snapshot(projectId);
      const currentView = this.viewFromSnapshot(current);
      const spec: PackageSpec = {
        ...currentView.spec,
        version: packageVersion,
        ...(input.status ? { status: input.status } : {}),
      };
      validatePackageSpec(spec);
      const changes = metadataChanges(current.files, spec);
      const id = `pv_${randomUUID().slice(0, 12)}`;
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.commit(
        projectId,
        input.expectedRevision,
        changes,
        gitMetadata(action),
      ), (savedRevision) => {
        this.db.prepare(`INSERT INTO package_versions
          (id, project_id, version, spec_json, coverage_json, note, revision_oid, created_at)
          VALUES (?,?,?,?,?,?,?,?)`)
          .run(
            id,
            projectId,
            packageVersion,
            JSON.stringify(spec),
            JSON.stringify(currentView.coverage),
            note,
            savedRevision.oid,
            savedRevision.createdAt,
          );
        this.recordRevision(
          projectId,
          savedRevision,
          action,
          changes.map((change) => change.path),
          packageVersion,
        );
      });
      return this.viewFromSnapshot(await this.repository.snapshot(projectId, revision.oid), packageVersion);
    });
  }

  /**
   * 将绑定 Git 基线的人工草稿发布为新版本。Git 提交、版本索引、修订索引和
   * 草稿状态在同一个补偿提交单元内完成；任一 SQLite 写入失败都会回滚 Git HEAD。
   */
  async publishDraftPackageVersion(
    projectId: string,
    input: PublishDraftPackageVersionInput,
  ): Promise<PublishedDraftPackageVersion> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      validatePackageSpec(input.spec);
      validateCoverage(input.coverage);
      const packageVersion = requiredText(input.spec.version, '包版本', 100);
      const note = optionalText(input.note, '版本备注', 1000);
      const draftId = requiredText(input.draftId, '人工草稿 ID', 100);
      if (!Number.isSafeInteger(input.expectedDraftRevision) || input.expectedDraftRevision < 0) {
        throw new PackageArtifactStoreError('invalid_input', '人工草稿修订号不正确');
      }

      const current = await this.repository.snapshot(projectId);
      assertExpectedRevision(input.expectedRevision, current.revision.oid);
      const currentView = this.viewFromSnapshot(current);
      const target = draftPackageFiles(current, currentView, input.spec, input.coverage);
      const changes = changesForManagedFiles(current.files, target);
      if (!changes.length) {
        throw new PackageArtifactStoreError('invalid_input', '人工草稿没有可发布的实际变化');
      }
      this.viewFromSnapshot(applyCandidateChanges(current, changes), packageVersion);

      const packageVersionId = `pv_${randomUUID().slice(0, 12)}`;
      const revision = await this.commitAndIndex(
        projectId,
        input.expectedRevision,
        () => this.repository.commit(
          projectId,
          input.expectedRevision,
          changes,
          gitMetadata(action),
        ),
        (savedRevision) => {
          this.db.prepare(`INSERT INTO package_versions
            (id, project_id, version, spec_json, coverage_json, note, revision_oid, created_at)
            VALUES (?,?,?,?,?,?,?,?)`)
            .run(
              packageVersionId,
              projectId,
              packageVersion,
              JSON.stringify(input.spec),
              JSON.stringify(input.coverage),
              note,
              savedRevision.oid,
              savedRevision.createdAt,
            );
          const updated = this.db.prepare(`UPDATE package_drafts
            SET status = 'published', revision = revision + 1, updated_at = ?
            WHERE id = ? AND project_id = ? AND status = 'active' AND revision = ?
              AND base_revision_oid = ?`)
            .run(
              savedRevision.createdAt,
              draftId,
              projectId,
              input.expectedDraftRevision,
              input.expectedRevision,
            );
          if (Number(updated.changes) !== 1) {
            throw new ArtifactRepositoryError(
              'revision_conflict',
              '人工草稿已更新、已结束或基线修订发生变化，请刷新后重试',
            );
          }
          this.recordRevision(
            projectId,
            savedRevision,
            action,
            changes.map((change) => change.path),
            packageVersion,
          );
        },
      );
      const draft = this.db.prepare(`SELECT id, revision, updated_at FROM package_drafts
        WHERE id = ? AND project_id = ?`).get(draftId, projectId) as {
          id: string;
          revision: number;
          updated_at: string;
        };
      return {
        package: this.viewFromSnapshot(
          await this.repository.snapshot(projectId, revision.oid),
          packageVersion,
        ),
        packageVersionId,
        changedPaths: changes.map((change) => change.path),
        draft: {
          id: draft.id,
          status: 'published',
          revision: draft.revision,
          updatedAt: draft.updated_at,
        },
      };
    });
  }

  /** 更新当前语义版本的状态，保留所有人工修订文件和修订历史。 */
  async finalizeCurrentPackageVersion(
    projectId: string,
    input: FinalizeCurrentPackageVersionInput,
  ): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const note = optionalText(input.note, '版本备注', 1000);
      const current = await this.repository.snapshot(projectId);
      const currentView = this.viewFromSnapshot(current);
      const spec: PackageSpec = { ...currentView.spec, status: input.status };
      const changes = metadataChanges(current.files, spec);
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.commit(
        projectId,
        input.expectedRevision,
        changes,
        gitMetadata(action),
      ), (savedRevision) => {
        const row = this.db.prepare(`SELECT id FROM package_versions
          WHERE project_id = ? AND version = ? ORDER BY rowid DESC LIMIT 1`)
          .get(projectId, currentView.packageVersion) as { id: string } | undefined;
        if (!row) throw new PackageArtifactStoreError('invalid_package', '当前知识包版本不存在');
        this.db.prepare(`UPDATE package_versions
          SET spec_json = ?, coverage_json = ?, note = ?, revision_oid = ?, created_at = ?
          WHERE id = ? AND project_id = ?`)
          .run(
            JSON.stringify(spec),
            JSON.stringify(currentView.coverage),
            note,
            savedRevision.oid,
            savedRevision.createdAt,
            row.id,
            projectId,
          );
        this.recordRevision(
          projectId,
          savedRevision,
          action,
          changes.map((change) => change.path),
          currentView.packageVersion,
        );
      });
      return this.viewFromSnapshot(
        await this.repository.snapshot(projectId, revision.oid),
        currentView.packageVersion,
      );
    });
  }

  async current(projectId: string): Promise<PackageArtifactView | null> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      return this.optionalViewFromSnapshot(await this.repository.snapshot(projectId));
    });
  }

  async byPackageVersion(projectId: string, version: string): Promise<PackageArtifactView | null> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const normalizedVersion = requiredText(version, '包版本', 100);
      const row = this.db.prepare(`SELECT version, revision_oid FROM package_versions
        WHERE project_id = ? AND version = ? AND revision_oid IS NOT NULL
        ORDER BY rowid DESC LIMIT 1`).get(projectId, normalizedVersion) as any;
      if (!row?.revision_oid) return null;
      return this.viewFromSnapshot(await this.repository.snapshot(projectId, row.revision_oid), row.version);
    });
  }

  async readFile(projectId: string, path: string, revision?: string): Promise<PackageArtifactFile> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const normalizedPath = validateReadablePath(path);
      const snapshot = await this.repository.snapshot(projectId, revision);
      const file = snapshot.files.find((item) => item.path === normalizedPath);
      if (!file) throw new PackageArtifactStoreError('file_not_found', '文件不存在');
      return { path: normalizedPath, content: decodeText(file.content), revision: snapshot.revision };
    });
  }

  async readPackageFiles(projectId: string, revision?: string): Promise<PackageArtifactFiles> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const snapshot = await this.repository.snapshot(projectId, revision);
      const view = this.viewFromSnapshot(snapshot);
      const files: Record<string, string> = {};
      const binaryFiles: string[] = [];
      for (const file of snapshot.files) {
        if (!file.path.startsWith(PACKAGE_PREFIX)) continue;
        const path = file.path.slice(PACKAGE_PREFIX.length);
        const content = tryDecodeText(file.content);
        if (content === null) binaryFiles.push(path);
        else files[path] = content;
      }
      return {
        revision: snapshot.revision,
        spec: view.spec,
        coverage: view.coverage,
        files,
        binaryFiles: binaryFiles.sort(compareNames),
      };
    });
  }

  async readPackageArchiveFiles(
    projectId: string,
    revision?: string,
  ): Promise<PackageArtifactArchiveFiles> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const snapshot = await this.repository.snapshot(projectId, revision);
      const view = this.viewFromSnapshot(snapshot);
      const files: Record<string, Uint8Array> = {};
      for (const file of snapshot.files) {
        if (!file.path.startsWith(PACKAGE_PREFIX)) continue;
        files[file.path.slice(PACKAGE_PREFIX.length)] = new Uint8Array(file.content);
      }
      return { revision: snapshot.revision, spec: view.spec, coverage: view.coverage, files };
    });
  }

  async saveFile(projectId: string, input: SavePackageFileInput): Promise<PackageArtifactFile> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const path = validatePackagePath(input.path);
      if (typeof input.content !== 'string') {
        throw new PackageArtifactStoreError('invalid_input', '文件内容不能为空');
      }
      const current = await this.repository.snapshot(projectId);
      if (!current.files.some((file) => file.path === path)) {
        throw new PackageArtifactStoreError('file_not_found', '文件不存在');
      }
      const changes: ArtifactChange[] = [{ kind: 'write', path, content: input.content }];
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.commit(
        projectId,
        input.expectedRevision,
        changes,
        gitMetadata(action),
      ), (savedRevision) => {
        this.recordRevision(projectId, savedRevision, action, [path]);
      });
      return this.readFileFromSnapshot(await this.repository.snapshot(projectId, revision.oid), path);
    });
  }

  async saveFiles(projectId: string, input: SavePackageFilesInput): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const entries = Object.entries(input.files ?? {});
      const deletePaths = [...new Set(input.deletePaths ?? [])];
      if ((!entries.length && !deletePaths.length)
        || entries.length + deletePaths.length > MAX_CANDIDATE_CHANGES) {
        throw new PackageArtifactStoreError('invalid_input', '请提交 1 到 500 个知识包文件变更');
      }
      const current = await this.repository.snapshot(projectId);
      assertExpectedRevision(input.expectedRevision, current.revision.oid);
      const currentView = this.viewFromSnapshot(current);
      const currentFiles = new Map(current.files.map((file) => [file.path, file.content]));
      const encoder = new TextEncoder();
      const writes: ArtifactChange[] = entries.map(([rawPath, content]) => {
        const path = validatePackagePath(rawPath);
        if (typeof content !== 'string') {
          throw new PackageArtifactStoreError('invalid_input', `文件内容不正确：${path}`);
        }
        return { kind: 'write' as const, path, content };
      });
      const writePaths = new Set(writes.map((change) => change.path));
      const changedWrites = writes.filter((change) => {
        if (change.kind !== 'write') return false;
        const before = currentFiles.get(change.path);
        return before === undefined || !bytesEqual(before, encoder.encode(String(change.content)));
      });
      const deletes: ArtifactChange[] = deletePaths.flatMap((rawPath) => {
        const path = validatePackagePath(rawPath);
        if (writePaths.has(path)) {
          throw new PackageArtifactStoreError('invalid_input', `同一文件不能同时写入和删除：${path}`);
        }
        return currentFiles.has(path) ? [{ kind: 'delete' as const, path }] : [];
      });
      const changes = [...changedWrites, ...deletes]
        .sort((left, right) => compareNames(left.path, right.path));
      if (!changes.length && action.cause === 'flowchart.confirm') return currentView;
      if (!changes.length) {
        throw new PackageArtifactStoreError('invalid_input', '提交内容没有实际变化');
      }
      this.viewFromSnapshot(applyCandidateChanges(current, changes), currentView.packageVersion);
      const revision = await this.commitAndIndex(
        projectId,
        input.expectedRevision,
        () => this.repository.commit(
          projectId,
          input.expectedRevision,
          changes,
          gitMetadata(action),
        ),
        (savedRevision) => {
          this.recordRevision(projectId, savedRevision, action, changes.map((change) => change.path));
        },
      );
      return this.viewFromSnapshot(
        await this.repository.snapshot(projectId, revision.oid),
        currentView.packageVersion,
      );
    });
  }

  async resetFile(projectId: string, input: ResetPackageFileInput): Promise<PackageArtifactFile> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const path = validatePackagePath(input.path);
      const current = await this.repository.snapshot(projectId);
      const view = this.viewFromSnapshot(current);
      const rendered = renderPackage(view.spec);
      const generated = rendered[path.slice(PACKAGE_PREFIX.length)];
      if (generated === undefined) throw new PackageArtifactStoreError('file_not_found', '文件不存在');
      const changes: ArtifactChange[] = [{ kind: 'write', path, content: generated }];
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.commit(
        projectId,
        input.expectedRevision,
        changes,
        gitMetadata(action),
      ), (savedRevision) => {
        this.recordRevision(projectId, savedRevision, action, [path]);
      });
      return this.readFileFromSnapshot(await this.repository.snapshot(projectId, revision.oid), path);
    });
  }

  async history(projectId: string, limit = 50): Promise<ArtifactRevisionRecord[]> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      return this.revisions.list(projectId, limit);
    });
  }

  async diff(projectId: string, fromRevision: string, toRevision: string): Promise<ArtifactDiff> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      return this.repository.diff(projectId, fromRevision, toRevision);
    });
  }

  async restore(projectId: string, input: RestorePackageInput): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const target = await this.repository.snapshot(projectId, input.targetRevision);
      if (!target.files.some((file) => file.path === SPEC_PATH)) {
        throw new PackageArtifactStoreError('revision_has_no_package', '这个版本还没有知识包');
      }
      const targetView = this.viewFromSnapshot(target);
      const changedPaths = (await this.repository.diff(
        projectId,
        input.expectedRevision,
        input.targetRevision,
      )).files.map((file) => file.path);
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.restore(
        projectId,
        input.targetRevision,
        input.expectedRevision,
        gitMetadata(action),
      ), (savedRevision) => {
        this.recordRevision(projectId, savedRevision, action, changedPaths, targetView.packageVersion);
      });
      return this.viewFromSnapshot(await this.repository.snapshot(projectId, revision.oid));
    });
  }

  /** 当前确认版本改变后，仍基于旧版本的未处理方案不再可应用，但保留完整审计内容。 */
  supersedePendingCandidateRevisions(
    projectId: string,
    actorName: string,
    note = '阶段回退后候选方案的基线已经失效',
  ): number {
    const actor = requiredText(actorName, '操作人', 100);
    const candidates = (this.db.prepare(`SELECT id FROM artifact_candidate_revisions
      WHERE project_id = ? AND status = 'pending' ORDER BY created_at`)
      .all(projectId) as Array<{ id: string }>);
    for (const row of candidates) {
      const candidate = this.candidateRevisionUnlocked(projectId, row.id);
      this.decideCandidate(candidate, 'superseded', {
        cause: 'candidate_superseded',
        summary: '候选修订因阶段回退失效',
        actorKind: 'user',
        actorName: actor,
      }, candidate.selectedGroupIds, note);
    }
    return candidates.length;
  }

  /**
   * 保存不影响当前确认版本的候选修订。变更组在创建后不可变，接受时才会形成 Git 修订。
   */
  async createCandidateRevision(
    projectId: string,
    input: CreatePackageCandidateRevisionInput,
  ): Promise<PackageCandidateRevision> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const current = await this.repository.snapshot(projectId);
      assertExpectedRevision(input.expectedRevision, current.revision.oid);
      const groups = normalizeCandidateGroups(input.groups, current.files);
      const selection = resolveCandidateSelection(groups);
      const target = applyCandidateChanges(current, selectedCandidateChanges(groups, selection.selectedGroupIds));
      this.viewFromSnapshot(target);

      const candidate: PackageCandidateRevision = {
        id: `cr_${randomUUID().slice(0, 12)}`,
        projectId,
        baseRevision: current.revision.oid,
        status: 'pending',
        cause: action.cause,
        summary: action.summary,
        actorKind: action.actorKind,
        actorName: action.actorName,
        groups,
        selectedGroupIds: [],
        createdAt: (action.createdAt ?? new Date()).toISOString(),
      };
      this.db.prepare(`INSERT INTO artifact_candidate_revisions (
        id, project_id, base_revision_oid, status, cause, summary, actor_kind, actor_name,
        groups_json, selected_group_ids_json, created_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(
        candidate.id,
        projectId,
        candidate.baseRevision,
        candidate.status,
        candidate.cause,
        candidate.summary,
        candidate.actorKind,
        candidate.actorName,
        serializeCandidateGroups(groups),
        '[]',
        candidate.createdAt,
      );
      return cloneCandidate(candidate);
    });
  }

  async getCandidateRevision(projectId: string, candidateId: string): Promise<PackageCandidateRevision> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      return this.candidateRevisionUnlocked(projectId, candidateId);
    });
  }

  async listCandidateRevisions(
    projectId: string,
    status?: PackageCandidateRevisionStatus,
    limit = 100,
  ): Promise<PackageCandidateRevision[]> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const normalizedLimit = Math.max(1, Math.min(200, Math.trunc(limit) || 100));
      if (status !== undefined && !CANDIDATE_STATUSES.has(status)) {
        throw new PackageArtifactStoreError('invalid_input', '候选修订状态不正确');
      }
      const rows = status
        ? this.db.prepare(`SELECT * FROM artifact_candidate_revisions
          WHERE project_id = ? AND status = ? ORDER BY created_at DESC, rowid DESC LIMIT ?`)
          .all(projectId, status, normalizedLimit)
        : this.db.prepare(`SELECT * FROM artifact_candidate_revisions
          WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?`)
          .all(projectId, normalizedLimit);
      return (rows as any[]).map(candidateFromRow);
    });
  }

  async candidateRevisionOverview(
    projectId: string,
    status?: PackageCandidateRevisionStatus,
    limit = 100,
  ): Promise<PackageCandidateRevisionOverview> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const normalizedLimit = Math.max(1, Math.min(200, Math.trunc(limit) || 100));
      if (status !== undefined && !CANDIDATE_STATUSES.has(status)) {
        throw new PackageArtifactStoreError('invalid_input', '候选修订状态不正确');
      }
      const rows = status
        ? this.db.prepare(`SELECT * FROM artifact_candidate_revisions
          WHERE project_id = ? AND status = ? ORDER BY created_at DESC, rowid DESC LIMIT ?`)
          .all(projectId, status, normalizedLimit)
        : this.db.prepare(`SELECT * FROM artifact_candidate_revisions
          WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?`)
          .all(projectId, normalizedLimit);
      return {
        candidates: (rows as any[]).map(candidateFromRow),
        pendingCount: this.pendingCandidateRevisionCountUnlocked(projectId),
      };
    });
  }

  async hasPendingCandidateRevisions(projectId: string): Promise<boolean> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      return this.pendingCandidateRevisionCountUnlocked(projectId) > 0;
    });
  }

  async countPendingCandidateRevisions(projectId: string): Promise<number> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      return this.pendingCandidateRevisionCountUnlocked(projectId);
    });
  }

  async previewCandidateRevision(
    projectId: string,
    candidateId: string,
    selectedGroupIds?: readonly string[],
  ): Promise<PackageCandidatePreview> {
    return this.runExclusive(projectId, async () => {
      await this.ensureProjectUnlocked(projectId);
      const candidate = this.candidateRevisionUnlocked(projectId, candidateId);
      const selection = resolveCandidateSelection(candidate.groups, selectedGroupIds);
      return this.previewCandidateUnlocked(candidate, selection);
    });
  }

  async acceptCandidateRevision(
    projectId: string,
    input: AcceptPackageCandidateRevisionInput,
  ): Promise<AcceptedPackageCandidateRevision> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      const note = optionalText(input.note, '处理备注', 1000) ?? undefined;
      await this.ensureProjectUnlocked(projectId);
      const candidate = this.candidateRevisionUnlocked(projectId, input.candidateId);
      assertPendingCandidate(candidate);
      const selection = resolveCandidateSelection(candidate.groups, input.selectedGroupIds);
      assertCandidateValidation(candidate.groups, selection.selectedGroupIds);

      const current = await this.repository.snapshot(projectId);
      if (current.revision.oid !== candidate.baseRevision) {
        this.decideCandidate(candidate, 'superseded', {
          ...action,
          cause: 'candidate_superseded',
          summary: '候选修订因基线变化失效',
        }, selection.selectedGroupIds,
          note ?? '当前确认版本已经变化');
        throw new ArtifactRepositoryError(
          'revision_conflict',
          '当前确认版本已经变化，请基于最新版本重新创建候选修订',
          { expectedRevision: candidate.baseRevision, actualRevision: current.revision.oid },
        );
      }

      const currentView = this.viewFromSnapshot(current);
      const currentRevision = this.revisions.get(projectId, current.revision.oid);
      let packageVersion: string;
      try {
        packageVersion = nextValidationCandidateVersion({
          currentVersion: currentView.packageVersion,
          currentStatus: currentView.spec.status ?? 'draft',
          ...(currentRevision?.cause === 'package_import' ? { changeLevel: 'minor' as const } : {}),
        });
      } catch (cause) {
        throw new PackageArtifactStoreError(
          'invalid_input',
          cause instanceof Error ? cause.message : '无法生成下一个知识包版本',
        );
      }
      const contentChanges = selectedCandidateChanges(candidate.groups, selection.selectedGroupIds);
      const candidateTarget = applyCandidateChanges(current, contentChanges);
      const candidateView = this.viewFromSnapshot(candidateTarget);
      const renderedCandidateSpec = parseRenderedPackageFiles(packageTextFiles(candidateTarget.files));
      const structuredSpecChanged = contentChanges.some((change) => (
        change.kind === 'write' && change.path === SPEC_PATH
      ));
      // Domain tools stage an authoritative structured spec for fields that Markdown cannot carry.
      const candidateSpec = structuredSpecChanged
        ? candidateView.spec
        : preserveUnrenderedPackageMetadata(renderedCandidateSpec, candidateView.spec, contentChanges);
      validateChangedCandidateScripts(currentView.spec, candidateSpec, candidateTarget, contentChanges);
      const allowedVersions = [currentView.spec.version, packageVersion];
      if (!allowedVersions.includes(candidateSpec.version) || !allowedVersions.includes(candidateView.spec.version)) {
        throw new PackageArtifactStoreError('invalid_input', '候选修订不能自行指定知识包版本');
      }
      const versionedSpec: PackageSpec = {
        ...candidateSpec,
        ...(input.packageDataSourceType ? { dataSourceType: input.packageDataSourceType } : {}),
        version: packageVersion,
        status: 'review',
      };
      const changes = overlayArtifactChanges(
        contentChanges,
        metadataChanges(candidateTarget.files, versionedSpec),
      );
      const target = applyCandidateChanges(current, changes);
      const acceptedView = this.viewFromSnapshot(target, packageVersion);
      const packageVersionId = `pv_${randomUUID().slice(0, 12)}`;
      const revision = await this.commitAndIndex(
        projectId,
        candidate.baseRevision,
        () => this.repository.commit(
          projectId,
          candidate.baseRevision,
          changes,
          gitMetadata(action),
        ),
        (savedRevision) => {
          this.db.prepare(`INSERT INTO package_versions
            (id, project_id, version, spec_json, coverage_json, note, revision_oid, created_at)
            VALUES (?,?,?,?,?,?,?,?)`)
            .run(
              packageVersionId,
              projectId,
              packageVersion,
              JSON.stringify(acceptedView.spec),
              JSON.stringify(acceptedView.coverage),
              note ?? candidate.summary,
              savedRevision.oid,
              savedRevision.createdAt,
            );
          this.recordRevision(
            projectId,
            savedRevision,
            action,
            changes.map((change) => change.path),
            packageVersion,
          );
          this.decideCandidate(
            candidate,
            'accepted',
            action,
            selection.selectedGroupIds,
            note,
            savedRevision.oid,
          );
        },
      );
      return {
        candidate: this.candidateRevisionUnlocked(projectId, candidate.id),
        selection,
        revision,
        packageVersion,
      };
    });
  }

  async rejectCandidateRevision(
    projectId: string,
    input: DecidePackageCandidateRevisionInput,
  ): Promise<PackageCandidateRevision> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      const note = optionalText(input.note, '处理备注', 1000) ?? undefined;
      await this.ensureProjectUnlocked(projectId);
      const candidate = this.candidateRevisionUnlocked(projectId, input.candidateId);
      assertPendingCandidate(candidate);
      this.decideCandidate(candidate, 'rejected', action, [], note);
      return this.candidateRevisionUnlocked(projectId, candidate.id);
    });
  }

  async returnCandidateRevision(
    projectId: string,
    input: ReturnPackageCandidateRevisionInput,
  ): Promise<PackageCandidateRevision> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      const note = requiredText(input.note ?? '', '退回说明', 1000);
      await this.ensureProjectUnlocked(projectId);
      const candidate = this.candidateRevisionUnlocked(projectId, input.candidateId);
      assertPendingCandidate(candidate);
      const selection = resolveCandidateSelection(candidate.groups, input.selectedGroupIds);
      this.decideCandidate(
        candidate,
        'revision_requested',
        action,
        selection.requestedGroupIds,
        note,
      );
      return this.candidateRevisionUnlocked(projectId, candidate.id);
    });
  }

  private candidateRevisionUnlocked(projectId: string, candidateId: string): PackageCandidateRevision {
    const id = requiredText(candidateId, '候选修订 ID', 100);
    const row = this.db.prepare(`SELECT * FROM artifact_candidate_revisions
      WHERE id = ? AND project_id = ?`).get(id, projectId) as any;
    if (!row) throw new PackageArtifactStoreError('candidate_not_found', '候选修订不存在');
    return candidateFromRow(row);
  }

  private pendingCandidateRevisionCountUnlocked(projectId: string): number {
    const row = this.db.prepare(`SELECT COUNT(*) AS count FROM artifact_candidate_revisions
      WHERE project_id = ? AND status = 'pending'`).get(projectId) as { count: number };
    return Number(row.count);
  }

  private async previewCandidateUnlocked(
    candidate: PackageCandidateRevision,
    selection: PackageCandidateSelection,
  ): Promise<PackageCandidatePreview> {
    const base = await this.repository.snapshot(candidate.projectId, candidate.baseRevision);
    const target = applyCandidateChanges(
      base,
      selectedCandidateChanges(candidate.groups, selection.selectedGroupIds),
    );
    const view = this.viewFromSnapshot(target);
    const files: Record<string, string> = {};
    const binaryFiles: string[] = [];
    for (const file of target.files) {
      if (!file.path.startsWith(PACKAGE_PREFIX)) continue;
      const path = file.path.slice(PACKAGE_PREFIX.length);
      const content = tryDecodeText(file.content);
      if (content === null) binaryFiles.push(path);
      else files[path] = content;
    }
    return {
      candidate: cloneCandidate(candidate),
      selection,
      spec: view.spec,
      coverage: view.coverage,
      files,
      binaryFiles: binaryFiles.sort(compareNames),
      diff: diffCandidateSnapshots(base, target),
    };
  }

  private decideCandidate(
    candidate: PackageCandidateRevision,
    status: Exclude<PackageCandidateRevisionStatus, 'pending'>,
    action: NormalizedAction,
    selectedGroupIds: readonly string[],
    note?: string,
    acceptedRevision?: string,
  ): void {
    const decidedAt = (action.createdAt ?? new Date()).toISOString();
    const result = this.db.prepare(`UPDATE artifact_candidate_revisions SET
      status = ?, selected_group_ids_json = ?, accepted_revision_oid = ?,
      decision_cause = ?, decision_summary = ?, decision_actor_kind = ?, decision_actor_name = ?,
      decision_note = ?, decided_at = ?
      WHERE id = ? AND project_id = ? AND status = 'pending'`).run(
        status,
        JSON.stringify(selectedGroupIds),
        acceptedRevision ?? null,
        action.cause,
        action.summary,
        action.actorKind,
        action.actorName,
        note ?? null,
        decidedAt,
        candidate.id,
        candidate.projectId,
      );
    if (Number(result.changes) !== 1) {
      throw new PackageArtifactStoreError('candidate_not_pending', '候选修订已经处理');
    }
  }

  private async ensureProjectUnlocked(projectId: string): Promise<ArtifactRevision> {
    this.assertProject(projectId);
    await this.repository.initialize(projectId);
    await this.recoverInterruptedCommit(projectId);
    const currentAfterRecovery = await this.repository.current(projectId);
    if (!this.revisions.latest(projectId)) {
      let initial = currentAfterRecovery;
      while (initial.parentOid) {
        initial = (await this.repository.snapshot(projectId, initial.parentOid)).revision;
      }
      this.revisions.record({
        projectId,
        oid: initial.oid,
        parentOid: initial.parentOid,
        cause: 'project_created',
        summary: '创建项目',
        actorKind: 'system',
        actorName: '建模系统',
        changedPaths: [],
        createdAt: initial.createdAt,
      });
    }

    const recovered = await this.rollbackUnindexedCommits(projectId, currentAfterRecovery);
    const legacyRows = this.db.prepare(`SELECT rowid AS source_rowid, * FROM package_versions
      WHERE project_id = ? ORDER BY rowid`).all(projectId) as any[];
    const current = await this.repository.snapshot(projectId, recovered.oid);
    const needsMigration = legacyRows.some((row) => !row.revision_oid)
      || (legacyRows.length > 0 && !current.files.some((file) => file.path === SPEC_PATH));
    if (!needsMigration) return current.revision;

    let head = current;
    for (const row of legacyRows) {
      if (row.revision_oid) continue;
      const spec = parseSpec(row.spec_json);
      const coverage = parseCoverage(row.coverage_json);
      const packageVersion = requiredText(String(row.version ?? ''), '包版本', 100);
      const target = packageFiles(spec, coverage);
      // 历史版本补建 revision_oid 时，当前 Git HEAD 可能已含流程图、
      // 导入附件或扩展目录。这些不属于规格派生文件，迁移不得删除。
      preserveImportedExtras(head.files, target);
      const changes = changesForManagedFiles(head.files, target);
      let revision = head.revision;
      if (changes.length > 0) {
        const createdAt = optionalDate(row.created_at);
        const summary = optionalText(row.note, '版本备注', 1000) ?? `导入版本 ${row.version}`;
        revision = await this.commitAndIndex(projectId, head.revision.oid, () => this.repository.commit(
          projectId,
          head.revision.oid,
          changes,
          {
            message: summary,
            ...(createdAt ? { createdAt } : {}),
          },
        ), (savedRevision) => {
          this.revisions.record({
            projectId,
            oid: savedRevision.oid,
            parentOid: savedRevision.parentOid,
            cause: 'migration',
            summary,
            actorKind: 'system',
            actorName: '历史数据',
            packageVersion,
            changedPaths: changes.map((change) => change.path),
            createdAt: savedRevision.createdAt,
          });
          this.db.prepare('UPDATE package_versions SET revision_oid = ? WHERE id = ? AND project_id = ?')
            .run(savedRevision.oid, row.id, projectId);
        });
        head = await this.repository.snapshot(projectId, revision.oid);
      } else {
        this.db.prepare('UPDATE package_versions SET revision_oid = ? WHERE id = ? AND project_id = ?')
          .run(revision.oid, row.id, projectId);
      }
    }
    return head.revision;
  }

  private async recoverInterruptedCommit(projectId: string): Promise<void> {
    const intent = this.db.prepare(`SELECT base_revision_oid, created_at FROM artifact_commit_intents
      WHERE project_id = ?`).get(projectId) as {
        base_revision_oid: string;
        created_at: string;
      } | undefined;
    if (!intent) return;
    if (isWithinRecoveryGrace(intent.created_at)) {
      throw new PackageArtifactStoreError('commit_in_progress', '知识包正在保存，请稍后重试');
    }

    const current = await this.repository.snapshot(projectId);
    if (this.revisions.get(projectId, current.revision.oid)) {
      this.deleteCommitIntent(projectId);
      return;
    }
    if (current.revision.oid === intent.base_revision_oid) {
      this.deleteCommitIntent(projectId);
      return;
    }
    if (current.revision.parentOid !== intent.base_revision_oid) {
      throw new PackageArtifactStoreError(
        'invalid_package',
        '检测到未完成的知识包提交，但 Git HEAD 已继续变化，已拒绝自动改写历史',
      );
    }

    await this.repository.rollbackCommit(projectId, current.revision.oid);
    this.deleteCommitIntent(projectId);
  }

  private optionalViewFromSnapshot(snapshot: ArtifactSnapshot): PackageArtifactView | null {
    return snapshot.files.some((file) => file.path === SPEC_PATH) ? this.viewFromSnapshot(snapshot) : null;
  }

  private viewFromSnapshot(snapshot: ArtifactSnapshot, packageVersion?: string): PackageArtifactView {
    const specFile = snapshot.files.find((file) => file.path === SPEC_PATH);
    if (!specFile) throw new PackageArtifactStoreError('package_not_found', '尚未生成知识包');
    const coverageFile = snapshot.files.find((file) => file.path === COVERAGE_PATH);
    const spec = parseSpec(decodeText(specFile.content));
    const coverage = parseCoverage(coverageFile ? decodeText(coverageFile.content) : null);
    return {
      revision: snapshot.revision,
      packageVersion: packageVersion ?? spec.version,
      spec,
      coverage,
      files: snapshot.files.map((file) => file.path).filter((path) => path.startsWith(PACKAGE_PREFIX)).sort(compareNames),
    };
  }

  private readFileFromSnapshot(snapshot: ArtifactSnapshot, path: string): PackageArtifactFile {
    const file = snapshot.files.find((item) => item.path === path);
    if (!file) throw new PackageArtifactStoreError('file_not_found', '文件不存在');
    return { path, content: decodeText(file.content), revision: snapshot.revision };
  }

  private recordRevision(
    projectId: string,
    revision: ArtifactRevision,
    action: NormalizedAction,
    changedPaths: readonly string[],
    packageVersion?: string,
  ): void {
    this.revisions.record({
      projectId,
      oid: revision.oid,
      parentOid: revision.parentOid,
      cause: action.cause,
      summary: action.summary,
      actorKind: action.actorKind,
      actorName: action.actorName,
      packageVersion,
      changedPaths,
      createdAt: revision.createdAt,
    });
  }

  private assertProject(projectId: string): void {
    const row = this.db.prepare('SELECT 1 FROM projects WHERE id = ?').get(projectId);
    if (!row) throw new PackageArtifactStoreError('project_not_found', '项目不存在');
  }

  /**
   * SQLite 中的修订索引是跨存储提交的完成标记。如果进程在 Git 提交后、
   * SQLite 事务提交前退出，下次访问会丢弃这些未建立索引的尾部提交。
   */
  private async rollbackUnindexedCommits(
    projectId: string,
    current: ArtifactRevision,
  ): Promise<ArtifactRevision> {
    const indexed = this.revisions.latest(projectId);
    if (!indexed || indexed.oid === current.oid) return current;
    if (isWithinRecoveryGrace(current.createdAt)) {
      throw new PackageArtifactStoreError('commit_in_progress', '知识包正在保存，请稍后重试');
    }

    const dangling: ArtifactRevision[] = [];
    let cursor = current;
    while (cursor.oid !== indexed.oid) {
      if (!cursor.parentOid) {
        throw new ArtifactRepositoryError(
          'invalid_repository',
          'Git 当前修订与 SQLite 修订索引不在同一历史链',
        );
      }
      dangling.push(cursor);
      cursor = (await this.repository.snapshot(projectId, cursor.parentOid)).revision;
    }

    let recovered = current;
    for (const revision of dangling) {
      recovered = await this.repository.rollbackCommit(projectId, revision.oid);
    }
    return recovered;
  }

  /**
   * Git 和 SQLite 无法参加同一个底层事务。因此所有正式修订都在项目串行队列内
   * 先形成 Git 提交，再用单个 SQLite 事务写入索引；索引失败时将 HEAD
   * 按 CAS 规则退回刚才的父修订，避免对外暴露“有文件、无索引”的半提交。
   */
  private async commitAndIndex(
    projectId: string,
    baseRevision: string,
    commit: () => Promise<ArtifactRevision>,
    index: (revision: ArtifactRevision) => void,
  ): Promise<ArtifactRevision> {
    const createdAt = new Date().toISOString();
    try {
      this.db.prepare(`INSERT INTO artifact_commit_intents
        (project_id, base_revision_oid, created_at) VALUES (?,?,?)`)
        .run(projectId, baseRevision, createdAt);
    } catch (error) {
      if (this.db.prepare('SELECT 1 FROM artifact_commit_intents WHERE project_id = ?').get(projectId)) {
        throw new PackageArtifactStoreError('commit_in_progress', '知识包正在保存，请稍后重试');
      }
      throw error;
    }

    let revision: ArtifactRevision;
    try {
      revision = await commit();
    } catch (commitError) {
      this.deleteCommitIntent(projectId);
      throw commitError;
    }

    try {
      this.transaction(() => {
        index(revision);
        const deleted = this.db.prepare('DELETE FROM artifact_commit_intents WHERE project_id = ?')
          .run(projectId);
        if (Number(deleted.changes) !== 1) {
          throw new Error('知识包提交意图不存在');
        }
      });
      return revision;
    } catch (indexError) {
      try {
        await this.repository.rollbackCommit(projectId, revision.oid);
      } catch (rollbackError) {
        throw new AggregateError(
          [indexError, rollbackError],
          `知识包索引写入失败，且 Git 修订 ${revision.oid} 无法自动回滚`,
        );
      }
      this.deleteCommitIntent(projectId);
      throw indexError;
    }
  }

  private deleteCommitIntent(projectId: string): void {
    this.db.prepare('DELETE FROM artifact_commit_intents WHERE project_id = ?').run(projectId);
  }

  private transaction<T>(task: () => T): T {
    if (this.db.isTransaction) {
      const savepoint = `package_artifact_${randomUUID().replaceAll('-', '')}`;
      this.db.exec(`SAVEPOINT ${savepoint}`);
      try {
        const result = task();
        this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
        return result;
      } catch (error) {
        this.db.exec(`ROLLBACK TO SAVEPOINT ${savepoint}`);
        this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
        throw error;
      }
    }

    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = task();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }

  private async runExclusive<T>(projectId: string, task: () => Promise<T>): Promise<T> {
    return runProjectWriteExclusive(projectId, task);
  }
}

interface NormalizedAction extends PackageRevisionAction {
  cause: string;
  summary: string;
  actorName: string;
}

interface StoredCandidateChangeGroup {
  id: string;
  summary: string;
  dependsOn: string[];
  changes: Array<
    | { kind: 'write'; path: string; contentBase64: string }
    | { kind: 'delete'; path: string }
  >;
  validation: PackageCandidateValidation;
}

function normalizeCandidateGroups(
  value: readonly PackageCandidateChangeGroupInput[],
  baseFiles: readonly ArtifactFile[],
): PackageCandidateChangeGroup[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_CANDIDATE_GROUPS) {
    throw new PackageArtifactStoreError(
      'invalid_input',
      `候选修订必须包含 1 到 ${MAX_CANDIDATE_GROUPS} 个原子变更组`,
    );
  }
  const encoder = new TextEncoder();
  const base = new Map(baseFiles.map((file) => [file.path, file.content]));
  const groupIds = new Set<string>();
  const changedPaths = new Set<string>();
  let changeCount = 0;
  let contentBytes = 0;

  const groups = value.map((rawGroup) => {
    if (!rawGroup || typeof rawGroup !== 'object') {
      throw new PackageArtifactStoreError('invalid_input', '原子变更组不正确');
    }
    const id = requiredText(rawGroup.id, '变更组 ID', 64);
    if (!GROUP_ID_PATTERN.test(id) || groupIds.has(id)) {
      throw new PackageArtifactStoreError('invalid_input', `变更组 ID 不正确或重复：${id}`);
    }
    groupIds.add(id);
    if (!Array.isArray(rawGroup.changes) || rawGroup.changes.length < 1) {
      throw new PackageArtifactStoreError('invalid_input', `变更组 ${id} 没有文件修改`);
    }
    const changes = rawGroup.changes.map((rawChange: ArtifactChange): ArtifactChange => {
      if (!rawChange || typeof rawChange !== 'object') {
        throw new PackageArtifactStoreError('invalid_input', `变更组 ${id} 的文件修改不正确`);
      }
      const path = validateReadablePath(rawChange.path);
      if (changedPaths.has(path)) {
        throw new PackageArtifactStoreError('invalid_input', `多个变更组不能同时修改同一文件：${path}`);
      }
      changedPaths.add(path);
      changeCount += 1;
      if (changeCount > MAX_CANDIDATE_CHANGES) {
        throw new PackageArtifactStoreError(
          'invalid_input',
          `候选修订最多包含 ${MAX_CANDIDATE_CHANGES} 个文件修改`,
        );
      }
      if (rawChange.kind === 'delete') {
        if (!base.has(path)) {
          throw new PackageArtifactStoreError('invalid_input', `候选删除没有实际变化：${path}`);
        }
        return { kind: 'delete', path };
      }
      if (rawChange.kind !== 'write'
        || (typeof rawChange.content !== 'string' && !(rawChange.content instanceof Uint8Array))) {
        throw new PackageArtifactStoreError('invalid_input', `候选文件内容不正确：${path}`);
      }
      const content = typeof rawChange.content === 'string'
        ? encoder.encode(rawChange.content)
        : new Uint8Array(rawChange.content);
      contentBytes += content.byteLength;
      if (contentBytes > MAX_CANDIDATE_BYTES) {
        throw new PackageArtifactStoreError('invalid_input', '候选修订文件内容不能超过 10 MiB');
      }
      const before = base.get(path);
      if (before !== undefined && bytesEqual(before, content)) {
        throw new PackageArtifactStoreError('invalid_input', `候选写入没有实际变化：${path}`);
      }
      return { kind: 'write', path, content };
    }).sort((left: ArtifactChange, right: ArtifactChange) => compareNames(left.path, right.path));
    return {
      id,
      summary: requiredText(rawGroup.summary, '变更组说明', 500),
      dependsOn: normalizeCandidateDependencies(rawGroup.dependsOn, id),
      changes,
      validation: {
        errors: normalizeCandidateMessages(rawGroup.validation?.errors, '校验错误'),
        warnings: normalizeCandidateMessages(rawGroup.validation?.warnings, '校验警告'),
      },
    };
  });

  for (const group of groups) {
    for (const dependency of group.dependsOn) {
      if (!groupIds.has(dependency)) {
        throw new PackageArtifactStoreError(
          'invalid_input',
          `变更组 ${group.id} 引用了不存在的依赖：${dependency}`,
        );
      }
    }
  }
  assertCandidateDependencyGraph(groups);
  return groups;
}

function normalizeCandidateDependencies(value: readonly string[] | undefined, groupId: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > MAX_CANDIDATE_GROUPS) {
    throw new PackageArtifactStoreError('invalid_input', `变更组 ${groupId} 的依赖不正确`);
  }
  const dependencies: string[] = [];
  const seen = new Set<string>();
  for (const raw of value) {
    const dependency = requiredText(raw, '变更组依赖', 64);
    if (!GROUP_ID_PATTERN.test(dependency) || dependency === groupId) {
      throw new PackageArtifactStoreError('invalid_input', `变更组 ${groupId} 的依赖不正确`);
    }
    if (!seen.has(dependency)) dependencies.push(dependency);
    seen.add(dependency);
  }
  return dependencies;
}

function normalizeCandidateMessages(value: readonly string[] | undefined, label: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 100) {
    throw new PackageArtifactStoreError('invalid_input', `${label}不正确`);
  }
  return value.map((message) => requiredText(message, label, 1000));
}

function assertCandidateDependencyGraph(groups: readonly PackageCandidateChangeGroup[]): void {
  const byId = new Map(groups.map((group) => [group.id, group]));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (id: string) => {
    if (visiting.has(id)) {
      throw new PackageArtifactStoreError('invalid_input', `原子变更组依赖形成循环：${id}`);
    }
    if (visited.has(id)) return;
    visiting.add(id);
    for (const dependency of byId.get(id)?.dependsOn ?? []) visit(dependency);
    visiting.delete(id);
    visited.add(id);
  };
  for (const group of groups) visit(group.id);
}

function resolveCandidateSelection(
  groups: readonly PackageCandidateChangeGroup[],
  selectedGroupIds?: readonly string[],
): PackageCandidateSelection {
  if (selectedGroupIds !== undefined && (!Array.isArray(selectedGroupIds) || selectedGroupIds.length < 1)) {
    throw new PackageArtifactStoreError('invalid_input', '请至少选择一个原子变更组');
  }
  const byId = new Map(groups.map((group) => [group.id, group]));
  const requested = new Set<string>();
  for (const rawId of selectedGroupIds ?? groups.map((group) => group.id)) {
    const id = requiredText(rawId, '变更组 ID', 64);
    if (!byId.has(id)) {
      throw new PackageArtifactStoreError('invalid_input', `候选修订不包含变更组：${id}`);
    }
    requested.add(id);
  }
  const included = new Set<string>();
  const include = (id: string) => {
    if (included.has(id)) return;
    const group = byId.get(id);
    if (!group) throw new PackageArtifactStoreError('invalid_input', `候选修订不包含变更组：${id}`);
    for (const dependency of group.dependsOn) include(dependency);
    included.add(id);
  };
  for (const id of requested) include(id);
  const requestedGroupIds = groups.filter((group) => requested.has(group.id)).map((group) => group.id);
  const resolvedGroupIds = groups.filter((group) => included.has(group.id)).map((group) => group.id);
  return {
    requestedGroupIds,
    selectedGroupIds: resolvedGroupIds,
    autoIncludedGroupIds: resolvedGroupIds.filter((id) => !requested.has(id)),
  };
}

function selectedCandidateChanges(
  groups: readonly PackageCandidateChangeGroup[],
  selectedGroupIds: readonly string[],
): ArtifactChange[] {
  const selected = new Set(selectedGroupIds);
  return groups
    .filter((group) => selected.has(group.id))
    .flatMap((group) => group.changes.map(cloneArtifactChange));
}

function overlayArtifactChanges(
  changes: readonly ArtifactChange[],
  replacements: readonly ArtifactChange[],
): ArtifactChange[] {
  const replacementPaths = new Set(replacements.map((change) => change.path));
  return [
    ...changes.filter((change) => !replacementPaths.has(change.path)),
    ...replacements,
  ].sort((left, right) => compareNames(left.path, right.path));
}

function assertCandidateValidation(
  groups: readonly PackageCandidateChangeGroup[],
  selectedGroupIds: readonly string[],
): void {
  const selected = new Set(selectedGroupIds);
  const errors = groups
    .filter((group) => selected.has(group.id))
    .flatMap((group) => group.validation.errors.map((error) => `${group.summary}：${error}`));
  if (errors.length > 0) {
    throw new PackageArtifactStoreError(
      'candidate_validation_failed',
      `候选修订校验未通过：${errors.join('；')}`,
    );
  }
}

function applyCandidateChanges(
  base: ArtifactSnapshot,
  changes: readonly ArtifactChange[],
): ArtifactSnapshot {
  const files = new Map(base.files.map((file) => [file.path, new Uint8Array(file.content)]));
  const encoder = new TextEncoder();
  for (const change of changes) {
    if (change.kind === 'delete') files.delete(change.path);
    else files.set(
      change.path,
      typeof change.content === 'string' ? encoder.encode(change.content) : new Uint8Array(change.content),
    );
  }
  return {
    revision: base.revision,
    files: [...files.entries()]
      .sort(([left], [right]) => compareNames(left, right))
      .map(([path, content]) => ({ path, content })),
  };
}

function packageTextFiles(files: readonly ArtifactFile[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const file of files) {
    if (!file.path.startsWith(PACKAGE_PREFIX)) continue;
    const content = tryDecodeText(file.content);
    if (content !== null) result[file.path.slice(PACKAGE_PREFIX.length)] = content;
  }
  return result;
}

function preserveUnrenderedPackageMetadata(
  parsed: PackageSpec,
  structured: PackageSpec,
  changes: readonly ArtifactChange[],
): PackageSpec {
  const structuredObjects = new Map(structured.objects.map((object) => [object.key, object]));
  const structuredOperations = new Map(structured.operations.map((operation) => [operation.key, operation]));
  const structuredScripts = new Map((structured.scripts ?? []).map((script) => [script.key, script]));
  const changedPaths = new Set(changes.map((change) => change.path));
  const objectIndexChanged = changedPaths.has(`${PACKAGE_PREFIX}objects/index.md`);
  const objectMappingsChanged = changedPaths.has(`${PACKAGE_PREFIX}mappings/object-data-mappings.json`);
  const sourceArtifactsChangedFor = (key: string) => objectIndexChanged
    || objectMappingsChanged
    || changedPaths.has(`${PACKAGE_PREFIX}objects/${key}.md`);
  const parsedObjectKeys = new Set(parsed.objects.map((object) => object.key));
  const objects = parsed.objects.map((object) => {
    const previous = structuredObjects.get(object.key);
    if (!previous) return object;
    const sourcesUnchanged = JSON.stringify(sourceLabelsFor(object))
      === JSON.stringify(sourceLabelsFor(previous));
    if (!sourceArtifactsChangedFor(object.key)) return previous;
    return {
      ...object,
      ...(previous.candidateId ? { candidateId: previous.candidateId } : {}),
      ...(sourcesUnchanged ? {
        sourceRefs: previous.sourceRefs,
        sourceTable: previous.sourceTable,
        primaryKey: previous.primaryKey,
      } : {}),
      fields: object.fields.map((field) => {
        const previousField = previous.fields.find((item) => item.key === field.key);
        return sourcesUnchanged && previousField?.sourceColumn
          ? { ...field, sourceColumn: previousField.sourceColumn }
          : field;
      }),
    };
  });
  for (const previous of structured.objects) {
    if (!parsedObjectKeys.has(previous.key) && !sourceArtifactsChangedFor(previous.key)) {
      objects.push(previous);
    }
  }
  return {
    ...parsed,
    ...(structured.dataSourceType ? { dataSourceType: structured.dataSourceType } : {}),
    ...(structured.candidateDispositions ? { candidateDispositions: structured.candidateDispositions } : {}),
    objects,
    operations: parsed.operations.map((operation) => {
      const previous = structuredOperations.get(operation.key);
      return previous?.competencyQuestionId
        && previous.competencyQuestion === operation.competencyQuestion
        ? { ...operation, competencyQuestionId: previous.competencyQuestionId }
        : operation;
    }),
    ...(parsed.scripts ? {
      scripts: parsed.scripts.map((script) => {
        const previous = structuredScripts.get(script.key);
        if (!previous) return script;
        return {
          ...script,
          parameters: script.parameters.map((parameter) => {
            const previousParameter = previous.parameters.find((item) => (
              item.name === parameter.name && item.flag === parameter.flag
            ));
            return previousParameter?.multiple
              ? { ...parameter, multiple: true }
              : parameter;
          }),
        };
      }),
    } : {}),
  };
}

function diffCandidateSnapshots(base: ArtifactSnapshot, target: ArtifactSnapshot): ArtifactFileDiff[] {
  const before = new Map(base.files.map((file) => [file.path, file.content]));
  const after = new Map(target.files.map((file) => [file.path, file.content]));
  const paths = [...new Set([...before.keys(), ...after.keys()])].sort(compareNames);
  const diff: ArtifactFileDiff[] = [];
  for (const path of paths) {
    const previous = before.get(path);
    const next = after.get(path);
    if (previous !== undefined && next !== undefined && bytesEqual(previous, next)) continue;
    if (previous === undefined && next !== undefined) {
      diff.push({ path, kind: 'added', after: new Uint8Array(next) });
    } else if (previous !== undefined && next === undefined) {
      diff.push({ path, kind: 'deleted', before: new Uint8Array(previous) });
    } else if (previous !== undefined && next !== undefined) {
      diff.push({
        path,
        kind: 'modified',
        before: new Uint8Array(previous),
        after: new Uint8Array(next),
      });
    }
  }
  return diff;
}

function serializeCandidateGroups(groups: readonly PackageCandidateChangeGroup[]): string {
  const stored: StoredCandidateChangeGroup[] = groups.map((group) => ({
    id: group.id,
    summary: group.summary,
    dependsOn: [...group.dependsOn],
    validation: {
      errors: [...group.validation.errors],
      warnings: [...group.validation.warnings],
    },
    changes: group.changes.map((change) => change.kind === 'delete'
      ? { kind: 'delete', path: change.path }
      : {
        kind: 'write',
        path: change.path,
        contentBase64: Buffer.from(
          typeof change.content === 'string' ? new TextEncoder().encode(change.content) : change.content,
        ).toString('base64'),
      }),
  }));
  return JSON.stringify(stored);
}

function deserializeCandidateGroups(value: unknown): PackageCandidateChangeGroup[] {
  try {
    const stored = JSON.parse(String(value ?? '')) as StoredCandidateChangeGroup[];
    if (!Array.isArray(stored) || stored.length < 1 || stored.length > MAX_CANDIDATE_GROUPS) throw new Error();
    const ids = new Set<string>();
    const paths = new Set<string>();
    const groups = stored.map((group) => {
      if (!group || !GROUP_ID_PATTERN.test(group.id) || ids.has(group.id) || !Array.isArray(group.changes)) {
        throw new Error();
      }
      ids.add(group.id);
      const changes = group.changes.map((change): ArtifactChange => {
        const path = validateReadablePath(change.path);
        if (paths.has(path)) throw new Error();
        paths.add(path);
        if (change.kind === 'delete') return { kind: 'delete', path };
        if (change.kind !== 'write' || typeof change.contentBase64 !== 'string') throw new Error();
        return { kind: 'write', path, content: new Uint8Array(Buffer.from(change.contentBase64, 'base64')) };
      });
      return {
        id: group.id,
        summary: requiredText(group.summary, '变更组说明', 500),
        dependsOn: normalizeCandidateDependencies(group.dependsOn, group.id),
        changes,
        validation: {
          errors: normalizeCandidateMessages(group.validation?.errors, '校验错误'),
          warnings: normalizeCandidateMessages(group.validation?.warnings, '校验警告'),
        },
      };
    });
    for (const group of groups) {
      if (group.dependsOn.some((dependency) => !ids.has(dependency))) throw new Error();
    }
    assertCandidateDependencyGraph(groups);
    return groups;
  } catch (error) {
    if (error instanceof PackageArtifactStoreError && error.code === 'invalid_package') throw error;
    throw new PackageArtifactStoreError('invalid_package', '候选修订内容不正确');
  }
}

function candidateFromRow(row: any): PackageCandidateRevision {
  const status = String(row.status ?? '') as PackageCandidateRevisionStatus;
  const actorKind = String(row.actor_kind ?? '') as ArtifactRevisionActorKind;
  if (!CANDIDATE_STATUSES.has(status) || !ACTOR_KINDS.has(actorKind)) {
    throw new PackageArtifactStoreError('invalid_package', '候选修订内容不正确');
  }
  let selectedGroupIds: string[];
  try {
    const selected = JSON.parse(String(row.selected_group_ids_json ?? '[]'));
    if (!Array.isArray(selected) || selected.some((id) => typeof id !== 'string')) throw new Error();
    selectedGroupIds = selected;
  } catch {
    throw new PackageArtifactStoreError('invalid_package', '候选修订内容不正确');
  }
  let decision: PackageCandidateDecision | undefined;
  if (row.decided_at) {
    const decisionActorKind = String(row.decision_actor_kind ?? '') as ArtifactRevisionActorKind;
    if (!ACTOR_KINDS.has(decisionActorKind)) {
      throw new PackageArtifactStoreError('invalid_package', '候选修订处理记录不正确');
    }
    decision = {
      cause: requiredText(row.decision_cause, '处理原因', 64),
      summary: requiredText(row.decision_summary, '处理说明', 500),
      actorKind: decisionActorKind,
      actorName: requiredText(row.decision_actor_name, '处理人', 100),
      ...(row.decision_note ? { note: String(row.decision_note) } : {}),
      decidedAt: String(row.decided_at),
    };
  }
  return {
    id: String(row.id),
    projectId: String(row.project_id),
    baseRevision: String(row.base_revision_oid),
    status,
    cause: String(row.cause),
    summary: String(row.summary),
    actorKind,
    actorName: String(row.actor_name),
    groups: deserializeCandidateGroups(row.groups_json),
    selectedGroupIds,
    ...(row.accepted_revision_oid ? { acceptedRevision: String(row.accepted_revision_oid) } : {}),
    ...(decision ? { decision } : {}),
    createdAt: String(row.created_at),
  };
}

function cloneArtifactChange(change: ArtifactChange): ArtifactChange {
  return change.kind === 'delete'
    ? { kind: 'delete', path: change.path }
    : {
      kind: 'write',
      path: change.path,
      content: typeof change.content === 'string' ? change.content : new Uint8Array(change.content),
    };
}

function cloneCandidate(candidate: PackageCandidateRevision): PackageCandidateRevision {
  return {
    ...candidate,
    groups: candidate.groups.map((group) => ({
      ...group,
      dependsOn: [...group.dependsOn],
      changes: group.changes.map(cloneArtifactChange),
      validation: {
        errors: [...group.validation.errors],
        warnings: [...group.validation.warnings],
      },
    })),
    selectedGroupIds: [...candidate.selectedGroupIds],
    ...(candidate.decision ? { decision: { ...candidate.decision } } : {}),
  };
}

function assertExpectedRevision(expectedRevision: string, actualRevision: string): void {
  const expected = requiredText(expectedRevision, '基线版本', 200);
  if (expected !== actualRevision) {
    throw new ArtifactRepositoryError(
      'revision_conflict',
      '当前确认版本已经变化，请刷新后重试',
      { expectedRevision: expected, actualRevision },
    );
  }
}

function assertPendingCandidate(candidate: PackageCandidateRevision): void {
  if (candidate.status !== 'pending') {
    throw new PackageArtifactStoreError('candidate_not_pending', '候选修订已经处理');
  }
}

function validateAction(action: PackageRevisionAction): NormalizedAction {
  if (!action || !CAUSE_PATTERN.test(action.cause)) {
    throw new PackageArtifactStoreError('invalid_input', '修改原因不正确');
  }
  if (!ACTOR_KINDS.has(action.actorKind)) {
    throw new PackageArtifactStoreError('invalid_input', '操作人类型不支持');
  }
  const createdAt = action.createdAt;
  if (createdAt !== undefined && (!(createdAt instanceof Date) || !Number.isFinite(createdAt.getTime()))) {
    throw new PackageArtifactStoreError('invalid_input', '修改时间不正确');
  }
  return {
    ...action,
    summary: requiredText(action.summary, '修改说明', 500),
    actorName: requiredText(action.actorName, '操作人', 100),
    actorEmail: optionalText(action.actorEmail, '邮箱', 200) ?? undefined,
  };
}

function gitMetadata(action: NormalizedAction) {
  return {
    message: action.summary,
    ...(action.actorEmail ? { author: { name: action.actorName, email: action.actorEmail } } : {}),
    ...(action.createdAt ? { createdAt: action.createdAt } : {}),
  };
}

function packageFiles(
  spec: PackageSpec,
  coverage: CoverageResult,
  includeDraftTemplate = false,
): Map<string, Uint8Array> {
  const normalizedSpec = canonicalizePackageSpec(spec);
  const encoder = new TextEncoder();
  const files = new Map<string, Uint8Array>([
    [SPEC_PATH, encoder.encode(`${JSON.stringify(normalizedSpec, null, 2)}\n`)],
    [COVERAGE_PATH, encoder.encode(`${JSON.stringify(coverage, null, 2)}\n`)],
  ]);
  for (const [path, content] of Object.entries(renderPackage(normalizedSpec, { includeDraftTemplate }))) {
    files.set(`${PACKAGE_PREFIX}${path}`, encoder.encode(content));
  }
  return files;
}

/**
 * 人工草稿只覆盖由本次结构化规格变化影响的标准文件。当前修订中已经存在的
 * 流程图、导入附件、扩展目录，以及没有被本次规格变化触及的受控人工修订都保留。
 */
function draftPackageFiles(
  current: ArtifactSnapshot,
  currentView: PackageArtifactView,
  spec: PackageSpec,
  coverage: CoverageResult,
): Map<string, Uint8Array> {
  const previousCanonical = packageFiles(currentView.spec, currentView.coverage);
  const target = packageFiles(spec, coverage);
  preserveImportedExtras(current.files, target);

  for (const file of current.files) {
    const previous = previousCanonical.get(file.path);
    const next = target.get(file.path);
    if (previous && next && bytesEqual(previous, next)) {
      target.set(file.path, new Uint8Array(file.content));
    }
  }
  return target;
}

function importedPackageFiles(
  spec: PackageSpec,
  coverage: CoverageResult,
  imported: Readonly<Record<string, string | Uint8Array>>,
): Map<string, Uint8Array> {
  const normalizedSpec = canonicalizePackageSpec(spec);
  const manifest = imported?.['manifest.yaml'];
  if (!imported || typeof imported !== 'object'
    || (typeof manifest !== 'string' && !(manifest instanceof Uint8Array))) {
    throw new PackageArtifactStoreError('invalid_package', '知识包缺少 manifest.yaml');
  }
  if (manifest instanceof Uint8Array) decodeText(manifest);
  const encoder = new TextEncoder();
  const files = new Map<string, Uint8Array>([
    [SPEC_PATH, encoder.encode(`${JSON.stringify(normalizedSpec, null, 2)}\n`)],
    [COVERAGE_PATH, encoder.encode(`${JSON.stringify(coverage, null, 2)}\n`)],
  ]);
  for (const [relativePath, content] of Object.entries(imported)) {
    if (typeof content !== 'string' && !(content instanceof Uint8Array)) {
      throw new PackageArtifactStoreError('invalid_package', `知识包文件内容不正确：${relativePath}`);
    }
    const path = validatePackagePath(`${PACKAGE_PREFIX}${relativePath}`);
    files.set(path, typeof content === 'string' ? encoder.encode(content) : new Uint8Array(content));
  }
  return files;
}

/** 规格化更新保留导入包的附件和扩展目录，同时允许删除旧规格生成的详情文件。 */
function preserveImportedExtras(
  current: readonly ArtifactFile[],
  target: Map<string, Uint8Array>,
): void {
  const previousSpecFile = current.find((file) => file.path === SPEC_PATH);
  if (!previousSpecFile) return;

  let previousManaged: ReadonlySet<string>;
  try {
    const previousSpec = parseSpec(decodeText(previousSpecFile.content));
    previousManaged = new Set(
      Object.keys(renderPackage(previousSpec)).map((path) => `${PACKAGE_PREFIX}${path}`),
    );
  } catch {
    // 无法可靠恢复旧规格的管理边界时保守保留，避免升级校验器后静默删除导入资产。
    for (const file of current) {
      if (file.path.startsWith(PACKAGE_PREFIX) && !target.has(file.path)) {
        target.set(file.path, new Uint8Array(file.content));
      }
    }
    return;
  }

  for (const file of current) {
    if (!file.path.startsWith(PACKAGE_PREFIX)
      || previousManaged.has(file.path)
      || target.has(file.path)) continue;
    target.set(file.path, new Uint8Array(file.content));
  }
}

function preserveUnchangedManagedFiles(
  current: ArtifactSnapshot,
  target: Map<string, Uint8Array>,
  currentView: PackageArtifactView,
): void {
  const canonicalCurrent = packageFiles(currentView.spec, currentView.coverage);
  const actual = new Map(current.files.map((file) => [file.path, file.content]));
  const paths = new Set([...canonicalCurrent.keys(), ...target.keys()]);
  for (const path of paths) {
    const before = canonicalCurrent.get(path);
    const after = target.get(path);
    if (before === undefined || after === undefined || !bytesEqual(before, after)) continue;
    const existing = actual.get(path);
    if (existing) target.set(path, new Uint8Array(existing));
  }
  preserveImportedExtras(current.files, target);
}

function displayManagedPath(path: string): string {
  return path.startsWith(PACKAGE_PREFIX) ? path.slice(PACKAGE_PREFIX.length) : path;
}

function metadataChanges(current: readonly ArtifactFile[], spec: PackageSpec): ArtifactChange[] {
  const manifest = current.find((file) => file.path === `${PACKAGE_PREFIX}manifest.yaml`);
  if (!manifest) throw new PackageArtifactStoreError('invalid_package', '知识包缺少 manifest.yaml');
  const encoder = new TextEncoder();
  const normalizedSpec = canonicalizePackageSpec(spec);
  const rendered = renderPackage(normalizedSpec, { validateScripts: false });
  const mappingPath = `${PACKAGE_PREFIX}mappings/object-data-mappings.json`;
  const mapping = rendered['mappings/object-data-mappings.json'];
  const changes: ArtifactChange[] = [
    { kind: 'write', path: SPEC_PATH, content: encoder.encode(`${JSON.stringify(normalizedSpec, null, 2)}\n`) },
    {
      kind: 'write',
      path: `${PACKAGE_PREFIX}manifest.yaml`,
      content: encoder.encode(updateManifestMetadata(decodeText(manifest.content), normalizedSpec)),
    },
  ];
  if (mapping !== undefined) {
    changes.push({ kind: 'write', path: mappingPath, content: encoder.encode(mapping) });
  } else if (current.some((file) => file.path === mappingPath)) {
    changes.push({ kind: 'delete', path: mappingPath });
  }
  return changes;
}

function updateManifestMetadata(content: string, spec: PackageSpec): string {
  try {
    return updateManifestPlatformMetadata(content, spec);
  } catch (cause) {
    throw new PackageArtifactStoreError(
      'invalid_package',
      cause instanceof Error ? cause.message : 'manifest.yaml 元数据不正确',
    );
  }
}

function validateChangedCandidateScripts(
  currentSpec: PackageSpec,
  candidateSpec: PackageSpec,
  target: ArtifactSnapshot,
  changes: readonly ArtifactChange[],
): void {
  const changedPaths = new Set(changes.map((change) => change.path));
  const currentByKey = new Map((currentSpec.scripts ?? []).map((script) => [script.key, script]));
  const targetFiles = new Map(target.files.map((file) => [file.path, file.content]));
  const validatedEntrypoints = new Set<string>();

  try {
    for (const script of candidateSpec.scripts ?? []) {
      const sourcePath = `${PACKAGE_PREFIX}scripts/${script.entrypoint}`;
      const definitionPath = `${PACKAGE_PREFIX}scripts/${script.key}.md`;
      const previous = currentByKey.get(script.key);
      const definitionChanged = previous === undefined
        || JSON.stringify(previous) !== JSON.stringify(script)
        || changedPaths.has(definitionPath);
      if (!definitionChanged && !changedPaths.has(sourcePath)) continue;

      const source = changedPaths.has(sourcePath) ? targetFiles.get(sourcePath) : undefined;
      validateGeneratedScriptSafety({
        ...script,
        ...(source ? { sourceCode: decodeText(source) } : {}),
      });
      validatedEntrypoints.add(sourcePath);
    }

    // Imported packages can contain executable attachments not represented in package-spec.json.
    // Once a candidate changes one, it becomes new work and must meet the same execution contract.
    for (const path of changedPaths) {
      if (!path.startsWith(`${PACKAGE_PREFIX}scripts/`)
        || !path.endsWith('.py')
        || validatedEntrypoints.has(path)) continue;
      const source = targetFiles.get(path);
      if (!source) continue;
      const entrypoint = path.slice(`${PACKAGE_PREFIX}scripts/`.length);
      validateGeneratedScriptSafety({
        key: entrypoint.slice(0, -3),
        title: entrypoint,
        description: '候选脚本',
        boundOperation: '',
        runtime: 'python',
        entrypoint,
        timeoutSeconds: 300,
        parameters: [],
        outputs: [],
        boundaries: [],
        sourceCode: decodeText(source),
      });
    }
  } catch (error) {
    throw new PackageArtifactStoreError(
      'candidate_validation_failed',
      error instanceof Error ? error.message : '候选脚本校验未通过',
    );
  }
}

function changesForManagedFiles(current: readonly ArtifactFile[], target: ReadonlyMap<string, Uint8Array>): ArtifactChange[] {
  const currentFiles = new Map(current.map((file) => [file.path, file.content]));
  const managedPaths = current
    .map((file) => file.path)
    .filter((path) => path === SPEC_PATH || path === COVERAGE_PATH || path.startsWith(PACKAGE_PREFIX));
  const paths = [...new Set([...managedPaths, ...target.keys()])].sort(compareNames);
  const changes: ArtifactChange[] = [];
  for (const path of paths) {
    const before = currentFiles.get(path);
    const after = target.get(path);
    if (after === undefined) changes.push({ kind: 'delete', path });
    else if (before === undefined || !bytesEqual(before, after)) changes.push({ kind: 'write', path, content: after });
  }
  return changes;
}

function parseSpec(value: string): PackageSpec {
  try {
    const spec = canonicalizePackageSpec(JSON.parse(value) as PackageSpec);
    validatePackageSpec(spec);
    return spec;
  } catch (error) {
    if (error instanceof PackageArtifactStoreError) throw error;
    throw new PackageArtifactStoreError('invalid_package', '知识包内容不正确');
  }
}

function parseCoverage(value: string | null | undefined): CoverageResult {
  if (!value) return { covered: [], uncovered: [] };
  try {
    const coverage = JSON.parse(value) as CoverageResult;
    validateCoverage(coverage);
    return coverage;
  } catch (error) {
    if (error instanceof PackageArtifactStoreError) throw error;
    throw new PackageArtifactStoreError('invalid_package', '覆盖结果不正确');
  }
}

function validatePackageSpec(value: PackageSpec): void {
  if (
    !value
    || typeof value !== 'object'
    || typeof value.id !== 'string'
    || typeof value.version !== 'string'
    || !Array.isArray(value.objects)
    || !Array.isArray(value.operations)
  ) {
    throw new PackageArtifactStoreError('invalid_package', '知识包内容不正确');
  }
}

function validateCoverage(value: CoverageResult): void {
  if (!value || typeof value !== 'object' || !Array.isArray(value.covered) || !Array.isArray(value.uncovered)) {
    throw new PackageArtifactStoreError('invalid_package', '覆盖结果不正确');
  }
}

function validateReadablePath(value: string): string {
  if (value === SPEC_PATH || value === COVERAGE_PATH) return value;
  return validatePackagePath(value);
}

function validatePackagePath(value: string): string {
  if (
    typeof value !== 'string'
    || !value.startsWith(PACKAGE_PREFIX)
    || value.length <= PACKAGE_PREFIX.length
    || value.length > 1024
    || /[\u0000-\u001f\u007f]/.test(value)
    || value.includes('\\')
    || value.includes('//')
    || value.split('/').some((part) => part === '' || part === '.' || part === '..' || part.toLowerCase() === '.git')
  ) {
    throw new PackageArtifactStoreError('invalid_input', '文件路径不正确');
  }
  return value;
}

function decodeText(content: Uint8Array): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(content);
  } catch {
    throw new PackageArtifactStoreError('invalid_package', '文件内容不是文本');
  }
}

function tryDecodeText(content: Uint8Array): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(content);
  } catch {
    return null;
  }
}

function requiredText(value: string, label: string, maxLength: number): string {
  const text = String(value ?? '').trim();
  if (!text) throw new PackageArtifactStoreError('invalid_input', `${label}不能为空`);
  if (text.length > maxLength || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text)) {
    throw new PackageArtifactStoreError('invalid_input', `${label}不正确`);
  }
  return text;
}

function optionalText(value: unknown, label: string, maxLength: number): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string') throw new PackageArtifactStoreError('invalid_input', `${label}不正确`);
  const text = value.trim();
  if (!text) return null;
  if (text.length > maxLength || /[\u0000-\u001f\u007f]/.test(text)) {
    throw new PackageArtifactStoreError('invalid_input', `${label}不正确`);
  }
  return text;
}

function optionalDate(value: unknown): Date | undefined {
  if (typeof value !== 'string' || !value) return undefined;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : undefined;
}

function bytesEqual(left: Uint8Array, right: Uint8Array): boolean {
  return left.byteLength === right.byteLength && left.every((byte, index) => byte === right[index]);
}

function compareNames(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left), Buffer.from(right));
}

function isWithinRecoveryGrace(createdAt: string): boolean {
  const timestamp = Date.parse(createdAt);
  return Number.isFinite(timestamp) && Date.now() - timestamp < INTERRUPTED_COMMIT_GRACE_MS;
}
