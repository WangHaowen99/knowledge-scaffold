import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const [
  { createProjectEventReplayGate },
  {
    PACKAGE_WORKSPACE_CANDIDATE_EVENT_TYPES,
    planPackageWorkspaceProjectEvent,
    planPackageWorkspaceWindowRefresh,
    shouldLoadPackageWorkspaceRevisions,
  },
] = await Promise.all([
  import('../frontend/src/projectEventReplay.js'),
  import('../frontend/src/packageWorkspaceRefresh.js'),
]);

test('PackageWorkspace 仅在版本视图下重拉 revisions', () => {
  assert.equal(shouldLoadPackageWorkspaceRevisions({ activeTab: 'versions' }), true);
  assert.equal(
    shouldLoadPackageWorkspaceRevisions({ activeTab: 'release', embedded: true, deliveryView: 'versions' }),
    true,
  );
  assert.equal(shouldLoadPackageWorkspaceRevisions({ activeTab: 'release', embedded: true, deliveryView: 'release' }), false);
  assert.equal(shouldLoadPackageWorkspaceRevisions({ activeTab: 'edit' }), false);
});

test('PackageWorkspace 的本地 window 刷新计划会保留版本视图补拉', () => {
  assert.deepEqual(planPackageWorkspaceWindowRefresh({ activeTab: 'edit' }), {
    scheduleLoad: true,
    loadRevisions: false,
  });
  assert.deepEqual(planPackageWorkspaceWindowRefresh({ activeTab: 'versions' }), {
    scheduleLoad: true,
    loadRevisions: true,
  });
  assert.deepEqual(
    planPackageWorkspaceWindowRefresh({ activeTab: 'release', embedded: true, deliveryView: 'versions' }),
    {
      scheduleLoad: true,
      loadRevisions: true,
    },
  );
});

test('PackageWorkspace 在正式修订事件到达时会刷新页面和版本列表', () => {
  const plan = planPackageWorkspaceProjectEvent({
    event: { type: 'package.revision.created', seq: 12 },
    gate: createProjectEventReplayGate(0),
    workspaceDirty: false,
    activeTab: 'versions',
  });

  assert.equal(PACKAGE_WORKSPACE_CANDIDATE_EVENT_TYPES.has('package.revision.created'), true);
  assert.deepEqual(plan, {
    gate: { replayThrough: 0, replaying: false, pendingRefresh: false },
    historical: false,
    candidateRefresh: true,
    immediateLoad: false,
    scheduleLoad: true,
    deferLoad: false,
    loadRevisions: true,
  });
});

test('PackageWorkspace 在候选类事件到达时不会误重拉正式版本列表', () => {
  const plan = planPackageWorkspaceProjectEvent({
    event: { type: 'optimization.expert_review.resolved', seq: 18 },
    gate: createProjectEventReplayGate(0),
    workspaceDirty: false,
    activeTab: 'versions',
  });

  assert.equal(plan.candidateRefresh, true);
  assert.equal(plan.scheduleLoad, true);
  assert.equal(plan.loadRevisions, false);
});

test('PackageWorkspace 在脏工作区会延后 project event 刷新', () => {
  const plan = planPackageWorkspaceProjectEvent({
    event: { type: 'package.revision.created', seq: 22 },
    gate: createProjectEventReplayGate(0),
    workspaceDirty: true,
    activeTab: 'edit',
  });

  assert.equal(plan.candidateRefresh, true);
  assert.equal(plan.scheduleLoad, false);
  assert.equal(plan.deferLoad, true);
  assert.equal(plan.loadRevisions, false);
});

test('PackageWorkspace 对 interaction.opened 会立即回源，脏工作区则只延后', () => {
  const clean = planPackageWorkspaceProjectEvent({
    event: { type: 'authoring.interaction.opened', seq: 30 },
    gate: createProjectEventReplayGate(0),
    workspaceDirty: false,
    activeTab: 'edit',
  });
  assert.equal(clean.historical, false);
  assert.equal(clean.immediateLoad, true);
  assert.equal(clean.scheduleLoad, true);
  assert.equal(clean.deferLoad, false);

  const dirty = planPackageWorkspaceProjectEvent({
    event: { type: 'authoring.interaction.opened', seq: 31 },
    gate: createProjectEventReplayGate(0),
    workspaceDirty: true,
    activeTab: 'edit',
  });
  assert.equal(dirty.immediateLoad, false);
  assert.equal(dirty.scheduleLoad, false);
  assert.equal(dirty.deferLoad, true);
});

test('PackageWorkspace 在 replay 边界的历史正式修订事件上只补一次 catch-up 刷新', () => {
  const plan = planPackageWorkspaceProjectEvent({
    event: { type: 'package.revision.created', seq: 7 },
    gate: createProjectEventReplayGate(7),
    workspaceDirty: false,
    activeTab: 'versions',
  });

  assert.deepEqual(plan, {
    gate: { replayThrough: 7, replaying: false, pendingRefresh: false },
    historical: true,
    candidateRefresh: true,
    immediateLoad: false,
    scheduleLoad: true,
    deferLoad: false,
    loadRevisions: true,
  });
});

test('PackageWorkspace 已把 project event 的 revision 刷新计划接回组件逻辑', () => {
  const source = readFileSync(new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url), 'utf8');

  assert.match(source, /planPackageWorkspaceProjectEvent\(\{/);
  assert.match(source, /activeTab,\s*embedded,\s*deliveryView,/s);
  assert.match(source, /if \(plan\.loadRevisions\) void loadRevisions\(\);/);
});
