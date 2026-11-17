// Studio 表视图的筛选、排序、分页状态与详情返回目标。
// 打开条目详情时表格会卸载，antd Table 自带的内部状态随之丢失，所以由 StudioView 持有。

export const INITIAL_STUDIO_TABLE_STATE = { current: 1, filters: {}, sorter: {} };
export const STUDIO_UNSET_OWNER = '__unset__';
const RULE_OWNER_FILTERS = ['departments', 'maintainers'];

// 只有图、表是进入详情的入口；从详情里的关联条目、编辑器跳转时沿用最初的入口
export function studioEntryReturnView(currentView, previousReturnView) {
  return currentView === 'graph' || currentView === 'table' ? currentView : previousReturnView;
}

export function studioTableState(pagination, filters, sorter) {
  const single = Array.isArray(sorter) ? sorter[0] : sorter;
  const state = {
    current: pagination?.current || 1,
    filters: { ...filters },
    sorter: single?.order ? { field: single.field, order: single.order } : {},
  };
  // 类型不再只选规则时两列随之隐藏，它们的筛选一并清掉，免得切回规则时带出旧条件
  if (!studioRuleColumnsVisible(state)) RULE_OWNER_FILTERS.forEach((key) => delete state.filters[key]);
  return state;
}

// 责任部门、维护人只登记在规则上：类型筛选只选了规则时才给出这两列
export function studioRuleColumnsVisible(state) {
  const kinds = state.filters.kind || [];
  return kinds.length === 1 && kinds[0] === 'rule';
}

export function studioFilteredValue(state, key) {
  return state.filters[key] || null;
}

export function studioSortOrder(state, field) {
  return state.sorter.field === field ? state.sorter.order : null;
}

// 规则「责任部门」「维护人」列的筛选项：取全部规则出现过的值，有规则没填时加「未填写」
export function studioOwnerFilters(nodes, field) {
  const rules = nodes.filter((node) => node.kind === 'rule');
  const values = [...new Set(rules.flatMap((node) => node[field] || []))]
    .sort((left, right) => left.localeCompare(right, 'zh-CN'));
  const options = values.map((value) => ({ text: value, value }));
  if (rules.some((node) => !node[field]?.length)) options.push({ text: '未填写', value: STUDIO_UNSET_OWNER });
  return options;
}

// 选了部门或维护人就只留规则；同一列多选取并集，不同列之间由 antd 取交集
export function studioOwnerMatches(row, field, value) {
  if (row.kind !== 'rule') return false;
  const values = row[field] || [];
  return value === STUDIO_UNSET_OWNER ? values.length === 0 : values.includes(value);
}

// 「最后更新时间」按日期范围筛选，起止日期都含当天、任一端可留空。
// 范围存成一个筛选键：antd 对同一列的多个键取并集，拆成两个键会变成“满足任一端”
export function studioUpdatedRangeKey(start, end) {
  return start || end ? `${start || ''}~${end || ''}` : '';
}

export function studioUpdatedRange(key) {
  const [start = '', end = ''] = String(key || '').split('~');
  return { start, end };
}

export function studioUpdatedWithin(row, key) {
  if (!Number.isFinite(row.updatedAt)) return false;
  const { start, end } = studioUpdatedRange(key);
  const time = row.updatedAt * 1000;
  if (start && time < localDayStart(start)) return false;
  return !end || time < localDayStart(end, 1);
}

// 按浏览器时区取某天 0 点，与「最后更新时间」列的显示一致
function localDayStart(date, offsetDays = 0) {
  const [year, month, day] = date.split('-').map(Number);
  return new Date(year, month - 1, day + offsetDays).getTime();
}
