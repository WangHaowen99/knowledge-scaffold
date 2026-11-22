// Studio 表格的规则筛选与更新时间范围（pages/studio/studioTable.js，develop !611）。全部为合成数据。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  STUDIO_UNSET_OWNER,
  studioOwnerFilters,
  studioOwnerMatches,
  studioRuleColumnsVisible,
  studioTableFilters,
  studioTableMatches,
  studioUpdatedRange,
  studioUpdatedRangeKey,
  studioUpdatedWithin,
} from '../src/pages/studio/studioTable.js';

test('只有类型选了规则时才给出责任部门、维护人两列', () => {
  assert.equal(studioRuleColumnsVisible('rule'), true);
  assert.equal(studioRuleColumnsVisible('all'), false);
  assert.equal(studioRuleColumnsVisible('object'), false);
  assert.equal(studioRuleColumnsVisible(undefined), false);
});

test('类型从规则切走时清掉部门、维护人筛选，切回规则不带出旧条件', () => {
  const ruleFilters = { departments: ['合成部门甲'], maintainers: ['维护人A'], updated: '2026-11-01~' };
  assert.equal(studioTableFilters(ruleFilters, 'rule'), ruleFilters);

  const objectFilters = studioTableFilters(ruleFilters, 'object');
  assert.deepEqual(objectFilters, { updated: '2026-11-01~' });
  assert.deepEqual(ruleFilters.departments, ['合成部门甲']);

  const backToRule = studioTableFilters(objectFilters, 'rule');
  assert.equal(backToRule.departments, undefined);
  assert.equal(backToRule.maintainers, undefined);
});

const seconds = (year, month, day, hour = 12) => new Date(year, month - 1, day, hour).getTime() / 1000;
const rows = [
  { id: '规则甲', kind: 'rule', departments: ['合成部门甲'], maintainers: ['维护人A', '维护人B'], updatedAt: seconds(2026, 11, 2) },
  { id: '规则乙', kind: 'rule', departments: ['合成部门乙'], maintainers: ['维护人A'], updatedAt: seconds(2026, 11, 10) },
  { id: '规则丙', kind: 'rule', departments: [], maintainers: [], updatedAt: seconds(2026, 11, 12, 23) },
  { id: '对象甲', kind: 'object', updatedAt: seconds(2026, 11, 2) },
];

// 页面的筛选顺序：先按类型（分段选择），再过表头筛选
const applyFilters = ({ kind = 'all', ...filters }) => rows
  .filter((row) => kind === 'all' || row.kind === kind)
  .filter((row) => studioTableMatches(row, filters, kind))
  .map((row) => row.id);

test('部门、维护人筛选项取自全部规则，有规则没填时提供「未填写」', () => {
  assert.deepEqual(studioOwnerFilters(rows, 'departments'), [
    { text: '合成部门甲', value: '合成部门甲' },
    { text: '合成部门乙', value: '合成部门乙' },
    { text: '未填写', value: STUDIO_UNSET_OWNER },
  ]);
  assert.deepEqual(
    studioOwnerFilters(rows.slice(0, 2), 'maintainers').map((item) => item.value),
    ['维护人A', '维护人B'],
  );
});

test('按部门、维护人单独筛选只留规则，选「未填写」得到没登记的规则', () => {
  assert.deepEqual(applyFilters({ kind: 'rule', departments: ['合成部门甲'] }), ['规则甲']);
  assert.deepEqual(applyFilters({ kind: 'rule', maintainers: ['维护人A'] }), ['规则甲', '规则乙']);
  assert.deepEqual(applyFilters({ kind: 'rule', departments: [STUDIO_UNSET_OWNER] }), ['规则丙']);
  assert.deepEqual(applyFilters({ kind: 'rule', departments: ['合成部门甲', '合成部门乙'] }), ['规则甲', '规则乙']);
  assert.equal(studioOwnerMatches(rows[3], 'departments', STUDIO_UNSET_OWNER), false);
  // 两列不显示时它们的条件不生效
  assert.deepEqual(applyFilters({ departments: ['合成部门甲'] }), ['规则甲', '规则乙', '规则丙', '对象甲']);
});

test('更新时间按日期范围筛选，起止两天都包含，任一端可留空，对所有类型生效', () => {
  const key = studioUpdatedRangeKey('2026-11-02', '2026-11-12');
  assert.deepEqual(studioUpdatedRange(key), { start: '2026-11-02', end: '2026-11-12' });
  assert.deepEqual(applyFilters({ updated: key }), ['规则甲', '规则乙', '规则丙', '对象甲']);
  assert.deepEqual(applyFilters({ updated: studioUpdatedRangeKey('2026-11-03', '') }), ['规则乙', '规则丙']);
  assert.deepEqual(applyFilters({ updated: studioUpdatedRangeKey('', '2026-11-09') }), ['规则甲', '对象甲']);
  assert.deepEqual(applyFilters({ kind: 'object', updated: studioUpdatedRangeKey('', '2026-11-09') }), ['对象甲']);
  assert.equal(studioUpdatedRangeKey('', ''), '');
  assert.equal(studioUpdatedWithin({ updatedAt: undefined }, key), false);
});

test('类型、部门、维护人、更新时间组合筛选取交集', () => {
  assert.deepEqual(applyFilters({
    kind: 'rule',
    maintainers: ['维护人A'],
    updated: studioUpdatedRangeKey('2026-11-05', '2026-11-12'),
  }), ['规则乙']);
  assert.deepEqual(applyFilters({
    kind: 'rule',
    departments: ['合成部门甲'],
    maintainers: ['维护人B'],
    updated: studioUpdatedRangeKey('2026-11-05', ''),
  }), []);
});
