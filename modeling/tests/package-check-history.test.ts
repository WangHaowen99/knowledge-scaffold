import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'package-check-history-'));

const { createProject } = await import('../src/projects/repo.js');
const { getDb } = await import('../src/db/index.js');
const {
  getCheck,
  saveCheck,
} = await import('../src/workflow/package-check.js');
const { getPackageWarningReview } = await import('../src/workflow/warning-acceptance.js');
const { deferPackageErrors, getPackageErrorDeferral } = await import('../src/workflow/package-error-deferral.js');
const { ArtifactRevisionRepo } = await import('../src/artifacts/revision-repo.js');

 test('迟到的旧修订校验不会覆盖已保存的新修订校验', () => {
  const project = createProject({ owner: 'alice', name: '校验修订历史' });
  saveCheck(project.id, '2.0.0', [{
    id: 'finding-b', source: 'rule', severity: '警告', category: '新修订',
    target: 'operations/b.md', message: 'B 修订的校验结果',
  }], 'revision-b');

  // A 的慢校验在 B 完成后才返回。
  saveCheck(project.id, '1.0.0', [{
    id: 'finding-a', source: 'rule', severity: '错误', category: '旧修订',
    target: 'operations/a.md', message: 'A 修订的迟到结果',
  }], 'revision-a');

  const current = getCheck(project.id, 'revision-b');
  assert.equal(current?.version, '2.0.0');
  assert.equal(current?.revision, 'revision-b');
  assert.deepEqual(current?.findings.map((finding) => finding.id), ['finding-b']);
});

test('兼容调用按 artifact 序号读取最新修订而不是最后完成的校验', () => {
  const project = createProject({ owner: 'alice', name: '最近校验兼容读取' });
  const revisionA = 'a'.repeat(40);
  const revisionB = 'b'.repeat(40);
  const revisions = new ArtifactRevisionRepo();
  revisions.record({
    projectId: project.id, oid: revisionA, cause: 'generate', summary: 'A 修订',
    actorKind: 'agent', actorName: '建模助手',
  });
  revisions.record({
    projectId: project.id, oid: revisionB, parentOid: revisionA, cause: 'edit', summary: 'B 修订',
    actorKind: 'user', actorName: 'alice',
  });

  saveCheck(project.id, '2.0.0', [{
    id: 'finding-b-sequenced', source: 'rule', severity: '警告', category: '新修订',
    target: 'operations/b.md', message: 'B 修订的校验结果',
  }], revisionB);
  saveCheck(project.id, '1.0.0', [{
    id: 'finding-a-sequenced', source: 'rule', severity: '错误', category: '旧修订',
    target: 'operations/a.md', message: 'A 修订的迟到结果',
  }], revisionA);

  assert.equal(getCheck(project.id)?.revision, revisionB);
});

test('精确读取兼容只存在于旧 package_checks 表的记录', () => {
  const project = createProject({ owner: 'alice', name: '旧校验表兼容' });
  getDb().prepare(`INSERT INTO package_checks
    (project_id, version, findings_json, artifact_revision_oid, created_at)
    VALUES (?,?,?,?,?)`).run(
    project.id,
    '0.9.0',
    JSON.stringify([{ id: 'legacy-finding', source: 'rule', severity: '提示',
      category: '旧记录', target: 'package/index.md', message: '旧表中的校验结果' }]),
    'legacy-revision',
    '2026-07-24T00:00:00.000Z',
  );

  const restored = getCheck(project.id, 'legacy-revision');
  assert.equal(restored?.version, '0.9.0');
  assert.equal(restored?.findings[0].id, 'legacy-finding');
});

test('警告复核始终读取明确修订的校验结果', () => {
  const project = createProject({ owner: 'alice', name: '警告修订历史' });
  saveCheck(project.id, '2.0.0', [{
    id: 'warning-b', source: 'semantic', severity: '警告', category: '业务边界',
    target: 'operations/b.md', message: 'B 修订待确认的业务边界',
  }], 'revision-b');
  saveCheck(project.id, '1.0.0', [{
    id: 'warning-a', source: 'semantic', severity: '警告', category: '旧业务边界',
    target: 'operations/a.md', message: 'A 修订迟到的业务边界',
  }], 'revision-a');

  const review = getPackageWarningReview(project.id, 'revision-b');
  assert.equal(review.warningTotal, 1);
  assert.equal(review.items[0].finding.id, 'warning-b');
});

test('旧修订校验迟到不使新修订的错误暂缓失效', () => {
  const project = createProject({ owner: 'alice', name: '错误暂缓修订历史' });
  saveCheck(project.id, '2.0.0', [{
    id: 'error-b', source: 'semantic', severity: '错误', category: '业务口径',
    target: 'operations/b.md', message: 'B 修订尚待后续评测验证',
  }], 'revision-b');
  deferPackageErrors({
    projectId: project.id,
    revision: 'revision-b',
    reason: '先验证完整业务链路，再依据真实评测统一修复',
    actor: 'alice',
    actorRole: 'project_owner',
  });

  saveCheck(project.id, '1.0.0', [{
    id: 'error-a', source: 'semantic', severity: '错误', category: '旧业务口径',
    target: 'operations/a.md', message: 'A 修订迟到的错误',
  }], 'revision-a');

  assert.equal(getPackageErrorDeferral(project.id, 'revision-b')?.valid, true);
});
