import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb } from '../db/index.js';
import type { CoverageResult, PackageSpec } from '../types.js';
import { renderPackage } from '../workflow/package-render.js';
import { ArtifactRepositoryError } from './repository.js';
import type {
  ArtifactChange,
  ArtifactDiff,
  ArtifactFile,
  ArtifactRepository,
  ArtifactRevision,
  ArtifactSnapshot,
} from './repository.js';
import {
  ArtifactRevisionRepo,
  type ArtifactRevisionActorKind,
  type ArtifactRevisionRecord,
} from './revision-repo.js';

const SPEC_PATH = 'model/package-spec.json';
const COVERAGE_PATH = 'model/coverage.json';
const PACKAGE_PREFIX = 'package/';
const CAUSE_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/;
const ACTOR_KINDS = new Set<ArtifactRevisionActorKind>(['user', 'agent', 'system']);
const INTERRUPTED_COMMIT_GRACE_MS = 30_000;

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
}

export interface SaveCurrentAsPackageVersionInput {
  expectedRevision: string;
  version: string;
  status?: PackageSpec['status'];
  note?: string;
  action: PackageRevisionAction;
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
  files: Record<string, string>;
  deletePaths?: string[];
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
  | 'commit_in_progress';

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
  private readonly projectQueues = new Map<string, Promise<void>>();

  constructor(options: PackageArtifactStoreOptions) {
    this.db = options.db ?? getDb();
    this.repository = options.repository;
    this.revisions = options.revisions ?? new ArtifactRevisionRepo(this.db);
  }

