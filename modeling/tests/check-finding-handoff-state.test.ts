import assert from 'node:assert/strict';
import test from 'node:test';

import { upsertPendingAuthoringInteraction } from '../frontend/src/aiConstructionLiveState.js';
import {
  currentFindingIgnoreInteraction,
  findingIgnoreInteraction,
} from '../frontend/src/checkFindingHandoff.js';

const warning = { id: 'warning-1', severity: '警告' };
const error = { id: 'error-1', severity: '错误' };

test('当前修订的告警放行待办让按钮保持为已打开状态', () => {
  const interaction = {
    id: 'ignore-warning', status: 'pending', baseRevision: 'rev-current',
    payload: { purpose: 'check_finding_ignore', scope: 'warning', findingId: 'warning-1' },
  };
  assert.equal(findingIgnoreInteraction([interaction], warning, 'rev-current'), interaction);
  assert.equal(findingIgnoreInteraction([interaction], warning, 'rev-next'), null);
  assert.equal(findingIgnoreInteraction([interaction], { ...warning, id: 'warning-2' }, 'rev-current'), null);
});

test('错误放行虽是整批表单，但只有点开的错误按钮指向该待办', () => {
  const interaction = {
    id: 'ignore-errors', status: 'pending', baseRevision: 'rev-current',
    payload: { purpose: 'check_finding_ignore', scope: 'errors', findingId: 'error-1' },
  };
  assert.equal(currentFindingIgnoreInteraction([interaction], 'rev-current'), interaction);
  assert.equal(findingIgnoreInteraction([interaction], error, 'rev-current'), interaction);
  assert.equal(
    findingIgnoreInteraction([interaction], { id: 'error-2', severity: '错误' }, 'rev-current'),
    null,
  );
  assert.equal(findingIgnoreInteraction([interaction], warning, 'rev-current'), null);
});

test('接口返回的待办立即并入会话且重复对账不会产生两份', () => {
  const interaction = {
    id: 'ignore-warning', status: 'pending', runId: 'run-ignore',
    payload: { purpose: 'check_finding_ignore', findingId: 'warning-1' },
  };
  const session = {
    cursor: 3,
    authoring: { lastEventSeq: 3, pendingInteractions: [] },
  };
  const opened = upsertPendingAuthoringInteraction(session, interaction, 8);
  assert.equal(opened.cursor, 8);
  assert.equal(opened.authoring.lastEventSeq, 8);
  assert.deepEqual(opened.authoring.pendingInteractions, [interaction]);

  const reconciled = upsertPendingAuthoringInteraction(opened, { ...interaction, title: '填写理由' }, 8);
  assert.equal(reconciled.authoring.pendingInteractions.length, 1);
  assert.equal(reconciled.authoring.pendingInteractions[0].title, '填写理由');
});
