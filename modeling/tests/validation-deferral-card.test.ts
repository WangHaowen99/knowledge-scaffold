import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

import ValidationDeferralCard from '../frontend/src/ValidationDeferralCard.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('暂缓错误卡明确保留风险且要求填写原因后才能继续', () => {
  const html = renderToStaticMarkup(React.createElement(ValidationDeferralCard, {
    errorCount: 4,
    onConfirm: () => {},
    onCancel: () => {},
  }));

  assert.match(html, /暂缓知识包错误修复/);
  assert.match(html, /保留 4 个阻断错误/);
  assert.match(html, /正式发布前仍必须消除/);
  assert.match(html, /说明暂缓原因/);
  assert.match(html, /确认暂缓并继续/);
  assert.match(html, /disabled/);
});
