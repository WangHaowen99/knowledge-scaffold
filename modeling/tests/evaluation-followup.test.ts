import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  followUpStepForRound,
  latestDiagnosableRound,
} from '../frontend/src/evaluation-followup.js';

test('评测未通过或运行失败后进入失败诊断，全部通过时不跳转', () => {
  assert.equal(followUpStepForRound({ status: 'done', passCount: 1, progressTotal: 2 }), 'diagnose');
  assert.equal(followUpStepForRound({ status: 'failed', passCount: 0, progressTotal: 2 }), 'diagnose');
  assert.equal(followUpStepForRound({ status: 'done', passCount: 2, progressTotal: 2 }), '');
  assert.equal(followUpStepForRound({ status: 'running', passCount: 0, progressTotal: 2 }), '');
});

test('失败诊断优先使用最新终态失败轮次，包括基础设施失败', () => {
  const rounds = [
    { id: 'failed-latest', status: 'failed', passCount: 0, progressTotal: 1 },
    { id: 'done-failed', status: 'done', passCount: 0, progressTotal: 1 },
    { id: 'done-passed', status: 'done', passCount: 1, progressTotal: 1 },
  ];

  assert.equal(latestDiagnosableRound(rounds)?.id, 'failed-latest');
});
