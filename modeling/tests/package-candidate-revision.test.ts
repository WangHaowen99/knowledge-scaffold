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

const schema = await readFile(new URL('../src/db/schema.sql', import.meta.url), 'utf8');
const coverage: CoverageResult = { covered: [], uncovered: [] };

function spec(): PackageSpec {
  return {
    id: 'candidate-review',
    name: '候选修订测试',
    description: '验证候选修订不会越过专家确认',
    version: '1.0.0-rc.4',
    status: 'review',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [{
      key: 'Application', title: '申请单', description: '业务申请单', fields: [],
    }],
    operations: [{
      key: 'Approve', title: '审批', description: '审批申请单',
      inputs: [], outputs: [], flow: ['检查申请单'], relatedObjects: ['Application'],
    }],
  };
}

const action = (cause: string, summary: string) => ({
  cause,
  summary,
  actorKind: 'user' as const,
  actorName: 'alice',
});

async function fixture(t: test.TestContext) {
  const directory = await mkdtemp(join(tmpdir(), 'package-candidate-revision-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const db = new DatabaseSync(':memory:');
  t.after(() => db.close());
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  const projectId = `pj_${Math.random().toString(16).slice(2, 14)}`;
  const now = '2026-07-30T00:00:00.000Z';
  db.prepare(`INSERT INTO projects
    (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?)`).run(
    projectId, 'alice', '候选修订测试', '审批', '', 'generate', now, now,
  );
  let tick = 0;
  const repository = new IsomorphicGitArtifactRepository({
    rootDir: join(directory, 'projects'),
    now: () => new Date(Date.UTC(2026, 6, 30, 0, 0, tick++)),
  });
  const store = new PackageArtifactStore({
    db,
    repository,
    revisions: new ArtifactRevisionRepo(db),
  });
  const initial = await store.ensureProject(projectId);
  const generated = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: spec(),
    coverage,
    action: action('generate', '建立测试知识包'),
  });
  return { db, projectId, repository, store, generated };
}

test('预览不会推进当前修订，接受依赖组时自动补齐并生成递增版本', async (t) => {
  const { db, projectId, repository, store, generated } = await fixture(t);
  const index = (await store.readFile(projectId, 'package/index.md')).content;
  const operation = (await store.readFile(projectId, 'package/operations/Approve.md')).content;
  const candidate = await store.createCandidateRevision(projectId, {
    expectedRevision: generated.revision.oid,
    groups: [{
      id: 'base-description',
      summary: '补充知识包说明',
      changes: [{ kind: 'write', path: 'package/index.md', content: `${index}\n候选说明\n` }],
    }, {
      id: 'approve-detail',
      summary: '补充审批操作说明',
      dependsOn: ['base-description'],
      changes: [{
        kind: 'write',
        path: 'package/operations/Approve.md',
        content: `${operation}\n审批候选说明\n`,
      }],
    }],
    action: action('candidate_created', '准备两组候选修改'),
  });

  const preview = await store.previewCandidateRevision(projectId, candidate.id, ['approve-detail']);
  assert.deepEqual(preview.selection.requestedGroupIds, ['approve-detail']);
  assert.deepEqual(preview.selection.selectedGroupIds, ['base-description', 'approve-detail']);
  assert.deepEqual(preview.selection.autoIncludedGroupIds, ['base-description']);
  assert.equal((await repository.current(projectId)).oid, generated.revision.oid);
  assert.doesNotMatch((await store.readFile(projectId, 'package/index.md')).content, /候选说明/u);

  const accepted = await store.acceptCandidateRevision(projectId, {
    candidateId: candidate.id,
    selectedGroupIds: ['approve-detail'],
    action: action('candidate_accepted', '接受审批候选修改'),
  });
  assert.equal(accepted.revision.parentOid, generated.revision.oid);
  assert.equal(accepted.packageVersion, '1.0.0-rc.5');
  assert.deepEqual(accepted.candidate.selectedGroupIds, ['base-description', 'approve-detail']);
  assert.match((await store.readFile(projectId, 'package/index.md')).content, /候选说明/u);
  assert.match((await store.readFile(projectId, 'package/operations/Approve.md')).content, /审批候选说明/u);
  const current = await store.current(projectId);
  assert.equal(current?.packageVersion, '1.0.0-rc.5');
  assert.equal(current?.spec.status, 'review');
  assert.match((await store.readFile(projectId, 'package/manifest.yaml')).content, /version: 1\.0\.0-rc\.5/u);
  const versions = db.prepare(`SELECT version, revision_oid FROM package_versions
    WHERE project_id = ? ORDER BY rowid`).all(projectId) as any[];
  assert.deepEqual(versions.map((row) => row.version), ['1.0.0-rc.4', '1.0.0-rc.5']);
  assert.equal(versions[1].revision_oid, accepted.revision.oid);
  assert.equal((await store.history(projectId))[0].packageVersion, '1.0.0-rc.5');
});

