import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'warning-acceptance-'));

const { createProject } = await import('../src/projects/repo.js');
const { saveCheck } = await import('../src/workflow/package-check.js');
const {
  acceptPackageWarnings,
  getPackageWarningReview,
  getReleaseKnownLimitations,
  saveReleaseKnownLimitations,
} = await import('../src/workflow/warning-acceptance.js');

const warning = {
  id: 'warning-1',
  source: 'rule' as const,
  severity: '警告' as const,
  category: '口径待确认',
  target: 'metrics/score.md',
  message: '边界口径尚未由专家确认',
};

test('警告接受绑定知识包修订并在修订变化后自动失效', () => {
  const project = createProject({ owner: 'alice', name: '警告接受' });
  saveCheck(project.id, '0.1.0', [warning], 'revision-a');

  const before = getPackageWarningReview(project.id, 'revision-a');
  assert.equal(before.pendingValidation, 1);
  assert.equal(before.readyForValidation, false);

  const accepted = acceptPackageWarnings({
    projectId: project.id,
    revision: 'revision-a',
    fingerprints: [before.items[0].fingerprint],
    note: '已核对影响：仅影响边界说明，不改变主流程结论',
    actor: 'alice',
    actorRole: 'project_owner',
    phase: 'validation',
  });
  assert.equal(accepted.readyForValidation, true);
  assert.equal(accepted.pendingReleaseConfirmation, 1);

  saveCheck(project.id, '0.1.1', [warning], 'revision-b');
  const changed = getPackageWarningReview(project.id, 'revision-b');
  assert.equal(changed.readyForValidation, false);
  assert.equal(changed.items[0].accepted, false);
});

test('发布前必须再次确认非阻断警告并保留已知限制', () => {
  const project = createProject({ owner: 'alice', name: '发布限制' });
  saveCheck(project.id, '1.0.0-rc.1', [warning], 'revision-a');
  const first = getPackageWarningReview(project.id, 'revision-a');
  acceptPackageWarnings({
    projectId: project.id,
    revision: 'revision-a',
    fingerprints: [first.items[0].fingerprint],
    note: '验证阶段已接受影响',
    actor: 'alice',
    actorRole: 'project_owner',
    phase: 'validation',
  });
  const releaseAccepted = acceptPackageWarnings({
    projectId: project.id,
    revision: 'revision-a',
    fingerprints: [first.items[0].fingerprint],
    note: '同意作为本次发布的已知限制',
    actor: 'alice',
    actorRole: 'project_owner',
    phase: 'release',
  });
  assert.equal(releaseAccepted.readyForRelease, true);
  assert.equal(releaseAccepted.knownLimitations.length, 1);

  saveReleaseKnownLimitations({
    projectId: project.id,
    version: '1.0.0',
    revision: 'revision-a',
    knownLimitations: releaseAccepted.knownLimitations,
    actor: 'alice',
  });
  assert.deepEqual(getReleaseKnownLimitations(project.id, '1.0.0'), releaseAccepted.knownLimitations);
});

test('发布阻断警告即使在验证阶段接受也不能带入发布', () => {
  const project = createProject({ owner: 'alice', name: '阻断警告' });
  const blocking = { ...warning, id: 'warning-blocking', releaseBlocking: true };
  saveCheck(project.id, '1.0.0-rc.1', [blocking], 'revision-a');
  const review = getPackageWarningReview(project.id, 'revision-a');
  const accepted = acceptPackageWarnings({
    projectId: project.id,
    revision: 'revision-a',
    fingerprints: [review.items[0].fingerprint],
    note: '验证阶段记录影响',
    actor: 'alice',
    actorRole: 'project_owner',
    phase: 'validation',
  });
  assert.equal(accepted.readyForValidation, true);
  assert.equal(accepted.readyForRelease, false);
  assert.equal(accepted.releaseBlocking, 1);
  assert.throws(() => acceptPackageWarnings({
    projectId: project.id,
    revision: 'revision-a',
    fingerprints: [review.items[0].fingerprint],
    note: '尝试发布',
    actor: 'alice',
    actorRole: 'project_owner',
    phase: 'release',
  }), /发布阻断警告/);
});
