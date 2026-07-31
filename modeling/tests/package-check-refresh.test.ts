import assert from 'node:assert/strict';
import { test } from 'node:test';

import { synchronizePackageCheck } from '../frontend/src/packageCheckRefresh.js';

test('知识包更新事件会自动重检过期修订且同一修订只执行一次', async () => {
  const eventTarget = new EventTarget();
  let currentRevision = 'revision-2';
  let checkedRevision = 'revision-1';
  let runCount = 0;
  let updatedEvents = 0;
  let invalidatedViews = 0;
  eventTarget.addEventListener('mw-package-check-updated', () => { updatedEvents += 1; });

  const sync = synchronizePackageCheck({
    projectId: 'project-check-refresh',
    eventTarget,
    loadCheck: async () => ({
      currentRevision,
      stale: checkedRevision !== currentRevision,
      check: checkedRevision === currentRevision ? { revision: checkedRevision, findings: [] } : null,
      warningReview: null,
    }),
    runCheck: async () => {
      runCount += 1;
      checkedRevision = currentRevision;
      return { version: '1.0.0', revision: checkedRevision, findings: [], warningReview: null };
    },
    onPackageUpdated: () => { invalidatedViews += 1; },
  });

  await sync.whenIdle();
  assert.equal(runCount, 1);
  assert.equal(updatedEvents, 1);

  eventTarget.dispatchEvent(new Event('mw-package-updated'));
  assert.equal(invalidatedViews, 1, '知识包更新时应立即使当前检查视图失效');
  eventTarget.dispatchEvent(new Event('mw-package-check-updated'));
  await sync.whenIdle();
  assert.equal(runCount, 1, '同一当前修订的重复事件不得再次运行检查');

  currentRevision = 'revision-3';
  eventTarget.dispatchEvent(new Event('mw-package-updated'));
  await sync.whenIdle();
  assert.equal(runCount, 2, '新的知识包修订应自动运行一次检查');
  sync.stop();
});

test('同一修订自动重检失败后可由后续更新事件再次触发', async () => {
  const eventTarget = new EventTarget();
  let runCount = 0;
  let checked = false;
  const errors: string[] = [];

  const sync = synchronizePackageCheck({
    projectId: 'project-check-retry',
    eventTarget,
    loadCheck: async () => ({
      currentRevision: 'revision-retry',
      stale: !checked,
      check: checked ? { revision: 'revision-retry', findings: [] } : null,
      warningReview: null,
    }),
    runCheck: async () => {
      runCount += 1;
      if (runCount === 1) throw new Error('临时检查失败');
      checked = true;
      return { revision: 'revision-retry', findings: [] };
    },
    onError: (error) => { errors.push(error instanceof Error ? error.message : String(error)); },
  });

  await sync.whenIdle();
  assert.equal(runCount, 1);
  assert.deepEqual(errors, ['临时检查失败']);

  eventTarget.dispatchEvent(new Event('mw-package-updated'));
  await sync.whenIdle();
  assert.equal(runCount, 2, '失败的同一修订必须允许后续事件重试');

  eventTarget.dispatchEvent(new Event('mw-package-updated'));
  await sync.whenIdle();
  assert.equal(runCount, 2, '同一修订成功后仍不得重复检查');
  sync.stop();
});

test('同步器卸载后在途检查完成不得回写界面或广播事件', async () => {
  const eventTarget = new EventTarget();
  let resolveCheck!: (value: { revision: string; findings: never[] }) => void;
  const inFlight = new Promise<{ revision: string; findings: never[] }>((resolve) => {
    resolveCheck = resolve;
  });
  const callbacks: string[] = [];
  let updatedEvents = 0;
  eventTarget.addEventListener('mw-package-check-updated', () => { updatedEvents += 1; });

  const sync = synchronizePackageCheck({
    projectId: 'project-check-unmounted',
    eventTarget,
    loadCheck: async () => ({
      currentRevision: 'revision-unmounted',
      stale: true,
      check: null,
      warningReview: null,
    }),
    runCheck: async () => inFlight,
    onChecked: () => { callbacks.push('checked'); },
    onError: () => { callbacks.push('error'); },
    onRecheckStart: () => { callbacks.push('start'); },
    onRecheckFinish: () => { callbacks.push('finish'); },
  });

  await new Promise<void>((resolve) => { queueMicrotask(resolve); });
  assert.deepEqual(callbacks, ['start']);
  sync.stop();
  resolveCheck({ revision: 'revision-unmounted', findings: [] });
  await sync.whenIdle();

  assert.deepEqual(callbacks, ['start']);
  assert.equal(updatedEvents, 0);
});