test('校验错误、退回和拒绝都不会改变当前知识包修订', async (t) => {
  const { projectId, store, generated } = await fixture(t);
  const index = (await store.readFile(projectId, 'package/index.md')).content;
  const create = (id: string, validation?: { errors?: string[] }) => store.createCandidateRevision(
    projectId,
    {
      expectedRevision: generated.revision.oid,
      groups: [{
        id,
        summary: `候选 ${id}`,
        changes: [{ kind: 'write', path: 'package/index.md', content: `${index}\n${id}\n` }],
        validation,
      }],
      action: action('candidate_created', `创建候选 ${id}`),
    },
  );

  const invalid = await create('invalid', { errors: ['结构校验未通过'] });
  await assert.rejects(
    store.acceptCandidateRevision(projectId, {
      candidateId: invalid.id,
      action: action('candidate_accepted', '尝试接受错误候选'),
    }),
    (error) => error instanceof PackageArtifactStoreError
      && error.code === 'candidate_validation_failed',
  );
  assert.equal((await store.getCandidateRevision(projectId, invalid.id)).status, 'pending');

  const returned = await create('returned');
  const returnResult = await store.returnCandidateRevision(projectId, {
    candidateId: returned.id,
    selectedGroupIds: ['returned'],
    note: '请补充影响说明',
    action: action('candidate_revision_requested', '退回候选修订'),
  });
  assert.equal(returnResult.status, 'revision_requested');
  assert.equal(returnResult.decision?.note, '请补充影响说明');

  const rejected = await create('rejected');
  const rejectResult = await store.rejectCandidateRevision(projectId, {
    candidateId: rejected.id,
    note: '不采用这项修改',
    action: action('candidate_rejected', '拒绝候选修订'),
  });
  assert.equal(rejectResult.status, 'rejected');
  assert.equal((await store.current(projectId))?.revision.oid, generated.revision.oid);
});

test('基线变化会使候选失效并记录准确的失效原因', async (t) => {
  const { projectId, store, generated } = await fixture(t);
  const index = (await store.readFile(projectId, 'package/index.md')).content;
  const candidate = await store.createCandidateRevision(projectId, {
    expectedRevision: generated.revision.oid,
    groups: [{
      id: 'stale-change',
      summary: '将要过期的修改',
      changes: [{ kind: 'write', path: 'package/index.md', content: `${index}\n候选内容\n` }],
    }],
    action: action('candidate_created', '创建将要过期的候选'),
  });
  const latest = await store.saveFile(projectId, {
    expectedRevision: generated.revision.oid,
    path: 'package/index.md',
    content: `${index}\n人工更新\n`,
    action: action('manual_edit', '人工更新当前版本'),
  });

  await assert.rejects(
    store.acceptCandidateRevision(projectId, {
      candidateId: candidate.id,
      action: action('candidate_accepted', '尝试接受过期候选'),
    }),
    (error) => error instanceof ArtifactRepositoryError && error.code === 'revision_conflict',
  );
  const superseded = await store.getCandidateRevision(projectId, candidate.id);
  assert.equal(superseded.status, 'superseded');
  assert.equal(superseded.decision?.cause, 'candidate_superseded');
  assert.equal(superseded.decision?.summary, '候选修订因基线变化失效');
  assert.equal((await store.current(projectId))?.revision.oid, latest.revision.oid);
});

test('候选接受索引失败时回滚 Git 并保留待处理候选', async (t) => {
  const { db, projectId, repository, store, generated } = await fixture(t);
  const index = (await store.readFile(projectId, 'package/index.md')).content;
  const candidate = await store.createCandidateRevision(projectId, {
    expectedRevision: generated.revision.oid,
    groups: [{
      id: 'rollback-change',
      summary: '验证失败回滚',
      changes: [{ kind: 'write', path: 'package/index.md', content: `${index}\n回滚候选\n` }],
    }],
    action: action('candidate_created', '创建回滚候选'),
  });
  db.exec(`CREATE TRIGGER fail_candidate_revision
    BEFORE INSERT ON artifact_revisions
    WHEN NEW.cause = 'candidate_accepted'
    BEGIN SELECT RAISE(ABORT, '模拟候选索引失败'); END`);

  await assert.rejects(
    store.acceptCandidateRevision(projectId, {
      candidateId: candidate.id,
      action: action('candidate_accepted', '接受回滚候选'),
    }),
    /模拟候选索引失败/u,
  );
  assert.equal((await repository.current(projectId)).oid, generated.revision.oid);
  assert.equal((await store.getCandidateRevision(projectId, candidate.id)).status, 'pending');
  assert.equal((db.prepare(`SELECT COUNT(*) AS count FROM package_versions
    WHERE project_id = ?`).get(projectId) as any).count, 1);
  assert.equal((db.prepare(`SELECT COUNT(*) AS count FROM artifact_commit_intents
    WHERE project_id = ?`).get(projectId) as any).count, 0);
});
