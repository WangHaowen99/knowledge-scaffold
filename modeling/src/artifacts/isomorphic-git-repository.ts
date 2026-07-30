import fs from 'node:fs';
import { lstat, mkdir, readdir, rm, writeFile } from 'node:fs/promises';
import { basename, dirname, posix, resolve, sep } from 'node:path';
import git, { type ReadCommitResult, type TreeEntry } from 'isomorphic-git';

import {
  ArtifactRepositoryError,
  type ArtifactAuthor,
  type ArtifactChange,
  type ArtifactCommitMetadata,
  type ArtifactDiff,
  type ArtifactFile,
  type ArtifactFileDiff,
  type ArtifactRepository,
  type ArtifactRevision,
  type ArtifactSnapshot,
} from './repository.js';

const DEFAULT_AUTHOR: ArtifactAuthor = {
  name: 'Knowledge Modeling',
  email: 'modeling@localhost',
};

const OID_PATTERN = /^[0-9a-f]{40}$/;
const PROJECT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const PROJECT_QUEUES = new Map<string, Promise<void>>();

export interface IsomorphicGitArtifactRepositoryOptions {
  rootDir: string;
  author?: ArtifactAuthor;
  now?: () => Date;
}

interface TreeNode {
  files: Map<string, Uint8Array>;
  directories: Map<string, TreeNode>;
}

export class IsomorphicGitArtifactRepository implements ArtifactRepository {
  private readonly rootDir: string;
  private readonly author: ArtifactAuthor;
  private readonly now: () => Date;

  constructor(options: IsomorphicGitArtifactRepositoryOptions) {
    this.rootDir = resolve(options.rootDir);
    this.author = validateAuthor(options.author ?? DEFAULT_AUTHOR);
    this.now = options.now ?? (() => new Date());
  }

  async initialize(projectId: string): Promise<ArtifactRevision> {
    return this.runExclusive(projectId, async () => {
      const dir = await this.prepareProjectDirectory(projectId);
      const gitPath = resolve(dir, '.git');

      if (await pathExists(gitPath)) {
        await assertDirectory(gitPath, '版本目录不正确');
        try {
          return await this.readCurrent(dir);
        } catch (error) {
          if (!(error instanceof ArtifactRepositoryError) || error.code !== 'not_initialized') throw error;
        }
      } else {
        const entries = await readdir(dir);
        if (entries.length > 0) {
          throw new ArtifactRepositoryError('workspace_not_empty', '项目目录已有文件');
        }
        await git.init({ fs, dir, defaultBranch: 'main' });
      }

      const tree = await git.writeTree({ fs, dir, tree: [] });
      const createdAt = this.now();
      const oid = await git.commit({
        fs,
        dir,
        ref: 'refs/heads/main',
        parent: [],
        tree,
        message: '创建项目',
        author: gitAuthor(this.author, createdAt),
      });
      return this.readRevision(dir, oid);
    });
  }

  async current(projectId: string): Promise<ArtifactRevision> {
    return this.runExclusive(projectId, async () => this.readCurrent(this.existingProjectDirectory(projectId)));
  }

  async snapshot(projectId: string, revision?: string): Promise<ArtifactSnapshot> {
    return this.runExclusive(projectId, async () => {
      const dir = this.existingProjectDirectory(projectId);
      await this.assertInitialized(dir);
      const oid = revision === undefined ? await this.resolveHead(dir) : validateRevisionId(revision);
      return this.readSnapshot(dir, oid);
    });
  }

  async commit(
    projectId: string,
    expectedRevision: string,
    changes: readonly ArtifactChange[],
    metadata: ArtifactCommitMetadata,
  ): Promise<ArtifactRevision> {
    return this.runExclusive(projectId, async () => {
      const dir = this.existingProjectDirectory(projectId);
      await this.assertInitialized(dir);
      const currentOid = await this.resolveHead(dir);
      assertExpectedRevision(expectedRevision, currentOid);
      const current = await this.readSnapshot(dir, currentOid);
      const targetFiles = applyChanges(current.files, changes);

      if (sameFiles(current.files, targetFiles)) {
        throw new ArtifactRepositoryError('no_change', '内容没有变化');
      }

      return this.createRevision(dir, currentOid, current.files, targetFiles, validateMetadata(metadata, this.author, this.now));
    });
  }

