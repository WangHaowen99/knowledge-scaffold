import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ASSISTANT_SCROLL_ACTION,
  assistantScrollAction,
} from '../frontend/src/workbench/right/scrollPolicy.js';

test('历史消息回放期间固定视口，完成后只瞬时定位一次', () => {
  assert.equal(assistantScrollAction({
    replayingHistory: true,
    followingTail: true,
  }), ASSISTANT_SCROLL_ACTION.HOLD);
  assert.equal(assistantScrollAction({
    historyJustFinished: true,
    followingTail: true,
  }), ASSISTANT_SCROLL_ACTION.TAIL_INSTANT);
});

test('历史回放结束时，恢复的待确认卡片使用瞬时定位', () => {
  assert.equal(assistantScrollAction({
    historyJustFinished: true,
    followingTail: true,
    hasNewInteraction: true,
  }), ASSISTANT_SCROLL_ACTION.FOCUS_INSTANT);
  assert.equal(assistantScrollAction({
    followingTail: true,
    hasNewInteraction: true,
  }), ASSISTANT_SCROLL_ACTION.FOCUS_SMOOTH);
});

test('用户离开底部后不抢滚动位置', () => {
  assert.equal(assistantScrollAction({
    followingTail: false,
  }), ASSISTANT_SCROLL_ACTION.HOLD);
});
