import test from 'node:test';
import assert from 'node:assert/strict';
import { hideCoverageMetrics } from '../src/lib/coveragePresentation.js';

const block = (value, type = 'paragraph') => ({ type, children: [{ type: 'text', value }] });
test('hides legacy coverage figures, retains missing branches and pass rate', () => {
  const tree = { children: [block('测试设计覆盖率', 'heading'), block('设计覆盖率：100%'),
    block('判断分支总数：14'), block('未覆盖分支：异常路径'), block('推演通过率：70%'),
    block('综合得分：80'), { type: 'code', value: '设计覆盖率：100%' }] };
  hideCoverageMetrics()(tree);
  assert.deepEqual(tree.children.map(n => n.value || n.children[0].value),
    ['测试设计缺项检查', '未覆盖分支：异常路径', '推演通过率：70%', '综合得分：80', '设计覆盖率：100%']);
});
test('keeps missing details on a combined metric line', () => {
  const tree = { children: [block('设计覆盖率：80% 未覆盖分支：退出')] };
  hideCoverageMetrics()(tree);
  assert.equal(tree.children[0].children[0].value, '未覆盖分支：退出');
});

const row = (...values) => ({ type: 'tableRow', children: values.map(v => block(v, 'tableCell')) });
test('hides coverage table columns while preserving checks and pass rate', () => {
  const table = { type: 'table', align: [null, null, null, null], children: [
    row('操作', '设计覆盖率', '缺失分支', '推演通过率'), row('合成操作', '80%', '异常退出', '70%'),
  ] };
  hideCoverageMetrics()({ children: [table] });
  assert.deepEqual(table.children.map(r => r.children.map(c => c.children[0].value)),
    [['操作', '缺失分支', '推演通过率'], ['合成操作', '异常退出', '70%']]);
  assert.equal(table.align.length, 3);
});
test('hides label/value metric rows without hiding missing-item rows', () => {
  const table = { type: 'table', children: [row('指标', '值'), row('设计覆盖率', '80%'),
    row('已覆盖分支数', '8/10'), row('未覆盖分支', '异常退出'), row('推演通过率', '70%')] };
  hideCoverageMetrics()({ children: [table] });
  assert.deepEqual(table.children.map(r => r.children[0].children[0].value),
    ['指标', '未覆盖分支', '推演通过率']);
});