  async rollbackCommit(projectId: string, expectedRevision: string): Promise<ArtifactRevision> {
    return this.runExclusive(projectId, async () => {
      const dir = this.existingProjectDirectory(projectId);
      await this.assertInitialized(dir);
      const currentOid = await this.resolveHead(dir);
      assertExpectedRevision(expectedRevision, currentOid);
      const current = await this.readSnapshot(dir, currentOid);
      const parentOid = current.revision.parentOid;
      if (!parentOid) {
        throw new ArtifactRepositoryError('invalid_input', '初始修订不能回滚');
      }
      const parent = await this.readSnapshot(dir, parentOid);
      const branch = await git.currentBranch({ fs, dir, fullname: true });
      if (!branch) throw new ArtifactRepositoryError('invalid_repository', '版本库状态不正确');

      await this.materialize(dir, parent.files, current.files);
      try {
        await git.writeRef({ fs, dir, ref: branch, value: parentOid, force: true });
      } catch (error) {
        await this.materialize(dir, current.files).catch(() => undefined);
        throw error;
      }
      return parent.revision;
    });
  }

  async history(projectId: string, limit = 50): Promise<ArtifactRevision[]> {
    return this.runExclusive(projectId, async () => {
      const dir = this.existingProjectDirectory(projectId);
      await this.assertInitialized(dir);
      if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
        throw new ArtifactRepositoryError('invalid_input', '版本数量不正确');
      }
      const commits = await git.log({ fs, dir, ref: 'HEAD', depth: limit });
      return commits.map(toRevision);
    });
  }

  async diff(projectId: string, fromRevision: string, toRevision: string): Promise<ArtifactDiff> {
    return this.runExclusive(projectId, async () => {
      const dir = this.existingProjectDirectory(projectId);
      await this.assertInitialized(dir);
      const from = await this.readSnapshot(dir, validateRevisionId(fromRevision));
      const to = await this.readSnapshot(dir, validateRevisionId(toRevision));
      const before = new Map(from.files.map((file) => [file.path, file.content]));
      const after = new Map(to.files.map((file) => [file.path, file.content]));
      const paths = [...new Set([...before.keys(), ...after.keys()])].sort(compareNames);
      const files: ArtifactFileDiff[] = [];

      for (const path of paths) {
        const oldContent = before.get(path);
        const newContent = after.get(path);
        if (oldContent && newContent && bytesEqual(oldContent, newContent)) continue;
        if (oldContent === undefined) files.push({ path, kind: 'added', after: cloneBytes(newContent!) });
        else if (newContent === undefined) files.push({ path, kind: 'deleted', before: cloneBytes(oldContent) });
        else files.push({ path, kind: 'modified', before: cloneBytes(oldContent), after: cloneBytes(newContent) });
      }

      return { from: from.revision, to: to.revision, files };
    });
  }

  async restore(
    projectId: string,
    targetRevision: string,
    expectedRevision: string,
    metadata?: ArtifactCommitMetadata,
  ): Promise<ArtifactRevision> {
    return this.runExclusive(projectId, async () => {
      const dir = this.existingProjectDirectory(projectId);
      await this.assertInitialized(dir);
      const currentOid = await this.resolveHead(dir);
      assertExpectedRevision(expectedRevision, currentOid);
      const target = await this.readSnapshot(dir, validateRevisionId(targetRevision));
      const current = await this.readSnapshot(dir, currentOid);
      const commitMetadata = validateMetadata(metadata ?? {
        message: `恢复到 ${target.revision.oid.slice(0, 8)}`,
      }, this.author, this.now);

      return this.createRevision(dir, currentOid, current.files, target.files, commitMetadata, true);
    });
  }

  private async createRevision(
    dir: string,
    parentOid: string,
    currentFiles: readonly ArtifactFile[],
    targetFiles: readonly ArtifactFile[],
    metadata: Required<ArtifactCommitMetadata>,
    allowEmpty = false,
  ): Promise<ArtifactRevision> {
    if (!allowEmpty && sameFiles(currentFiles, targetFiles)) {
      throw new ArtifactRepositoryError('no_change', '内容没有变化');
    }

    const tree = await this.writeTree(dir, targetFiles);
    await this.materialize(dir, targetFiles, currentFiles);
    try {
      const branch = await git.currentBranch({ fs, dir, fullname: true });
      if (!branch) throw new ArtifactRepositoryError('invalid_repository', '版本库状态不正确');
      const oid = await git.commit({
        fs,
        dir,
        ref: branch,
        parent: [parentOid],
        tree,
        message: metadata.message,
        author: gitAuthor(metadata.author, metadata.createdAt),
      });
      return this.readRevision(dir, oid);
    } catch (error) {
      await this.materialize(dir, currentFiles).catch(() => undefined);
      throw error;
    }
  }

  private async writeTree(dir: string, files: readonly ArtifactFile[]): Promise<string> {
    const root: TreeNode = { files: new Map(), directories: new Map() };
    for (const file of files) {
      const parts = file.path.split('/');
      const filename = parts.pop()!;
      let node = root;
      for (const part of parts) {
        let child = node.directories.get(part);
        if (!child) {
          child = { files: new Map(), directories: new Map() };
          node.directories.set(part, child);
        }
        node = child;
      }
      node.files.set(filename, file.content);
    }

    const writeNode = async (node: TreeNode): Promise<string> => {
      const entries: TreeEntry[] = [];
      for (const [path, content] of node.files) {
        const oid = await git.writeBlob({ fs, dir, blob: content });
        entries.push({ mode: '100644', path, oid, type: 'blob' });
      }
      for (const [path, child] of node.directories) {
        const oid = await writeNode(child);
        entries.push({ mode: '040000', path, oid, type: 'tree' });
      }
      entries.sort((left, right) => compareNames(
        left.type === 'tree' ? `${left.path}/` : left.path,
        right.type === 'tree' ? `${right.path}/` : right.path,
      ));
      return git.writeTree({ fs, dir, tree: entries });
    };

    return writeNode(root);
  }

  private async materialize(
    dir: string,
    files: readonly ArtifactFile[],
    rollbackFiles?: readonly ArtifactFile[],
  ): Promise<void> {
    try {
      const entries = await readdir(dir);
      await Promise.all(entries
        .filter((entry) => entry !== '.git')
        .map((entry) => rm(resolve(dir, entry), { recursive: true, force: true })));

      for (const file of files) {
        const target = resolveArtifactPath(dir, file.path);
        await mkdir(dirname(target), { recursive: true });
        await writeFile(target, file.content, { mode: 0o644 });
      }
    } catch (error) {
      if (rollbackFiles) await this.materialize(dir, rollbackFiles).catch(() => undefined);
      throw error;
    }
  }

  private async readSnapshot(dir: string, oid: string): Promise<ArtifactSnapshot> {
    const revision = await this.readRevision(dir, oid);
    let paths: string[];
    try {
      paths = await git.listFiles({ fs, dir, ref: oid });
    } catch (error) {
      throw mapRevisionError(error, oid);
    }

    const files: ArtifactFile[] = [];
    for (const path of paths.sort(compareNames)) {
      validateArtifactPath(path);
      try {
        const result = await git.readBlob({ fs, dir, oid, filepath: path });
        files.push({ path, content: cloneBytes(result.blob) });
      } catch (error) {
        throw mapRevisionError(error, oid);
      }
    }
    return { revision, files };
  }

  private async readCurrent(dir: string): Promise<ArtifactRevision> {
    await this.assertInitialized(dir);
    return this.readRevision(dir, await this.resolveHead(dir));
  }

  private async readRevision(dir: string, oid: string): Promise<ArtifactRevision> {
    try {
      return toRevision(await git.readCommit({ fs, dir, oid }));
    } catch (error) {
      throw mapRevisionError(error, oid);
    }
  }

  private async resolveHead(dir: string): Promise<string> {
    try {
      return await git.resolveRef({ fs, dir, ref: 'HEAD' });
    } catch (error) {
      if (gitErrorCode(error) === 'NotFoundError') {
        throw new ArtifactRepositoryError('not_initialized', '项目还没有版本记录');
      }
      throw error;
    }
  }

  private async assertInitialized(dir: string): Promise<void> {
    if (!(await pathExists(dir))) {
      throw new ArtifactRepositoryError('not_initialized', '项目还没有版本记录');
    }
    await assertDirectory(dir, '项目目录不正确');
    const gitPath = resolve(dir, '.git');
    if (!(await pathExists(gitPath))) {
      throw new ArtifactRepositoryError('not_initialized', '项目还没有版本记录');
    }
    await assertDirectory(gitPath, '版本目录不正确');
  }

  private existingProjectDirectory(projectId: string): string {
    const dir = projectDirectory(this.rootDir, projectId);
    return dir;
  }

  private async prepareProjectDirectory(projectId: string): Promise<string> {
    const dir = projectDirectory(this.rootDir, projectId);
    await mkdir(this.rootDir, { recursive: true });
    if (await pathExists(dir)) await assertDirectory(dir, '项目目录不正确');
    else await mkdir(dir);
    return dir;
  }

  private async runExclusive<T>(projectId: string, task: () => Promise<T>): Promise<T> {
    validateProjectId(projectId);
    const key = projectDirectory(this.rootDir, projectId);
    const previous = PROJECT_QUEUES.get(key) ?? Promise.resolve();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolveGate) => { release = resolveGate; });
    const tail = previous.then(() => gate);
    PROJECT_QUEUES.set(key, tail);

    await previous;
    try {
      return await task();
    } finally {
      release();
      if (PROJECT_QUEUES.get(key) === tail) PROJECT_QUEUES.delete(key);
    }
  }
}

