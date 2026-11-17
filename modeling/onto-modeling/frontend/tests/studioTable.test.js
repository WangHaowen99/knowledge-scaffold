// Studio 表视图状态、规则筛选与详情返回目标（lib/studioTable.js）。全部为合成数据。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  INITIAL_STUDIO_TABLE_STATE,
  STUDIO_UNSET_OWNER,
  studioEntryReturnView,
  studioFilteredValue,
  studioOwnerFilters,
  studioOwnerMatches,
  studioRuleColumnsVisible,
  studioSortOrder,
  studioTableState,
  studioUpdatedRange,
  studioUpdatedRangeKey,
  studioUpdatedWithin,
} from '../src/lib/studioTable.js';

test('从表格进入详情后返回表格，从图进入返回图', () => {
  assert.equal(studioEntryReturnView('table', 'graph'), 'table');
  assert.equal(studioEntryReturnView('graph', 'table'), 'graph');
});

test('在详情里点关联条目或退出编辑时保留最初的入口', () => {
  assert.equal(studioEntryReturnView('entry', 'table'), 'table');
  assert.equal(studioEntryReturnView('edit', 'table'), 'table');
});

test('表格卸载前的筛选、排序、页码可原样恢复', () => {
  const state = studioTableState(
    { current: 2, pageSize: 15 },
    { kind: ['rule'] },
    { field: 'updatedAt', order: 'descend', column: {} },
  );
  assert.deepEqual(state, {
    current: 2,
    filters: { kind: ['rule'] },
    sorter: { field: 'updatedAt', order: 'descend' },
  });
  assert.deepEqual(studioFilteredValue(state, 'kind'), ['rule']);
  assert.equal(studioSortOrder(state, 'updatedAt'), 'descend');
  assert.equal(studioSortOrder(state, 'out'), null);
});

test('清除筛选和取消排序后不残留旧条件', () => {
  const state = studioTableState({ current: 1 }, { kind: null }, { field: 'updatedAt', order: undefined });
  assert.equal(studioFilteredValue(state, 'kind'), null);
  assert.deepEqual(state.sorter, {});
  assert.equal(studioFilteredValue(INITIAL_STUDIO_TABLE_STATE, 'kind'), null);
  assert.equal(studioSortOrder(INITIAL_STUDIO_TABLE_STATE, 'updatedAt'), null);
});

test('只有类型筛选只选了规则时才给出责任部门、维护人两列', () => {
  const visible = (kind) => studioRuleColumnsVisible(studioTableState({ current: 1 }, { kind }, {}));
  assert.equal(visible(['rule']), true);
  assert.equal(visible(null), false);
  assert.equal(visible(['object']), false);
  assert.equal(visible(['rule', 'object']), false);
  assert.equal(studioRuleColumnsVisible(INITIAL_STUDIO_TABLE_STATE), false);
});

test('类型从规则切走时清掉部门、维护人筛选，切回规则不带出旧条件', () => {
  const ruleState = studioTableState(
    { current: 1 },
    { kind: ['rule'], departments: ['合成部门甲'], maintainers: ['维护人A'], updatedAt: ['2026-11-01~'] },
    {},
  );
  assert.deepEqual(ruleState.filters.departments, ['合成部门甲']);

  // antd 回调时两列还在，会把旧值一起带回来
  const objectState = studioTableState(
    { current: 1 },
    { ...ruleState.filters, kind: ['object'] },
    {},
  );
  assert.deepEqual(objectState.filters, { kind: ['object'], updatedAt: ['2026-11-01~'] });

  const backToRule = studioTableState({ current: 1 }, { ...objectState.filters, kind: ['rule'] }, {});
  assert.equal(studioFilteredValue(backToRule, 'departments'), null);
  assert.equal(studioFilteredValue(backToRule, 'maintainers'), null);
});

const seconds = (year, month, day, hour = 12) => new Date(year, month - 1, day, hour).getTime() / 1000;
const rows = [
  { id: '规则甲', kind: 'rule', departments: ['合成部门甲'], maintainers: ['维护人A', '维护人B'], updatedAt: seconds(2026, 11, 2) },
  { id: '规则乙', kind: 'rule', departments: ['合成部门乙'], maintainers: ['维护人A'], updatedAt: seconds(2026, 11, 10) },
  { id: '规则丙', kind: 'rule', departments: [], maintainers: [], updatedAt: seconds(2026, 11, 12, 23) },
  { id: '对象甲', kind: 'object', updatedAt: seconds(2026, 11, 2) },
];

// 模拟 antd：同一列的多个键取并集，不同列之间取交集
const columnFilters = {
  kind: (value, row) => row.kind === value,
  departments: (value, row) => studioOwnerMatches(row, 'departments', value),
  maintainers: (value, row) => studioOwnerMatches(row, 'maintainers', value),
  updatedAt: (value, row) => studioUpdatedWithin(row, value),
};
const applyFilters = (filters) => rows
  .filter((row) => Object.entries(filters).every(([key, values]) => (
    !values?.length || values.some((value) => columnFilters[key](value, row))
  )))
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
  assert.deepEqual(applyFilters({ departments: ['合成部门甲'] }), ['规则甲']);
  assert.deepEqual(applyFilters({ maintainers: ['维护人A'] }), ['规则甲', '规则乙']);
  assert.deepEqual(applyFilters({ departments: [STUDIO_UNSET_OWNER] }), ['规则丙']);
  assert.deepEqual(applyFilters({ departments: ['合成部门甲', '合成部门乙'] }), ['规则甲', '规则乙']);
});

test('更新时间按日期范围筛选，起止两天都包含，任一端可留空', () => {
  const key = studioUpdatedRangeKey('2026-11-02', '2026-11-12');
  assert.deepEqual(studioUpdatedRange(key), { start: '2026-11-02', end: '2026-11-12' });
  assert.deepEqual(applyFilters({ updatedAt: [key] }), ['规则甲', '规则乙', '规则丙', '对象甲']);
  assert.deepEqual(applyFilters({ updatedAt: [studioUpdatedRangeKey('2026-11-03', '')] }), ['规则乙', '规则丙']);
  assert.deepEqual(applyFilters({ updatedAt: [studioUpdatedRangeKey('', '2026-11-09')] }), ['规则甲', '对象甲']);
  assert.equal(studioUpdatedRangeKey('', ''), '');
  assert.equal(studioUpdatedWithin({ updatedAt: undefined }, key), false);
});

test('类型、部门、维护人、更新时间组合筛选取交集', () => {
  assert.deepEqual(applyFilters({
    kind: ['rule'],
    maintainers: ['维护人A'],
    updatedAt: [studioUpdatedRangeKey('2026-11-05', '2026-11-12')],
  }), ['规则乙']);
  assert.deepEqual(applyFilters({
    departments: ['合成部门甲'],
    maintainers: ['维护人B'],
    updatedAt: [studioUpdatedRangeKey('2026-11-05', '')],
  }), []);
});
