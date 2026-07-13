import test from 'node:test';
import assert from 'node:assert/strict';

import { clampSpotlightRect, getOnboardingTipStyle } from './onboardingLayout.js';

test('mobile onboarding tip remains inside the viewport', () => {
  const style = getOnboardingTipStyle(
    { x: -8, y: 100, w: 690, h: 80 },
    { width: 390, height: 844 },
    255,
  );

  assert.equal(style.left, 12);
  assert.equal(style.width, 366);
  assert.ok(style.left + style.width <= 390);
  assert.ok(style.top + 255 <= 832);
});

test('tall onboarding tip is constrained to the viewport', () => {
  const style = getOnboardingTipStyle(
    { x: 4, y: 240, w: 382, h: 350 },
    { width: 390, height: 640 },
    800,
  );

  assert.equal(style.top, 12);
  assert.equal(style.maxHeight, 616);
  assert.equal(style.overflowY, 'auto');
});

test('spotlight is clamped to all viewport edges', () => {
  const rect = clampSpotlightRect(
    { left: -20, right: 680, top: -10, bottom: 900 },
    { width: 390, height: 844 },
  );

  assert.deepEqual(rect, { x: 4, y: 4, w: 382, h: 836 });
});