function applyChanges(currentFiles: readonly ArtifactFile[], changes: readonly ArtifactChange[]): ArtifactFile[] {
  if (changes.length === 0) throw new ArtifactRepositoryError('empty_change', '没有要保存的改动');
  const files = new Map(currentFiles.map((file) => [file.path, cloneBytes(file.content)]));
  const changedPaths = new Set<string>();

  for (const change of changes) {
    const path = validateArtifactPath(change.path);
    if (changedPaths.has(path)) throw new ArtifactRepositoryError('invalid_input', `文件重复：${path}`);
    changedPaths.add(path);
    if (change.kind === 'write') files.set(path, toBytes(change.content));
    else if (change.kind === 'delete') files.delete(path);
    else throw new ArtifactRepositoryError('invalid_input', '改动类型不正确');
  }

  validateFileShape(files.keys());
  return [...files].sort(([left], [right]) => compareNames(left, right))
    .map(([path, content]) => ({ path, content }));
}

function validateFileShape(paths: Iterable<string>): void {
  const seen = new Set<string>();
  for (const path of [...paths].sort(compareNames)) {
    let parent = posix.dirname(path);
    while (parent !== '.') {
      if (seen.has(parent)) throw new ArtifactRepositoryError('invalid_path', `文件路径冲突：${path}`);
      parent = posix.dirname(parent);
    }
    seen.add(path);
  }
}

