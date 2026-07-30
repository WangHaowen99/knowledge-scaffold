import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import {
  ArtifactRepositoryError,
  ArtifactRevisionRepo,
  IsomorphicGitArtifactRepository,
  PackageArtifactStore,
  PackageArtifactStoreError,
} from '../src/artifacts/index.js';
import type { CoverageResult, PackageSpec } from '../src/types.js';
import { renderPackage } from '../src/workflow/package-render.js';

const schema = await readFile(new URL('../src/db/schema.sql', import.meta.url), 'utf8');

async function fixture(t: test.TestContext) {
  const directory = await mkdtemp(join(tmpdir(), 'package-artifact-store-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const db = new DatabaseSync(':memory:');
  t.after(() => db.close());
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  const projectId = `pj_${Math.random().toString(16).slice(2, 14)}`;
  const now = '2026-01-01T00:00:00.000Z';
  db.prepare(`INSERT INTO projects
    (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?)`).run(projectId, 'test', '企业营销', '金融', '', 'project_setup', now, now);
  let tick = 0;
  const repository = new IsomorphicGitArtifactRepository({
    rootDir: join(directory, 'projects'),
    now: () => new Date(Date.UTC(2026, 0, 1, 0, 0, tick++)),
  });
  const revisions = new ArtifactRevisionRepo(db);
  const store = new PackageArtifactStore({ db, repository, revisions });
  return { db, projectId, repository, revisions, store };
}

function spec(version: string, description = `企业营销 ${version}`): PackageSpec {
  return {
    id: 'corporate-marketing',
    name: '企业对公营销',
    description,
    version,
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [{
      key: 'customer',
      title: '客户',
      description: '对公客户',
      sourceTable: 'customer',
      fields: [{ key: 'id', cnName: '客户号', sourceColumn: 'id' }],
    }],
    operations: [{
      key: 'recommend',
      title: '产品推荐',
      description: '推荐合适产品',
      inputs: [],
      outputs: [],
      flow: ['分析客户需求'],
      relatedObjects: ['customer'],
      usesRules: [],
    }],
  };
}

const coverage: CoverageResult = { covered: [], uncovered: [] };
const userAction = (summary: string) => ({
  cause: 'manual_edit',
  summary,
  actorKind: 'user' as const,
  actorName: '张经理',
  actorEmail: 'zhang@example.com',
});

test('首次访问按时间迁移旧版本并冻结各版本文件', async (t) => {
  const { db, projectId, store } = await fixture(t);
  const firstSpec = spec('1.0.0');
  const secondSpec = spec('1.1.0');
  const insert = db.prepare(`INSERT INTO package_versions
    (id, project_id, version, spec_json, coverage_json, note, created_at)
    VALUES (?,?,?,?,?,?,?)`);
  insert.run('pv_old_1', projectId, '1.0.0', JSON.stringify(firstSpec), JSON.stringify(coverage),
    '第一版', '2026-01-01T01:00:00.000Z');
  insert.run('pv_old_2', projectId, '1.1.0', JSON.stringify(secondSpec), JSON.stringify(coverage),
    '第二版', '2026-01-01T02:00:00.000Z');
  const current = await store.current(projectId);
  assert.equal(current?.packageVersion, '1.1.0');
  assert.equal((await store.readFile(projectId, 'package/index.md')).content,
    renderPackage(secondSpec)['index.md']);
  const currentFiles = await store.readPackageFiles(projectId);
  assert.equal(currentFiles.files['index.md'], renderPackage(secondSpec)['index.md']);
  assert.equal(currentFiles.files['manifest.yaml']?.includes('version: 1.1.0'), true);

  const first = await store.byPackageVersion(projectId, '1.0.0');
  const second = await store.byPackageVersion(projectId, '1.1.0');
  assert.equal(first?.spec.description, '企业营销 1.0.0');
  assert.equal(second?.spec.description, '企业营销 1.1.0');
  assert.equal((await store.readFile(projectId, 'package/index.md', first!.revision.oid)).content,
    renderPackage(firstSpec)['index.md']);
  assert.equal((await store.readFile(projectId, 'package/index.md', second!.revision.oid)).content,
    renderPackage(secondSpec)['index.md']);
  const historicalFiles = await store.readPackageFiles(projectId, first!.revision.oid);
  assert.equal(historicalFiles.spec.version, '1.0.0');
  assert.equal(historicalFiles.files['index.md'], renderPackage(firstSpec)['index.md']);

  const links = db.prepare(`SELECT version, revision_oid FROM package_versions
    WHERE project_id = ? ORDER BY created_at`).all(projectId) as any[];
  assert.ok(links.every((row) => row.revision_oid));
  assert.notEqual(links[0].revision_oid, links[1].revision_oid);
  const history = await store.history(projectId);
  assert.deepEqual(history.map((item) => item.cause), ['migration', 'migration', 'project_created']);
  assert.deepEqual(history.slice(0, 2).map((item) => item.packageVersion), ['1.1.0', '1.0.0']);

  await store.current(projectId);
  assert.equal((await store.history(projectId)).length, history.length);
});

test('新包版本同时写入 Git 和 SQLite，并冻结历史内容', async (t) => {
  const { db, projectId, store } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  const first = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: spec('1.0.0'),
    coverage,
    note: '首次生成',
    action: {
      cause: 'generate', summary: '生成第一版', actorKind: 'agent', actorName: '智能助手',
    },
  });
  const second = await store.savePackageVersion(projectId, {
    expectedRevision: first.revision.oid,
    spec: spec('1.1.0', '新版说明'),
    coverage,
    action: {
      cause: 'optimization', summary: '优化内容', actorKind: 'agent', actorName: '智能助手',
    },
  });

  const rows = db.prepare(`SELECT version, revision_oid FROM package_versions
    WHERE project_id = ? ORDER BY created_at, rowid`).all(projectId) as any[];
  assert.deepEqual(rows.map((row) => row.version), ['1.0.0', '1.1.0']);
  assert.deepEqual(rows.map((row) => row.revision_oid), [first.revision.oid, second.revision.oid]);
  assert.equal((await store.byPackageVersion(projectId, '1.0.0'))?.spec.description, '企业营销 1.0.0');
  assert.equal((await store.current(projectId))?.spec.description, '新版说明');
});

