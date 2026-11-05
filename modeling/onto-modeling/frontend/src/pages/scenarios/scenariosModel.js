// 场景模拟页的纯逻辑：场景行的状态与筛选、统计卡文字、写入范围只读、页面命令（S-20）、趋势线（契约 AR-02 / AR-09、7.11）
import { sliceName } from '../../state/flow.js';
import { isLocked, lockState } from '../../state/readonly.js';

export const SCENARIO = 'trace.scenario';
export const MOCK = 'trace.mock';

// 行的显示态 → 组件 St 的键（过期优先）
export function rowState(row) {
  if (!row) return 'none';
  if (row.stale) return 'stale';
  const v = row.status?.value || row.fields?.result;
  return { pass: 'pass', fail: 'fail', stale: 'stale', not_run: 'none' }[v] || 'none';
}

export const STATUS_SEG = [['all', '全部'], ['pass', '通过'], ['fail', '失败'], ['stale', '过期']];

export function filterRows(rows, { status = 'all', slice = '' } = {}) {
  return (rows || []).filter((r) => (status === 'all' || rowState(r) === status) && (!slice || r.fields?.slice === slice));
}

export function countRows(rows) {
  const out = { total: 0, pass: 0, fail: 0, stale: 0, none: 0 };
  (rows || []).forEach((r) => { out.total += 1; out[rowState(r)] += 1; });
  return out;
}

// AR-01 的字段枚举 → 显示名
export function enumLabel(type, field, value) {
  const f = type?.schema?.fields?.find((x) => x.name === field);
  return f?.enum?.find((e) => e.value === value)?.label || value || '';
}
export function enumOptions(type, field) {
  const f = type?.schema?.fields?.find((x) => x.name === field);
  return (f?.enum || []).map((e) => ({ value: e.value, label: e.label }));
}

// 「分段」筛选和列：F8 起不分段，字段没有两个以上的取值时不出
export const showSlice = (type) => enumOptions(type, 'slice').length > 1;

export const INPUT_KIND = { custom: ['自定义输入', 'indigo'], mock: ['来自示例数据', 'line'] };

const pad = (n) => String(n).padStart(2, '0');
export function lastRun(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 失败行悬停：预期与实际
export function failHint(row) {
  const f = row?.fields || {};
  if (!f.expected && !f.actual) return f.fail_reason || '推演结果和预期不一致';
  return `预期 ${f.expected || '—'}，实得 ${f.actual || '—'}${f.fail_reason ? `：${f.fail_reason}` : ''}`;
}

// ---------- 统计卡 ----------
export function passRate(summary, counts) {
  const total = summary?.total ?? counts?.total ?? 0;
  const pass = summary?.by?.find((b) => b.value === 'pass')?.count ?? counts?.pass ?? 0;
  const pct = total ? Math.round((pass / total) * 100) : 0;
  const delta = summary?.delta_vs_previous_pct;
  const d = delta == null ? '' : ` · 比上一版 ${delta >= 0 ? '+' : ''}${delta}%`;
  return { pct, desc: `${pass} / ${total}${d}` };
}

export function failDesc(rows) {
  const fails = (rows || []).filter((r) => rowState(r) === 'fail');
  if (!fails.length) return '没有失败的场景';
  return fails.length === 1 ? fails[0].title : `${fails[0].title} 等 ${fails.length} 个`;
}

export function staleDesc(rows) {
  const src = [...new Set((rows || []).filter((r) => rowState(r) === 'stale').map((r) => r.fields?.stale_source).filter(Boolean))];
  if (!src.length) return (rows || []).some((r) => rowState(r) === 'stale') ? '资料或条目改过' : '没有过期场景';
  return `来自 ${src.join('、')}`;
}

// 示例数据统计卡的说明：只在还没生成时给一句（生成方式在表格「生成方式」列里）
export function mockDesc(mockRows) {
  return (mockRows || []).length ? null : '还没有生成';
}

export function skippedNote(skipped) {
  const labels = (skipped || []).map((s) => s.entry?.name).filter(Boolean);
  if (!labels.length) return null;
  const reasons = [...new Set((skipped || []).map((s) => s.reason).filter(Boolean))];
  return { labels: labels.join('、'), reason: reasons.length === 1 ? reasons[0] : '' };
}

// 趋势线（设计稿 spark）：按 trend 的值画折线和面积
export function sparkPaths(values, w = 110, h = 34) {
  const pts = (values || []).filter((v) => typeof v === 'number');
  if (pts.length < 2) return null;
  const mx = Math.max(...pts);
  const mn = Math.min(...pts);
  const k = (w - 4) / (pts.length - 1);
  const xy = pts.map((v, i) => [2 + i * k, h - 3 - ((v - mn) / ((mx - mn) || 1)) * (h - 6)]);
  const line = xy.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  return { line, area: `${line} L ${w - 2} ${h} L 2 ${h} Z` };
}

// ---------- 写入范围只读 ----------
export const scenarioPath = (type, key) => (type?.path || 'ext/trace/scenarios/{key}.md').replace('{key}', key);

export function rowLocked(lock, row, type) {
  if (!row || lockState(lock) === 'none') return false;
  return !!row.locked || isLocked(lock, { type: SCENARIO, key: row.key }) || isLocked(lock, { path: scenarioPath(type, row.key) });
}

// 锁来自批量推演（场景模拟节点）
export const fromBatch = (lock) => /^trace\./.test(lock?.node?.id || '');

// 页面上那条只读条的文字：{ running: [标题, 说明], pending: [标题, 说明] }；说明只在待确认时给推演计数
export function lockBarText(lock, lockedRows) {
  const n = lockedRows.length;
  const c = countRows(lockedRows);
  const sum = `通过 ${c.pass} · 失败 ${c.fail} · 过期 ${c.stale}`;
  const batch = fromBatch(lock);
  const name = sliceName(lock?.slice?.id, lock?.slice?.name);
  const slice = name ? `「${name}」` : '这部分内容';
  return {
    running: batch ? [`助手正在推演 ${n} 个场景，暂时只读`] : [`助手正在改${slice}，${n} 个场景暂时只读`],
    pending: [`和助手这一轮的修改有冲突，${n} 个场景暂时只读`, sum],
  };
}

// 锁住的行上那把锁的悬浮说明
export function rowLockPop(lock) {
  const st = lockState(lock);
  if (st === 'pending_confirm') return '有冲突，暂时只读';
  if (st === 'autobuild_slice') return '自主构建在改，暂时只读';
  return '助手在改，暂时只读';
}

// ---------- 页面命令（S-20）：只发命令，助手在工作台对话里跑 ----------
export const batchCommand = (total) => ({ command: '/ontology-trace-batch', args: { scope: 'all' }, text: `批量推演 ${total} 个场景` });
export const refreshCommand = (staleRows) => ({ command: '/ontology-trace-batch', args: { keys: staleRows.map((r) => r.key) }, text: `补齐 ${staleRows.length} 个过期场景` });
export const walkCommand = (row) => ({ command: '/ontology-trace-interactive', args: { scenario: row.key }, text: `交互模拟「${row.title}」` });
export const mockCommand = () => ({ command: '/ontology-mock', args: { by: 'scenario' }, text: '按场景补生成示例数据' });

// 页头「交互模拟」走哪个场景：选中的，没有就第一个失败的，再没有就第一个
export function walkTarget(rows, selectedKey) {
  const list = rows || [];
  return list.find((r) => r.key === selectedKey) || list.find((r) => rowState(r) === 'fail') || list[0] || null;
}