function validateArtifactPath(value: string): string {
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.length > 1024
    || /[\u0000-\u001f\u007f]/.test(value)
    || value.includes('\\')
    || value.startsWith('/')
    || posix.normalize(value) !== value
  ) {
    throw new ArtifactRepositoryError('invalid_path', '文件路径不正确');
  }
  const parts = value.split('/');
  if (parts.some((part) => part === '' || part === '.' || part === '..' || part.toLowerCase() === '.git')) {
    throw new ArtifactRepositoryError('invalid_path', '文件路径不正确');
  }
  return value;
}

function resolveArtifactPath(dir: string, artifactPath: string): string {
  validateArtifactPath(artifactPath);
  const target = resolve(dir, ...artifactPath.split('/'));
  if (!target.startsWith(`${dir}${sep}`) || basename(target) === '.git') {
    throw new ArtifactRepositoryError('invalid_path', '文件路径不正确');
  }
  return target;
}

function projectDirectory(rootDir: string, projectId: string): string {
  validateProjectId(projectId);
  const dir = resolve(rootDir, projectId);
  if (!dir.startsWith(`${rootDir}${sep}`)) {
    throw new ArtifactRepositoryError('invalid_project', '项目编号格式不正确');
  }
  return dir;
}

function validateProjectId(projectId: string): void {
  if (!PROJECT_ID_PATTERN.test(projectId) || projectId === '.' || projectId === '..') {
    throw new ArtifactRepositoryError('invalid_project', '项目编号格式不正确');
  }
}

