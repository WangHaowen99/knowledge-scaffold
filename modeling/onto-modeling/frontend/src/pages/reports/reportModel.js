// 报告页的纯逻辑：类型色、时间与耗时的写法、修复轮次、「这份报告」的几行、条目状态与链接问题的分类

// 报告类型 → 标签色（界面叫法由服务端 type_label 给）
export const TYPE_TONE = {
  rule: 'red', coverage: 'red', diagram: 'red', link: 'gray', research: 'blue', candidate: 'blue', semantic: 'indigo', trace: 'teal', codegen: 'gray',
};
export const CHECK_LABEL = {
  rule: '规则检查', link: '链接检查', research: '资料分析', semantic: '语义检查', trace: '场景追踪', candidate: '资料分析候选',
  coverage: '覆盖检查', diagram: '流程图校验', codegen: '代码生成',
};
// RP-06 能重新跑的检查
export const RECHECK_KINDS = ['rule', 'semantic', 'link', 'coverage', 'diagram'];

const p2 = (n) => String(n).padStart(2, '0');
export function fmtStamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}
// 「上一份」：和这份同一天写时刻，不同天写日期
export function fmtSince(iso, ref) {
  if (!iso) return '';
  const d = new Date(iso);
  const r = ref ? new Date(ref) : null;
  if (r && d.toDateString() === r.toDateString()) return `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
}

export function fmtDuration(ms) {
  if (ms == null) return '—';
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1).replace(/\.0$/, '') : Math.round(s)} 秒`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return rest ? `${m} 分 ${rest} 秒` : `${m} 分钟`;
}

const MODE = { regression: '回归 · 只查改动', full: '全量', research: '读全 · 出处核验' };

// 标题上面那行小字
export function eyebrow(report, verification) {
  if (!report) return '';
  if (report.type === 'research') {
    const n = report.extras?.total ?? report.extras?.items?.length ?? 0;
    const at = verification?.checked_at || report.extras?.verification?.checked_at || report.at;
    return `资料分析 · ${n} 份资料 · 核验 ${fmtStamp(at)}`;
  }
  if (report.type === 'link') return `${report.scope_text}之间的链接`;
  const sep = /」$/.test(report.scope_text || '') ? '· ' : ' · ';
  if (report.mode === 'regression') return `${report.scope_text}${sep}回归模式 · 只查改动`;
  return `${report.scope_text}${sep}全量`;
}

// 右侧「这份报告」
export function infoPairs(report) {
  if (!report) return [];
  const prev = report.previous ? `${fmtSince(report.previous.at, report.at)} · ${report.previous.summary}` : '—';
  const scope = report.type === 'research' ? `资料区 ${report.extras?.total ?? 0} 份` : report.scope_text;
  return [
    ['检查类型', CHECK_LABEL[report.type] || report.type_label],
    ['范围', scope],
    ['模式', MODE[report.mode] || report.mode],
    ['耗时', fmtDuration(report.duration_ms)],
    ['上一份', prev],
  ];
}

// 右侧「处理进度」
export function progressView(report) {
  const p = report?.progress || { handled: 0, total: 0, note: '' };
  const pct = p.total ? Math.round((p.handled / p.total) * 100) : 0;
  const label = report?.type === 'research' ? '候选已审阅' : '已处理';
  return { text: `${label} ${p.handled} / ${p.total}`, pct, note: p.note };
}

/**
 * 修复轮次（最多 2 轮，第 9 章建议 3）：rounds 里 n 不超过上限的是「第 n 轮」，之后那一根是「现在」。
 * 返回柱子、轮次文字、说明
 */
export function repairView(repair, items) {
  if (!repair) return null;
  const limit = repair.limit || 2;
  const bars = (repair.rounds || []).map((r) => ({ label: r.n <= limit ? `第 ${r.n} 轮` : '现在', count: r.count, cur: r.n > limit }));
  if (bars.length && !bars.some((b) => b.cur)) bars[bars.length - 1].cur = true;
  const done = Math.min(limit, (repair.rounds || []).filter((r) => r.n <= limit).length);
  const stuck = (items || []).filter((i) => i.status === 'expert').length;
  let note = `修复前先用影响分析把上下游一起补；最多修 ${limit} 轮。`;
  if (repair.exhausted && stuck) note += `还剩 ${stuck} 项没收敛，不再自动重试，交给你裁决。`;
  else if (repair.exhausted) note += '已经修满，剩下的按条处理。';
  else note += `现在是第 ${Math.max(1, done)} 轮。`;
  return { bars, title: `修复轮次 ${done} / ${limit}`, exhausted: !!repair.exhausted, note };
}

// 条目状态列
export const ITEM_STATUS = {
  expert: { chip: ['交给你裁决', 'indigo', 'hand'] },
  fixing: { running: '修复中' },
  pending: { chip: ['待处理', 'gray', null] },
  fixed: { chip: ['已修复', 'teal', 'check'] },
};

// 链接检查的问题分类（按检查器规则码）
const LINK_KIND = { 'LINK-ORPHAN': ['孤立', 'amber'], 'LINK-UNUSED': ['未用', 'amber'], 'LINK-DANGLING': ['失效链接', 'red'] };
export function linkKind(item) {
  return LINK_KIND[item?.code] || [item?.level === 'error' ? '错误' : '警告', item?.level === 'error' ? 'red' : 'amber'];
}
export const LINK_STATS = [['orphans', '孤立', '没有任何链接进出'], ['unused', '未用', '没有被任何操作引用'], ['dangling', '失效链接', '指向的元素不存在']];

// 「定位」去哪：条目去 Studio（表格或图谱），流程图去表格里的流程图行
export function locateTarget(location) {
  const t = location?.target;
  if (!t) return null;
  const page = location.page || 'studio-table';
  if (t.type === 'diagram.flowchart') return { page: 'studio-table', query: { entry: `flowchart:${t.key}` } };
  if (t.type) return { page: t.type === 'trace.scenario' ? 'scenarios' : page, query: { key: t.key } };
  return { page, query: { entry: t.id } };
}

// 资料用途、读取计数
export const PURPOSE_LABEL = { goal: '建模目标依据', evidence: '通用业务依据', schema: '数据结构', term: '术语' };
export const PURPOSE_TONE = { goal: 'blue', evidence: 'gray', schema: 'teal', term: 'indigo' };
export function materialCounts(c) {
  if (!c) return '';
  const out = [];
  if (c.objects) out.push(`对象 ${c.objects}`);
  if (c.rules) out.push(`规则 ${c.rules}`);
  if (c.terms) out.push(`术语 ${c.terms}`);
  if (c.fields) out.push(`字段 ${c.fields}`);
  return out.join(' · ');
}