test('SQLite 索引写入失败时回滚 Git，不留下半提交且可直接重试', async (t) => {
  const { db, projectId, repository, store } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  db.exec(`CREATE TRIGGER fail_generated_revision
    BEFORE INSERT ON artifact_revisions
    WHEN NEW.cause = 'generate'
    BEGIN SELECT RAISE(ABORT, '模拟索引写入失败'); END`);

  const input = {
    expectedRevision: initial.oid,
    spec: spec('1.0.0'),
    coverage,
    note: '首次生成',
    action: {
      cause: 'generate', summary: '生成第一版', actorKind: 'agent' as const, actorName: '智能助手',
    },
  };
  await assert.rejects(store.savePackageVersion(projectId, input), /模拟索引写入失败/);

  assert.equal((await repository.current(projectId)).oid, initial.oid);
  assert.equal((await repository.snapshot(projectId)).files.length, 0);
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM package_versions WHERE project_id = ?')
    .get(projectId) as any).count, 0);
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM artifact_commit_intents WHERE project_id = ?')
    .get(projectId) as any).count, 0);
  assert.deepEqual((await store.history(projectId)).map((item) => item.cause), ['project_created']);

  db.exec('DROP TRIGGER fail_generated_revision');
  const retried = await store.savePackageVersion(projectId, input);
  assert.equal(retried.packageVersion, '1.0.0');
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM package_versions WHERE project_id = ?')
    .get(projectId) as any).count, 1);
  assert.deepEqual((await store.history(projectId)).map((item) => item.cause), ['generate', 'project_created']);
});

test('进程在 Git 提交后中断时，下次访问回滚未建立 SQLite 索引的提交', async (t) => {
  const { db, projectId, repository, revisions, store } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  db.prepare(`INSERT INTO artifact_commit_intents
    (project_id, base_revision_oid, created_at) VALUES (?,?,?)`)
    .run(projectId, initial.oid, '2026-01-01T00:00:00.000Z');
  const interrupted = await repository.commit(projectId, initial.oid, [{
    kind: 'write', path: 'package/index.md', content: '# 尚未建立索引的内容\n',
  }], { message: '模拟进程在 SQLite 提交前退出' });
  assert.equal((await repository.current(projectId)).oid, interrupted.oid);

  const restarted = new PackageArtifactStore({ db, repository, revisions });
  const recovered = await restarted.ensureProject(projectId);

  assert.equal(recovered.oid, initial.oid);
  assert.equal((await repository.current(projectId)).oid, initial.oid);
  assert.equal((await repository.snapshot(projectId)).files.length, 0);
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM artifact_commit_intents WHERE project_id = ?')
    .get(projectId) as any).count, 0);
  assert.deepEqual((await restarted.history(projectId)).map((item) => item.cause), ['project_created']);
});

test('并发请求不会把刚刚开始的跨存储提交误判为中断任务', async (t) => {
  const { db, projectId, repository, revisions, store } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  db.prepare(`INSERT INTO artifact_commit_intents
    (project_id, base_revision_oid, created_at) VALUES (?,?,?)`)
    .run(projectId, initial.oid, new Date().toISOString());

  const concurrentReader = new PackageArtifactStore({ db, repository, revisions });
  await assert.rejects(
    concurrentReader.ensureProject(projectId),
    (error) => error instanceof PackageArtifactStoreError
      && error.code === 'commit_in_progress',
  );

  assert.equal((await repository.current(projectId)).oid, initial.oid);
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM artifact_commit_intents WHERE project_id = ?')
    .get(projectId) as any).count, 1);
});

