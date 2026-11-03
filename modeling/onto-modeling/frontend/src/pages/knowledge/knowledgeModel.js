// 知识沉淀页的纯逻辑：条目的显示文字、筛选参数、表单校验、冲突与删除说明、提名检查项、只读判定（契约 K-01～K-08、7.5）
import { isLocked, lockState } from '../../state/readonly.js';

// 类型：界面名与 chip 色（设计稿 MR_KN 的 tone）
export const KN_TYPES = {
  correction: ['纠错', 'red'],
  convention: ['惯例', 'teal'],
  term: ['术语', 'blue'],
  domain_rule: ['领域规则', 'indigo'],
};
export const TYPE_ITEMS = [['correction', '纠错'], ['convention', '惯例'], ['term', '术语'], ['domain_rule', '领域规则']];
export const TYPE_FILTER = [{ value: '', label: '全部类型' }, ...TYPE_ITEMS.map(([value, label]) => ({ value, label }))];

export const LAYER_BADGE = { project: ['项目', 'mr-ly-p'], industry: ['行业包', 'mr-ly-i'] };
export const LAYER_CHIP = { project: ['项目层', 'blue'], industry: ['行业包层', 'teal'] };
export const CONFIDENCE = { high: ['高', 'teal'], medium: ['中', 'amber'], low: ['低', 'gray'] };
export const KIND_LABEL = { object: '对象', operation: '操作', rule: '规则', metric: '指标', script: '脚本', flowchart: '流程图' };

// 写入范围锁里的知识条目：{ type: 'core.knowledge', key: <id> }
export const KNOWLEDGE_TYPE = 'core.knowledge';
export const knowledgeRef = (entry) => ({ type: KNOWLEDGE_TYPE, key: entry.id, title: entry.title });

