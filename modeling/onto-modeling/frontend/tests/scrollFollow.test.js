import assert from 'node:assert/strict';
import test from 'node:test';
import { STICK_GAP, scrollFollow } from '../src/pages/workbench/scrollFollow.js';

test('回到底部：离底部不到阈值算在底部，跟着新内容；往上翻过阈值就不跟、出按钮', () => {
  assert.equal(scrollFollow({ jumping: false, top: 0 }, { top: 900, gap: 0 }).stick, true);
  assert.equal(scrollFollow({ jumping: false, top: 900 }, { top: 860, gap: STICK_GAP - 1 }).stick, true);
  assert.equal(scrollFollow({ jumping: false, top: 900 }, { top: 700, gap: STICK_GAP }).stick, false);
});

test('回到底部：点了以后平滑滚动途中还没到底也算跟着，到底后回到按位置判断', () => {
  let f = { jumping: true, top: 100 };
  f = scrollFollow(f, { top: 400, gap: 2000 });
  assert.deepEqual(f, { jumping: true, top: 400, stick: true });
  f = scrollFollow(f, { top: 2400, gap: 0 });
  assert.deepEqual(f, { jumping: false, top: 2400, stick: true });
  f = scrollFollow(f, { top: 2000, gap: 400 });
  assert.equal(f.stick, false);
});

test('回到底部：平滑滚动途中往上滚（用户打断）就不再跟', () => {
  const f = scrollFollow({ jumping: true, top: 800 }, { top: 760, gap: 1500 });
  assert.deepEqual(f, { jumping: false, top: 760, stick: false });
  assert.equal(scrollFollow(f, { top: 900, gap: 1360 }).stick, false);
});