test('升级后首次访问也会回滚无意图、无索引的 Git 尾部提交', async (t) => {
  const { projectId, repository, revisions, store, db } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  await repository.commit(projectId, initial.oid, [{
    kind: 'write', path: 'package/index.md', content: '# 升级前中断的内容\n',
  }], { message: '模拟旧版本中断' });

  const restarted = new PackageArtifactStore({ db, repository, revisions });
  const recovered = await restarted.ensureProject(projectId);

  assert.equal(recovered.oid, initial.oid);
  assert.equal((await repository.current(projectId)).oid, initial.oid);
});

test('人工修改要求当前版本，撤销后恢复生成内容', async (t) => {
  const { db, projectId, store } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  const generated = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: spec('1.0.0'),
    coverage,
    action: { cause: 'generate', summary: '生成内容', actorKind: 'agent', actorName: '智能助手' },
  });
  const edited = await store.saveFile(projectId, {
    expectedRevision: generated.revision.oid,
    path: 'package/index.md',
    content: '# 人工修改\n',
    action: userAction('修改首页'),
  });
  assert.equal(edited.content, '# 人工修改\n');

  await assert.rejects(store.saveFile(projectId, {
    expectedRevision: generated.revision.oid,
    path: 'package/index.md',
    content: '# 旧页面修改\n',
    action: userAction('再次修改'),
  }), (error) => error instanceof ArtifactRepositoryError && error.code === 'revision_conflict');

  const reset = await store.resetFile(projectId, {
    expectedRevision: edited.revision.oid,
    path: 'package/index.md',
    action: userAction('撤销修改'),
  });
  assert.equal(reset.content, renderPackage(spec('1.0.0'))['index.md']);
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM package_versions WHERE project_id = ?')
    .get(projectId) as any).count, 1);
  assert.deepEqual((await store.history(projectId)).slice(0, 2).map((item) => item.packageVersion), [undefined, undefined]);
});

test('人工修订可保存为补丁版本且不覆盖修改内容', async (t) => {
  const { db, projectId, store } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  const generated = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: spec('1.1.0'),
    coverage,
    action: { cause: 'generate', summary: '生成内容', actorKind: 'agent', actorName: '智能助手' },
  });
  const originalIndex = (await store.readFile(projectId, 'package/index.md', generated.revision.oid)).content;
  const edited = await store.saveFile(projectId, {
    expectedRevision: generated.revision.oid,
    path: 'package/index.md',
    content: '# 人工确认后的知识包\n',
    action: userAction('修改首页'),
  });

  const patched = await store.saveCurrentAsPackageVersion(projectId, {
    expectedRevision: edited.revision.oid,
    version: '1.1.1',
    status: 'review',
    note: '人工评审修订',
    action: userAction('生成 1.1.1'),
  });

  assert.equal(patched.packageVersion, '1.1.1');
  assert.equal(patched.spec.version, '1.1.1');
  assert.equal(patched.spec.status, 'review');
  assert.equal((await store.readFile(projectId, 'package/index.md', patched.revision.oid)).content,
    '# 人工确认后的知识包\n');
  assert.match((await store.readFile(projectId, 'package/manifest.yaml', patched.revision.oid)).content,
    /version: 1\.1\.1/);
  assert.equal((await store.readFile(projectId, 'package/index.md', generated.revision.oid)).content, originalIndex);
  assert.deepEqual((db.prepare(`SELECT version FROM package_versions
    WHERE project_id = ? ORDER BY created_at, rowid`).all(projectId) as any[]).map((row) => row.version),
  ['1.1.0', '1.1.1']);
});

test('恢复旧包创建新修订但不创建包版本', async (t) => {
  const { db, projectId, store } = await fixture(t);
  const initial = await store.ensureProject(projectId);
  const first = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: spec('1.0.0'),
    coverage,
    action: { cause: 'generate', summary: '生成第一版', actorKind: 'agent', actorName: '智能助手' },
  });
  const second = await store.savePackageVersion(projectId, {
    expectedRevision: first.revision.oid,
    spec: spec('1.1.0'),
    coverage,
    action: { cause: 'generate', summary: '生成第二版', actorKind: 'agent', actorName: '智能助手' },
  });

  await assert.rejects(store.restore(projectId, {
    expectedRevision: second.revision.oid,
    targetRevision: initial.oid,
    action: userAction('恢复初始版本'),
  }), (error) => error instanceof PackageArtifactStoreError
    && error.code === 'revision_has_no_package' && error.message === '这个版本还没有知识包');

  const restored = await store.restore(projectId, {
    expectedRevision: second.revision.oid,
    targetRevision: first.revision.oid,
    action: { cause: 'restore', summary: '恢复第一版', actorKind: 'user', actorName: '张经理' },
  });
  assert.equal(restored.spec.version, '1.0.0');
  assert.notEqual(restored.revision.oid, first.revision.oid);
  assert.equal(restored.revision.parentOid, second.revision.oid);
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM package_versions WHERE project_id = ?')
    .get(projectId) as any).count, 2);
  const latest = (await store.history(projectId))[0];
  assert.equal(latest.cause, 'restore');
  assert.equal(latest.packageVersion, undefined);
  assert.ok((await store.diff(projectId, second.revision.oid, restored.revision.oid)).files.length > 0);
});
