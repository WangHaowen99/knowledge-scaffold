// Studio 表格的列筛选（develop !611）：类型用表格上方的分段选择，责任部门、维护人、最后更新时间在表头筛选。
// 不同列之间取交集，同一列多选取并集，再与搜索叠加。选中条目时表格不卸载（左表右编辑器），筛选和排序原样保留。

export const STUDIO_UNSET_OWNER = '__unset__';
export const RULE_OWNER_COLUMNS = [['departments', '责任部门'], ['maintainers', '维护人']];

// 责任部门、维护人只登记在规则上：类型只选规则时才给出这两列
export function studioRuleColumnsVisible(kind) {
  return kind === 'rule';
}

// 类型不再是规则时两列随之隐藏，它们的筛选一并清掉，免得切回规则时带出旧条件
export function studioTableFilters(filters, kind) {
  if (studioRuleColumnsVisible(kind)) return filters;
  const next = { ...filters };
  RULE_OWNER_COLUMNS.forEach(([key]) => delete next[key]);
  return next;
}

// 规则「责任部门」「维护人」列的筛选项：取全部规则出现过的值，有规则没填时加「未填写」
export function studioOwnerFilters(rows, field) {
  const rules = rows.filter((row) => row.kind === 'rule');
  const values = [...new Set(rules.flatMap((row) => row[field] || []))]
    .sort((left, right) => left.localeCompare(right, 'zh-CN'));
  const options = values.map((value) => ({ text: value, value }));
  if (rules.some((row) => !row[field]?.length)) options.push({ text: '未填写', value: STUDIO_UNSET_OWNER });
  return options;
}

// 选了部门或维护人就只留规则；选「未填写」得到没登记的规则
export function studioOwnerMatches(row, field, value) {
  if (row.kind !== 'rule') return false;
  const values = row[field] || [];
  return value === STUDIO_UNSET_OWNER ? values.length === 0 : values.includes(value);
}

// 表头筛选是否放行一行；filters = {departments?: string[], maintainers?: string[], updated?: 范围键}
export function studioTableMatches(row, filters, kind) {
  if (studioRuleColumnsVisible(kind)) {
    for (const [key] of RULE_OWNER_COLUMNS) {
      const values = filters[key];
      if (values?.length && !values.some((value) => studioOwnerMatches(row, key, value))) return false;
    }
  }
  return !filters.updated || studioUpdatedWithin(row, filters.updated);
}

// 「最后更新时间」按日期范围筛选，起止日期都含当天、任一端可留空；范围存成一个键 `起~止`
export function studioUpdatedRangeKey(start, end) {
  return start || end ? `${start || ''}~${end || ''}` : '';
}

export function studioUpdatedRange(key) {
  const [start = '', end = ''] = String(key || '').split('~');
  return { start, end };
}

// row.updatedAt 是 Unix 秒
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