const pad = (n) => String(n).padStart(2, '0');
export function mmdd(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
export function mmddhhmm(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : `${mmdd(iso)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 范围：「全局」或「对象：陈列量标准」；多个条目时「对象：A 等 2 个」
export function scopeLabel(scope) {
  if (!scope || scope.kind !== 'entries' || !scope.entries?.length) return '全局';
  const [first, ...rest] = scope.entries;
  const kind = KIND_LABEL[first.kind] || '条目';
  return rest.length ? `${kind}：${first.name} 等 ${scope.entries.length} 个` : `${kind}：${first.name}`;
}

// 列表里的来源：图标 + 文字（对话带日期）
export function sourceMeta(source) {
  if (!source) return { icon: 'info', text: '—' };
  if (source.kind === 'conversation') return { icon: 'msg', text: [source.label || '对话', mmdd(source.at)].filter(Boolean).join(' ') };
  if (source.kind === 'material') return { icon: 'filetext', text: source.label };
  if (source.kind === 'pack') return { icon: 'pkg', text: source.label };
  return { icon: 'edit', text: [source.label || '手动新增', mmdd(source.at)].filter(Boolean).join(' ') };
}

// K-01 查询参数：默认值不带
export function listQuery({ layer = 'all', type = '', q = '', cursor = null, limit = 20 } = {}) {
  const out = { limit };
  if (layer && layer !== 'all') out.layer = layer;
  if (type) out.type = type;
  if (q.trim()) out.q = q.trim();
  if (cursor) out.cursor = cursor;
  return out;
}

// 分层筛选：[值, 文字, 悬停]；「项目优先」这层意思放在「项目」那一格的悬停里（R9，分层条整块去掉）
export function layerSeg(counts) {
  const c = counts || {};
  return [['all', `全部 ${c.all ?? 0}`], ['project', `项目 ${c.project ?? 0}`, '同名时项目条目优先'], ['industry', `行业包 ${c.industry ?? 0}`]];
}

// 行业包层：提名回流、覆盖说明要用行业包的名称与包名
export function layerInfo(resp) {
  const ind = (resp?.layers || []).find((l) => l.layer === 'industry') || { count: 0, label: '' };
  return { industry: { count: ind.count, label: ind.label || '未启用行业包', pack: ind.packs?.[0] || null } };
}

export function totalFor(counts, layer) {
  if (!counts) return 0;
  return layer === 'project' ? counts.project : layer === 'industry' ? counts.industry : counts.all;
}

export function footText(shown, total) {
  return shown < total ? `显示前 ${shown} 条 · 共 ${total} 条` : `共 ${total} 条`;
}

// 本项目还没有沉淀任何条目：显示空状态（行业包条目不算「沉淀」）
export const isEmptyProject = (resp) => !!resp && (resp.layers || []).find((l) => l.layer === 'project')?.count === 0;

// ---------- 写入范围只读 ----------
export function entryLocked(lock, entry) {
  if (!entry || lockState(lock) === 'none') return false;
  return !!entry.locked || isLocked(lock, knowledgeRef(entry));
}

// 「编辑」「删除」禁用时的说明、详情卡上的只读条文字
export function lockCopy(lock) {
  const st = lockState(lock);
  if (st === 'none') return null;
  const slice = lock.slice?.name ? `「${lock.slice.name}」这一段` : '这部分内容';
  if (st === 'pending_confirm') {
    return { pop: '有冲突，暂不能编辑', text: { pending: ['这条知识和助手这一轮的修改有冲突：选好保留哪边前只读'] } };
  }
  if (st === 'autobuild_slice') {
    const name = lock.slice?.name ? `「${lock.slice.name}」` : '这一段';
    return { pop: '自主构建在改，暂不能编辑', text: { running: [`自主构建正在做${name}，这条知识暂时只读`] } };
  }
  return { pop: '助手在改，暂不能编辑', text: { running: [`助手正在改${slice}：这条知识这一轮结束前只读`] } };
}

// ---------- 新增 / 编辑表单 ----------
export function emptyForm(prefill = {}) {
  return { title: '', type: 'domain_rule', scope: { kind: 'global' }, background: '', rule: '', confidence: 'medium', ...prefill };
}

export function formFromDetail(d) {
  return {
    title: d.title || '', type: d.type || 'domain_rule', scope: d.scope || { kind: 'global' },
    background: d.background_md || '', rule: d.rule_md || '', confidence: d.confidence || 'medium',
  };
}

// 行业包条目：在项目里写同名条目覆盖（标题、类型、范围照抄，内容自己写）
export function overrideForm(d) {
  return emptyForm({ title: d.title, type: d.type, scope: d.scope || { kind: 'global' }, background: `覆盖行业包条目「${d.title}」：` });
}

export function validateForm(f) {
  const errs = {};
  const title = (f.title || '').trim();
  if (!title) errs.title = '写一个条目名称';
  else if (title.length > 60) errs.title = '名称不超过 60 个字';
  if (!(f.rule || '').trim()) errs.rule = '规则不能为空';
  if (f.scope?.kind === 'entries' && !f.scope.entries?.length) errs.scope = '选一个适用范围';
  return errs;
}

export function formBody(f, { base_revision, source } = {}) {
  const body = {
    title: f.title.trim(), type: f.type, scope: f.scope, rule_md: f.rule.trim(), confidence: f.confidence || 'medium',
  };
  if ((f.background || '').trim()) body.background_md = f.background.trim();
  if (base_revision) body.base_revision = base_revision;
  if (source) body.source = source;
  return body;
}

// ---------- 冲突检查（K-06） ----------
const short = (t) => String(t || '').split(/[，,；;]/)[0];
export { short as shortTitle };

// 冲突检查卡右上的计数（点开明细）；还没检查过时不显示
export function conflictChip(check) {
  if (!check?.checked_at) return null;
  const n = check.conflicts?.length || 0;
  return n ? { text: `${n} 处`, tone: 'amber', n } : { text: '没有冲突', tone: 'teal', n: 0 };
}

export function conflictLine(c) {
  const a = short(c.project_entry?.title);
  const b = short(c.industry_entry?.title);
  if (c.kind === 'duplicate') return `「${a}」与「${b}」重复`;
  if (c.kind === 'broken_ref') return `「${a}」引用的条目已不存在`;
  return `「${a}」与行业包「${b}」口径不同`;
}

// 明细弹窗里的一行：项目「…」↔ 行业包「…」：项目优先，已生效
export function conflictDetail(c) {
  if (c.kind !== 'scope_conflict') return conflictLine(c);
  return `项目「${short(c.project_entry?.title)}」↔ 行业包「${short(c.industry_entry?.title)}」：${c.effective === 'project' ? '项目优先，已生效' : '行业包优先'}`;
}

export const conflictKindTitle = (kind) => ({ scope_conflict: '口径冲突', duplicate: '重复条目', broken_ref: '失效引用' }[kind] || '冲突');

export function reviewCommand(c) {
  const text = `复核知识口径冲突「${short(c.project_entry?.title)} / ${short(c.industry_entry?.title)}」`;
  return { command: '/knowledge-review', args: { project_entry: c.project_entry?.id, industry_entry: c.industry_entry?.id }, text };
}

export function materialsLine(check) {
  return `与资料原文一致 ${check?.consistent_with_materials ?? 0} 条 · 找不到出处 ${check?.unsourced ?? 0} 条`;
}

export const countsLine = (check) => `重复条目 ${check?.duplicates ?? 0} · 失效引用 ${check?.broken_refs ?? 0}`;

export function checkSub(check) {
  return check?.checked_at ? mmddhhmm(check.checked_at) : null;
}

export function checkModalSub(check) {
  if (!check?.checked_at) return '还没有检查过';
  const sec = Math.max(1, Math.round((check?.duration_ms || 0) / 1000));
  return `${check?.total_entries ?? 0} 条 · 用时 ${sec} 秒`;
}

// ---------- 删除（K-05） ----------
export function deleteImpact(d) {
  const n = d?.used_by?.length || 0;
  const head = n ? `引用它的 ${n} 个产物下次刷新时不再带上这条知识` : '目前没有产物引用这条知识';
  const restored = d?.overrides?.entry_title ? `；被覆盖的行业包条目「${d.overrides.entry_title}」会重新生效` : '';
  return `${head}${restored}。`;
}

// ---------- 详情 ----------
export function usedByLabel(ref) {
  if (ref.type) {
    if (ref.type === 'trace.scenario') return /^场景/.test(ref.title) ? ref.title : `场景 ${ref.title}`;
    return ref.title;
  }
  return `${KIND_LABEL[ref.kind] || '条目'} ${ref.name}`;
}

// 点了去哪：本体条目去 Studio 表格，场景去场景走查（按 key 定位）
export function usedByTarget(ref) {
  if (ref.type === 'trace.scenario') return { page: 'scenarios', query: { key: ref.key } };
  if (ref.type) return null;
  return { page: 'studio-table', query: { entry: ref.id } };
}

export function quoteMeta(quote, convTitles = {}) {
  if (!quote) return '';
  const where = quote.conversation_id ? `对话「${convTitles[quote.conversation_id] || '已删除的对话'}」` : '';
  return [where, mmddhhmm(quote.at), quote.by].filter(Boolean).join(' · ');
}

// 正文：只认段落、列表、[[条目]] 和 **粗体**；开头的「## 规则」「## 背景」小标题去掉（标题已经写在外面）
export function richBlocks(md) {
  const lines = String(md || '').replace(/\r/g, '').split('\n');
  while (lines.length && /^#{1,6}\s/.test(lines[0].trim())) lines.shift();
  const blocks = [];
  let para = [];
  const flush = () => { if (para.length) { blocks.push({ t: 'p', parts: inlineParts(para.join(' ')) }); para = []; } };
  lines.forEach((raw) => {
    const s = raw.trim();
    if (!s) { flush(); return; }
    const li = s.match(/^(?:[-*]|\d+\.)\s+(.*)$/);
    if (li) { flush(); blocks.push({ t: 'li', parts: inlineParts(li[1]) }); return; }
    if (/^#{1,6}\s/.test(s)) { flush(); blocks.push({ t: 'h', parts: inlineParts(s.replace(/^#+\s*/, '')) }); return; }
    para.push(s);
  });
  flush();
  return blocks;
}

export function inlineParts(text) {
  const out = [];
  const re = /\[\[([^\]]+?)\]\]|\*\*([^*]+?)\*\*/g;
  const s = String(text || '');
  let pos = 0;
  let m;
  while ((m = re.exec(s))) {
    if (m.index > pos) out.push({ t: 'text', v: s.slice(pos, m.index) });
    out.push(m[1] !== undefined ? { t: 'link', v: m[1] } : { t: 'b', v: m[2] });
    pos = m.index + m[0].length;
  }
  if (pos < s.length) out.push({ t: 'text', v: s.slice(pos) });
  return out;
}

// 脱敏后的条目：按 highlights 切成普通 / 标黄片段
export function markSegments(text, highlights) {
  const s = String(text || '');
  const hs = (highlights || []).filter((h) => h.end > h.start).sort((a, b) => a.start - b.start);
  const out = [];
  let pos = 0;
  hs.forEach((h) => {
    if (h.start > pos) out.push({ v: s.slice(pos, h.start), mark: false });
    out.push({ v: s.slice(Math.max(pos, h.start), h.end), mark: true });
    pos = Math.max(pos, h.end);
  });
  if (pos < s.length) out.push({ v: s.slice(pos), mark: false });
  return out;
}

// ---------- 提名（K-07） ----------
export const CHECK_ITEMS = { customer_name: '单位名称', internal_id: '内部编号', amount: '金额', secret_or_pii: '疑似密钥 / 个人信息' };
export function checkRows(checks) {
  return (checks || []).map((c) => ({
    label: CHECK_ITEMS[c.item] || c.item,
    found: c.found,
    result: c.found ? [`发现 ${c.found} 处`, 'amber'] : ['未发现', 'teal'],
    action: c.found && c.action ? c.action : '—',
  }));
}

export const nominateTitle = (packLabel) => `提名回流到${packLabel ? `${packLabel}行业包` : '行业包'}`;

// 覆盖说明：覆盖 **零售连锁行业包 / 陈列上限取值**（行业条目写的是「按品类取」）
export function overrideParts(overrides, packLabel) {
  if (!overrides) return null;
  return {
    target: `${packLabel || overrides.pack}行业包 / ${overrides.entry_title}`,
    note: overrides.note ? `（${overrides.note}）` : '',
  };
}
