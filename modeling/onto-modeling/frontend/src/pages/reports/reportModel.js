// 报告页的纯逻辑：时间与耗时的写法、修复轮次、「这份报告」的几行、条目状态与链接问题的分类

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

// 右侧「处理进度」：只给计数和进度条（服务端的 progress.note 是说明槽位，不显示）
export function progressView(report) {
  const p = report?.progress || { handled: 0, total: 0 };
  const pct = p.total ? Math.round((p.handled / p.total) * 100) : 0;
  const label = report?.type === 'research' ? '候选已审阅' : '已处理';
  return { text: `${label} ${p.handled} / ${p.total}`, pct };
}

/**
 * 修复轮次（最多 2 轮，第 9 章建议 3）：rounds 里 n 不超过上限的是「第 n 轮」，之后那一根是「现在」。
 * 返回柱子、轮次文字；到上限还有没收敛的才带一句说明
 */
export function repairView(repair, items) {
  if (!repair) return null;
  const limit = repair.limit || 2;
  const bars = (repair.rounds || []).map((r) => ({ label: r.n <= limit ? `第 ${r.n} 轮` : '现在', count: r.count, cur: r.n > limit }));
  if (bars.length && !bars.some((b) => b.cur)) bars[bars.length - 1].cur = true;
  const done = Math.min(limit, (repair.rounds || []).filter((r) => r.n <= limit).length);
  const stuck = (items || []).filter((i) => i.status === 'expert').length;
  const note = repair.exhausted && stuck ? `还剩 ${stuck} 项没收敛，交给你裁决` : '';
  return { bars, title: `修复轮次 ${done} / ${limit}`, exhausted: !!repair.exhausted, note };
}

// 条目状态列
export const ITEM_STATUS = {
  expert: { chip: ['交给你裁决', 'indigo', 'hand'] },
  fixing: { running: '修复中' },
  pending: { chip: ['待处理', 'gray', null] },
  fixed: { chip: ['已修复', 'teal', 'check'] },
  skipped: { chip: ['已跳过', 'gray', null] },
};

// 链接检查的问题分类（按检查器规则码）
const LINK_KIND = { 'LINK-ORPHAN': ['孤立', 'amber'], 'LINK-UNUSED': ['未用', 'amber'], 'LINK-DANGLING': ['失效链接', 'red'] };
export function linkKind(item) {
  return LINK_KIND[item?.code] || [item?.level === 'error' ? '错误' : '警告', item?.level === 'error' ? 'red' : 'amber'];
}
export const LINK_STATS = [['orphans', '孤立'], ['unused', '未用'], ['dangling', '失效链接']];

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
export const PURPOSE_LABEL = { goal: '建模目标依据', evidence: '通用业务依据', schema: '数据结构', term: '术语', auto: '由助手判断' };
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

// ---------- 规则「来源」里的报告小节（`research/报告.md#小节名`）：打开报告原文并定位 ----------

// 报告路径 → 报告 id（同后端 reports.id_of）：research/20260101-订单.md → research:20260101-订单；reports/x.md → x
export function reportIdOf(path) {
  const p = String(path || '').replace(/\.md$/, '');
  return p.startsWith('research/') ? `research:${p.slice('research/'.length)}` : p.replace(/^reports\//, '');
}

// 原文里要定位的小节标题行：标题全文相同优先，其次标题包含小节名；找不到 -1
export function headingLine(lines, section) {
  const titles = lines.map((line) => (/^#{1,6}\s+(.*)$/.exec(line.trim()) || [])[1]?.trim() ?? null);
  const exact = titles.indexOf(section);
  return exact >= 0 ? exact : titles.findIndex((t) => t !== null && t.includes(section));
}
