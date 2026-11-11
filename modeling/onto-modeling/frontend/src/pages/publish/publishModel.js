// 发布知识包（设计稿 publish）的纯逻辑：门禁链每一关的样子、数据绑定问题与修正选项、能不能降级、发布记录的说法
import { isEvidencePack } from '../../state/evidence.js';

// 门禁卡片：样式类（设计稿 mr-gate ok / bad / warn）、图标、状态标、动作按钮
export function gateView(g) {
  const cls = g.status === 'fail' ? 'bad' : g.status === 'pass' || g.status === 'warn' ? 'ok' : 'warn';
  let icon = 'shield';
  if (g.id === 'data_binding') icon = cls === 'bad' ? 'alert' : 'db';
  else if (g.id === 'solver_contract') icon = 'badge';
  else if (g.id === 'package_structure') icon = 'pkg';
  else if (/anchor|evidence/.test(g.id)) icon = 'quote';
  else if (g.source?.kind === 'flow') icon = 'check';
  else if (g.source?.kind === 'pack') icon = 'gate';
  const chip = cls === 'ok' ? { k: 'pass', label: '通过' } : cls === 'bad' ? { k: 'fail', label: '不通过' } : { k: 'blocked', label: '未通过' };
  let action = null;
  if (g.action?.type === 'start_node') action = { label: '去运行', action: g.action };
  else if (g.action) action = { label: '去处理', action: g.action };
  return { cls, icon, chip, action };
}

export function blockingSummary(check) {
  const n = check?.blocking_count || 0;
  return n ? { text: `${n} 项挡住发布`, tone: 'red' } : { text: '全部通过', tone: 'teal' };
}

// 「发布为知识包」按钮置灰时的说明
export const publishBlockedTitle = (check) => (check?.blocking_count ? `${check.blocking_count} 项未通过` : '');

// 求解契约要端到端验证的包：发布时要确认「尚未验证」（验证在发布后的独立评测里做，ADR-0091）
export const needsSolverAck = (check) => Boolean((check?.gates || []).find((g) => g.id === 'solver_contract')?.requires_acknowledgement);

// 门禁链里显示哪些关：证据源不可用时，依赖证据源的包带来的门禁（如证据出处检查）不出现
export const visibleGates = (check, evidenceOn) => (check?.gates || []).filter((g) => evidenceOn || !isEvidencePack(g.source?.pack));

export const formatLabel = (check) => (check?.format === 'solver' ? '求解知识包' : '普通知识包');

// 数据绑定问题：状态标与下面的一行说明
const ISSUE_CHIP = {
  declared_missing: ['声明的表不存在', 'red'],
  no_columns: ['表里没有一列对得上', 'red'],
  declared_unverifiable: ['无表可核对', 'amber'],
};
export function issueView(issue) {
  const chip = ISSUE_CHIP[issue.kind] || ['对不上', 'red'];
  const d = issue.detail;
  const note = issue.message || (d ? `属性英文名 ${d.attr_columns.join('、')}；表列 ${d.table_columns.join('、')}` : '');
  return { chip, note };
}

// 「改为现有表」下拉：候选表在前（带对上的列数），其余登记过的表在后；收起时显示第一个候选
export function fixOptions(issue, tables) {
  const cand = (issue.candidates || []).map((c) => ({ value: c.table, label: `改为 ${c.table}`, hint: c.matched ? `对上 ${c.matched} 列` : '' }));
  const rest = (tables || []).filter((t) => !cand.some((c) => c.value === t) && t !== issue.declared).map((t) => ({ value: t, label: `改为 ${t}`, hint: '' }));
  return { placeholder: cand[0]?.label || '改为现有数据表', options: [...cand, ...rest] };
}
export const canFix = (issue, fix) => (issue.fixes || []).includes(fix);

// 能不能降级发布：降级只放宽数据绑定，还有别的门禁挡着时降级也发不了（R7）。text 是置灰按钮的悬停
export function degradeNote(check) {
  const others = (check?.gates || []).filter((g) => g.blocking && !g.degradable);
  if (!others.length) return { blocked: false, text: '' };
  return { blocked: true, text: `${others.map((g) => g.title).join('、')}未通过` };
}
export const unboundObjects = (check) => (check?.binding?.issues || []).map((x) => x.object);

// 422 gate_blocked：「合规检查未通过，仍不能发布」
export function gateBlockedText(details) {
  const t = (details?.gates || []).map((g) => g.title).filter(Boolean);
  return t.length ? `${t.join('、')}未通过，仍不能发布` : '门禁链有未通过的项，仍不能发布';
}

// 发布记录：门禁一列、评测服务一列
export function releaseGate(rec) {
  if (rec.gates?.degraded) {
    const n = rec.gates.unbound_objects?.length || 0;
    return { chip: ['降级发布', 'amber'], note: n ? `${n} 个对象不绑定数据表` : '' };
  }
  return { chip: ['全部通过', 'blue'], note: '' };
}

// EP-19 release.updated：按包版本号合并，新的在前
export function mergeRelease(list, rec) {
  const out = (list || []).filter((r) => r.version !== rec.version);
  out.push(rec);
  return out.sort((a, b) => String(b.at).localeCompare(String(a.at)));
}

// 版本下拉：当前分支上的版本
export const versionOptions = (items) => (items || []).map((v) => ({ value: v.commit, label: v.version_label }));

// 版本号由 Hub 分配；本地标签没写成时（local_record_pending）只有包版本
export function releasedText(rec) {
  const label = rec.version_label ? `${rec.version_label} · ` : '';
  return `已发布 ${label}包版本 ${rec.version}${rec.gates?.degraded ? '（降级发布）' : ''}`;
}
