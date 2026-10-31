import assert from 'node:assert/strict';
import { test } from 'node:test';

import { remainingHeight } from '../src/lib/useRemainingHeight.js';

test('填满到滚动容器内容区底部：扣掉底部内边距和取整余量', () => {
  assert.equal(
    remainingHeight({ containerBottom: 900, containerPaddingBottom: 24, elementTop: 476.4, minHeight: 240 }),
    398,
  );
});

test('剩余空间不足时保底，表格仍可用，由外层页面滚动', () => {
  assert.equal(
    remainingHeight({ containerBottom: 600, containerPaddingBottom: 24, elementTop: 520, minHeight: 240 }),
    240,
  );
});
