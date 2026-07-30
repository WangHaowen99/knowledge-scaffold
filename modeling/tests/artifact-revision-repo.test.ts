import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';

const dataDir = mkdtempSync(join(tmpdir(), 'modeling-artifact-revisions-'));
const legacy = new DatabaseSync(join(dataDir, 'modeling.db'));
legacy.exec(`
  CREATE TABLE package_versions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version TEXT NOT NULL,
    spec_json TEXT NOT NULL, coverage_json TEXT, note TEXT, created_at TEXT NOT NULL
  );
`);
legacy.close();
process.env.DATA_DIR = dataDir;

const [{ createProject }, { getDb }, artifacts] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/artifacts/index.js'),
]);

const {
  ArtifactRevisionIndexError,
  ArtifactRevisionRepo,
} = artifacts;

function oid(value: number): string {
  return value.toString(16).padStart(40, '0');
}

function projectId(): string {
  return createProject({ owner: 'test', name: `版本项目-${Math.random()}` }).id;
}

test('旧数据库自动补充版本关联列', () => {
  const db = getDb();
  const columns = (table: string) => (db.prepare(`PRAGMA table_info(${table})`).all() as { name: string }[])
    .map((row) => row.name);

  assert.ok(columns('package_versions').includes('revision_oid'));
  assert.ok(db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artifact_revisions'").get());
});

test('同一项目的版本序号递增并按新到旧读取', () => {
  const project = projectId();
  const repo = new ArtifactRevisionRepo(getDb());
  const first = repo.record({
    projectId: project,
    oid: oid(1),
    cause: 'initialize',
    summary: '创建项目',
    actorKind: 'system',
    actorName: '建模系统',
    changedPaths: [],
    createdAt: '2026-01-01T00:00:00.000Z',
  });
  const second = repo.record({
    projectId: project,
    oid: oid(2),
    parentOid: first.oid,
    cause: 'generate',
    summary: '生成第一版',
    actorKind: 'agent',
    actorName: '智能助手',
    packageVersion: '1.0.0',
    changedPaths: ['package/index.md', 'model/spec.json', 'package/index.md'],
    createdAt: '2026-01-01T00:01:00.000Z',
  });

  assert.equal(first.sequence, 1);
  assert.equal(second.sequence, 2);
  assert.equal(second.parentOid, first.oid);
  assert.equal(second.packageVersion, '1.0.0');
  assert.deepEqual(second.changedPaths, ['model/spec.json', 'package/index.md']);
  assert.deepEqual(repo.list(project).map((revision: { oid: string }) => revision.oid), [second.oid, first.oid]);
  assert.equal(repo.latest(project)?.oid, second.oid);
  assert.deepEqual(repo.get(project, first.oid), first);
  assert.equal(repo.get(project, oid(99)), null);
});

test('重复记录同一 Git 版本不会增加序号或覆盖说明', () => {
  const project = projectId();
  const repo = new ArtifactRevisionRepo(getDb());
  const first = repo.record({
    projectId: project,
    oid: oid(10),
    cause: 'edit',
    summary: '保存修改',
    actorKind: 'user',
    actorName: '张三',
    changedPaths: ['package/index.md'],
  });
  const replay = repo.record({
    projectId: project,
    oid: oid(10),
    cause: 'restore',
    summary: '这次内容不会覆盖原记录',
    actorKind: 'system',
    actorName: '建模系统',
  });

  assert.deepEqual(replay, first);
  assert.equal(repo.list(project).length, 1);
  const next = repo.record({
    projectId: project,
    oid: oid(11),
    parentOid: first.oid,
    cause: 'edit',
    summary: '再次修改',
    actorKind: 'user',
    actorName: '张三',
  });
  assert.equal(next.sequence, 2);
});

test('每个项目单独编号', () => {
  const firstProject = projectId();
  const secondProject = projectId();
  const repo = new ArtifactRevisionRepo(getDb());

  const first = repo.record({
    projectId: firstProject,
    oid: oid(20),
    cause: 'initialize',
    summary: '创建项目',
    actorKind: 'system',
    actorName: '建模系统',
  });
  const second = repo.record({
    projectId: secondProject,
    oid: oid(20),
    cause: 'initialize',
    summary: '创建项目',
    actorKind: 'system',
    actorName: '建模系统',
  });

  assert.equal(first.sequence, 1);
  assert.equal(second.sequence, 1);
});

test('错误输入返回简短提示', () => {
  const repo = new ArtifactRevisionRepo(getDb());
  const project = projectId();

  assert.throws(() => repo.record({
    projectId: project,
    oid: 'bad',
    cause: 'edit',
    summary: '保存修改',
    actorKind: 'user',
    actorName: '张三',
  }), (error: unknown) => error instanceof ArtifactRevisionIndexError
    && error.code === 'invalid_input' && error.message === '版本号不正确');

  assert.throws(() => repo.record({
    projectId: project,
    oid: oid(30),
    cause: 'edit',
    summary: '保存修改',
    actorKind: 'user',
    actorName: '张三',
    changedPaths: ['../outside.md'],
  }), (error: unknown) => error instanceof ArtifactRevisionIndexError
    && error.message === '文件路径不正确');

  assert.throws(() => repo.record({
    projectId: 'pj_missing',
    oid: oid(30),
    cause: 'edit',
    summary: '保存修改',
    actorKind: 'user',
    actorName: '张三',
  }), (error: unknown) => error instanceof ArtifactRevisionIndexError
    && error.code === 'project_not_found' && error.message === '项目不存在');
});