  async ensureProject(projectId: string): Promise<ArtifactRevision> {
    return this.runExclusive(projectId, () => this.ensureProjectUnlocked(projectId));
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
          WHERE project_id = ? AND version = ? ORDER BY created_at DESC, rowid DESC LIMIT 1`)
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
        ORDER BY created_at DESC, rowid DESC LIMIT 1`).get(projectId, normalizedVersion) as any;
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
      for (const file of snapshot.files) {
        if (!file.path.startsWith(PACKAGE_PREFIX)) continue;
        files[file.path.slice(PACKAGE_PREFIX.length)] = decodeText(file.content);
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

  /** 在同一个 Git 修订中原子更新一组知识包文件。 */
  async saveFiles(projectId: string, input: SavePackageFilesInput): Promise<PackageArtifactView> {
    return this.runExclusive(projectId, async () => {
      const action = validateAction(input.action);
      await this.ensureProjectUnlocked(projectId);
      const entries = Object.entries(input.files);
      const deletePaths = [...new Set(input.deletePaths ?? [])];
      if ((!entries.length && !deletePaths.length) || entries.length + deletePaths.length > 100) {
        throw new PackageArtifactStoreError('invalid_input', '批量文件必须包含 1 到 100 个文件');
      }
      const current = await this.repository.snapshot(projectId);
      const currentView = this.viewFromSnapshot(current);
      const currentFiles = new Map(current.files.map((file) => [file.path, file.content]));
      const encoder = new TextEncoder();
      const writes: ArtifactChange[] = entries.map(([rawPath, content]) => {
        const path = validatePackagePath(rawPath);
        if (!path.startsWith(PACKAGE_PREFIX)) {
          throw new PackageArtifactStoreError('invalid_input', '只能批量保存知识包文件');
        }
        if (typeof content !== 'string') {
          throw new PackageArtifactStoreError('invalid_input', `文件内容不正确：${path}`);
        }
        return { kind: 'write' as const, path, content: encoder.encode(content) };
      }).filter((change) => {
        const before = currentFiles.get(change.path);
        return before === undefined || !bytesEqual(before, change.content);
      });
      const writePaths = new Set(writes.map((change) => change.path));
      const deletes: ArtifactChange[] = deletePaths.flatMap((rawPath) => {
        const path = validatePackagePath(rawPath);
        if (!path.startsWith(PACKAGE_PREFIX)) {
          throw new PackageArtifactStoreError('invalid_input', '只能批量删除知识包文件');
        }
        if (writePaths.has(path)) {
          throw new PackageArtifactStoreError('invalid_input', `同一文件不能同时写入和删除：${path}`);
        }
        return currentFiles.has(path) ? [{ kind: 'delete' as const, path }] : [];
      });
      const changes = [...writes, ...deletes].sort((left, right) => compareNames(left.path, right.path));
      if (!changes.length) return currentView;
      const revision = await this.commitAndIndex(projectId, input.expectedRevision, () => this.repository.commit(
        projectId,
        input.expectedRevision,
        changes,
        gitMetadata(action),
      ), (savedRevision) => {
        this.recordRevision(projectId, savedRevision, action, changes.map((change) => change.path));
      });
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
      this.viewFromSnapshot(target);
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
        this.recordRevision(projectId, savedRevision, action, changedPaths);
      });
      return this.viewFromSnapshot(await this.repository.snapshot(projectId, revision.oid));
    });
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
      WHERE project_id = ? ORDER BY created_at, rowid`).all(projectId) as any[];
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

  private async runExclusive<T>(projectId: string, task: () => Promise<T>): Promise<T> {
    const previous = this.projectQueues.get(projectId) ?? Promise.resolve();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolveGate) => { release = resolveGate; });
    const tail = previous.then(() => gate);
    this.projectQueues.set(projectId, tail);
    await previous;
    try {
      return await task();
    } finally {
      release();
      if (this.projectQueues.get(projectId) === tail) this.projectQueues.delete(projectId);
    }
  }
}

interface NormalizedAction extends PackageRevisionAction {
  cause: string;
  summary: string;
  actorName: string;
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

function packageFiles(spec: PackageSpec, coverage: CoverageResult): Map<string, Uint8Array> {
  const encoder = new TextEncoder();
  const files = new Map<string, Uint8Array>([
    [SPEC_PATH, encoder.encode(`${JSON.stringify(spec, null, 2)}\n`)],
    [COVERAGE_PATH, encoder.encode(`${JSON.stringify(coverage, null, 2)}\n`)],
  ]);
  for (const [path, content] of Object.entries(renderPackage(spec))) {
    files.set(`${PACKAGE_PREFIX}${path}`, encoder.encode(content));
  }
  return files;
}

function metadataChanges(current: readonly ArtifactFile[], spec: PackageSpec): ArtifactChange[] {
  const manifest = current.find((file) => file.path === `${PACKAGE_PREFIX}manifest.yaml`);
  if (!manifest) throw new PackageArtifactStoreError('invalid_package', '知识包缺少 manifest.yaml');
  const encoder = new TextEncoder();
  return [
    { kind: 'write', path: SPEC_PATH, content: encoder.encode(`${JSON.stringify(spec, null, 2)}\n`) },
    {
      kind: 'write',
      path: `${PACKAGE_PREFIX}manifest.yaml`,
      content: encoder.encode(updateManifestMetadata(decodeText(manifest.content), spec)),
    },
  ];
}

function updateManifestMetadata(content: string, spec: PackageSpec): string {
  const generated = renderPackage(spec)['manifest.yaml'];
  const metadata = new Map(
    generated.split('\n')
      .filter((line) => /^(version|status):/.test(line))
      .map((line) => [line.slice(0, line.indexOf(':')), line]),
  );
  let updated = content;
  for (const key of ['version', 'status']) {
    const replacement = metadata.get(key);
    if (!replacement) continue;
    const pattern = new RegExp(`^${key}:.*$`, 'm');
    if (!pattern.test(updated)) {
      throw new PackageArtifactStoreError('invalid_package', `manifest.yaml 缺少 ${key}`);
    }
    updated = updated.replace(pattern, replacement);
  }
  return updated;
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
    const spec = JSON.parse(value) as PackageSpec;
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
