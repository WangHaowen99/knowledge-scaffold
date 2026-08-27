import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const tour = readFileSync(new URL('../frontend/src/Tour.jsx', import.meta.url), 'utf8');
const dataSource = readFileSync(new URL('../frontend/src/DataSourcePanel.jsx', import.meta.url), 'utf8');

test('嵌入评测页的数据源引导只在用户主动打开时显示', () => {
  assert.match(tour, /Tour\(\{ scene, steps, autoStart = true \}\)/u);
  assert.match(tour, /if \(autoStart && !localStorage\.getItem\(doneKey\)\) setIdx\(0\)/u);
  assert.match(dataSource, /<Tour[^>]+autoStart=\{false\}/u);
  assert.match(dataSource, /mw-tour-start/u);
});