function validateRevisionId(oid: string): string {
  if (!OID_PATTERN.test(oid)) throw new ArtifactRepositoryError('revision_not_found', '找不到这个版本');
  return oid;
}

function assertExpectedRevision(expected: string, current: string): void {
  if (validateRevisionId(expected) !== current) {
    throw new ArtifactRepositoryError('revision_conflict', '内容已更新，请刷新后重试', {
      expectedRevision: expected,
      currentRevision: current,
    });
  }
}

function validateMetadata(
  metadata: ArtifactCommitMetadata,
  defaultAuthor: ArtifactAuthor,
  now: () => Date,
): Required<ArtifactCommitMetadata> {
  const message = metadata.message?.trim();
  if (!message || message.length > 500 || message.includes('\0')) {
    throw new ArtifactRepositoryError('invalid_input', '版本说明不正确');
  }
  const createdAt = metadata.createdAt ?? now();
  if (!(createdAt instanceof Date) || !Number.isFinite(createdAt.getTime())) {
    throw new ArtifactRepositoryError('invalid_input', '版本时间不正确');
  }
  return { message, author: validateAuthor(metadata.author ?? defaultAuthor), createdAt };
}

function validateAuthor(author: ArtifactAuthor): ArtifactAuthor {
  const name = author.name?.trim();
  const email = author.email?.trim();
  if (!name || !email || name.length > 100 || email.length > 200 || name.includes('\0') || email.includes('\0')) {
    throw new ArtifactRepositoryError('invalid_input', '提交人信息不正确');
  }
  return { name, email };
}

function gitAuthor(author: ArtifactAuthor, date: Date) {
  return {
    ...author,
    timestamp: Math.floor(date.getTime() / 1000),
    timezoneOffset: date.getTimezoneOffset(),
  };
}

function toRevision(result: ReadCommitResult): ArtifactRevision {
  return {
    oid: result.oid,
    parentOid: result.commit.parent[0] ?? null,
    message: result.commit.message.replace(/\n+$/, ''),
    author: { name: result.commit.author.name, email: result.commit.author.email },
    createdAt: new Date(result.commit.author.timestamp * 1000).toISOString(),
  };
}

function mapRevisionError(error: unknown, oid: string): Error {
  if (gitErrorCode(error) === 'NotFoundError') {
    return new ArtifactRepositoryError('revision_not_found', '找不到这个版本', { revision: oid });
  }
  return error instanceof Error ? error : new Error(String(error));
}

function gitErrorCode(error: unknown): string | undefined {
  return typeof error === 'object' && error !== null && 'code' in error
    ? String((error as { code?: unknown }).code)
    : undefined;
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
}

async function assertDirectory(path: string, message: string): Promise<void> {
  const stats = await lstat(path);
  if (!stats.isDirectory() || stats.isSymbolicLink()) {
    throw new ArtifactRepositoryError('invalid_repository', message);
  }
}

function sameFiles(left: readonly ArtifactFile[], right: readonly ArtifactFile[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((file, index) => file.path === right[index]?.path && bytesEqual(file.content, right[index].content));
}

function bytesEqual(left: Uint8Array, right: Uint8Array): boolean {
  return left.byteLength === right.byteLength && left.every((value, index) => value === right[index]);
}

function toBytes(content: string | Uint8Array): Uint8Array {
  return typeof content === 'string' ? new TextEncoder().encode(content) : cloneBytes(content);
}

function cloneBytes(content: Uint8Array): Uint8Array {
  return new Uint8Array(content);
}

function compareNames(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left), Buffer.from(right));
}
