import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-project-write-lock-'));

const [
  { createProject },
  { getPackageArtifactStore },
  { createOrGetPackageDraft },
] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/workflow/package-draft.js'),
]);

test('正式 revision 的提交检查与人工草稿创建按项目串行化', async () => {
  const project = createProject({ owner: 'alice', name: 'Project write lock', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: `lock-${project.id}`,
    name: '共享写锁测试',
    description: '验证草稿不能插入正式 revision 的检查和提交之间。',
  });
  let enteredCheck!: () => void;
  const checkEntered = new Promise<void>((resolve) => { enteredCheck = resolve; });
  let releaseCheck!: () => void;
  const checkRelease = new Promise<void>((resolve) => { releaseCheck = resolve; });
  const saving = artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: { ...initial.spec, description: '正式 revision 正在提交。' },
    coverage: initial.coverage,
    action: {
      cause: 'project_write_lock_test', summary: '验证共享项目写锁', actorKind: 'system', actorName: '测试系统',
    },
    beforeCommit: async () => {
      enteredCheck();
      await checkRelease;
    },
  });
  await checkEntered;

  let draftSettled = false;
  const creatingDraft = createOrGetPackageDraft(project.id, project.owner).finally(() => { draftSettled = true; });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(draftSettled, false);

  releaseCheck();
  const saved = await saving;
  const created = await creatingDraft;
  assert.equal(created.created, true);
  assert.equal(created.draft.baseRevisionOid, saved.revision.oid);
});
