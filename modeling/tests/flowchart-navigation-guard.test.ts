import assert from 'node:assert/strict';
import test from 'node:test';

import {
  confirmFlowchartNavigation,
  protectFlowchartBeforeUnload,
} from '../frontend/src/flowchartNavigationGuard.js';

test('流程图没有未保存修改时直接允许切换步骤', () => {
  let confirmations = 0;
  assert.equal(confirmFlowchartNavigation(false, () => {
    confirmations += 1;
    return false;
  }), true);
  assert.equal(confirmations, 0);
});

test('流程图有未保存修改时由用户决定是否离开', () => {
  assert.equal(confirmFlowchartNavigation(true, () => false), false);
  assert.equal(confirmFlowchartNavigation(true, () => true), true);
});

test('流程图有未保存修改时阻止浏览器直接卸载', () => {
  let prevented = false;
  const event = {
    returnValue: undefined,
    preventDefault() { prevented = true; },
  };
  assert.equal(protectFlowchartBeforeUnload(event, true), true);
  assert.equal(prevented, true);
  assert.equal(event.returnValue, '');
});
