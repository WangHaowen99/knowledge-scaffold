// Studio 图谱与表格页的纯逻辑：条目类型、写入范围锁的说法、草稿改动摘要、提交结果文案、证据标签
import { sliceName } from '../../state/flow.js';
import { isLocked, lockState } from '../../state/readonly.js';

// 条目类型：界面叫法、标签色、图谱里的形状与颜色（同设计稿 SD_COL、表格 T）
export const KIND_META = {
  object: { label: '对象', tone: 'blue', color: '#2563EB', shape: 'circle' },
  operation: { label: '操作', tone: 'teal', color: '#0D9488', shape: 'square' },
  rule: { label: '规则', tone: 'amber', color: '#D97706', shape: 'diamond' },
  metric: { label: '指标', tone: 'indigo', color: '#4F46E5', shape: 'circle' },
  flowchart: { label: '流程图', tone: 'line', color: '#4F46E5', shape: 'branch', icon: 'branch' },
  script: { label: '脚本', tone: 'gray', color: '#64748B', shape: 'square' },
};
export const KIND_ORDER = ['object', 'operation', 'rule', 'metric', 'flowchart', 'script'];
export const kindLabel = (k) => KIND_META[k]?.label || k;

// 图谱小圆点 / 方块 / 菱形的样式（统计条与图例共用）
export function kindDotStyle(kind, size = 10) {
  const m = KIND_META[kind] || KIND_META.object;
  const base = { display: 'inline-block', width: size, height: size, background: m.color, flex: 'none' };
  if (m.shape === 'square') return { ...base, borderRadius: size <= 8 ? 2 : 3 };
  if (m.shape === 'diamond') return { ...base, borderRadius: size <= 8 ? 1 : 2, transform: size <= 8 ? 'rotate(45deg)' : 'rotate(45deg) scale(.85)' };
  return { ...base, borderRadius: '50%' };
}

// EntryRow.status → 状态列；editing（本人有未提交草稿）单独画成「编辑中」
export function rowStatus(row) {
  if (row.status === 'editing') return { chip: ['编辑中', 'blue', 'edit'] };
  if (row.needs_review) return { st: 'needs_review' };
  return { st: { confirmed: 'confirmed', pending: 'pending', stale: 'stale', draft: 'draft' }[row.status] || 'confirmed' };
}

// 条目表：类型分段 + 关键字（名称、Key 子串，不分大小写）
export function filterRows(rows, kind, q) {
  const s = (q || '').trim().toLowerCase();
  return (rows || []).filter((r) => (kind === 'all' || r.kind === kind)
    && (!s || `${r.name}\n${r.key}`.toLowerCase().includes(s)));
}

// 点一行时的地址：带着对话链接给的搜索词（q），地址里的 q 不变，搜索框就不会被清掉
export function rowQuery(query, id) {
  return query?.q ? { entry: id, q: query.q } : { entry: id };
}

// 类型分段的项：全部在前，流程图只在图示包启用时出现（counts.flowchart 为 null 表示没启用）
export function kindSegments(counts, { flowchartEnabled } = {}) {
  const c = counts || {};
  const out = [{ k: 'all', label: '全部', n: c.all ?? 0 }];
  KIND_ORDER.forEach((k) => {
    if (k === 'flowchart' && (!flowchartEnabled || c.flowchart == null)) return;
    out.push({ k, label: KIND_META[k].label, n: c[k] ?? 0, plugin: k === 'flowchart' });
  });
  return out;
}

// ---------- 写入范围锁（详设 4.3、契约 7.6 ScopeLock） ----------
const SHORT = { assistant_running: '运行中', pending_confirm: '有冲突', autobuild_slice: '自主构建中' };

/**
 * 条目在写入范围里时的各处说法；不在范围里返回 null。
 * target：EntryRef / ArtifactRef；name：界面上的条目名；affected：等你决定的决策卡改动预览里列出的条目 id
 */
export function lockCopy(lock, target, name, { affected = [] } = {}) {
  if (!target || !isLocked(lock, target)) return null;
  const state = lockState(lock);
  // F8 之前按段跑的自主构建锁带分段名；不分段（「全部」）时不提段
  const unit = sliceName(lock.slice?.id, lock.slice?.name);
  const decided = !!target.id && affected.includes(target.id);
  const until = state === 'assistant_running' ? '这一轮结束前只读' : state === 'pending_confirm' ? '选好保留哪边前只读' : '自主构建做完这部分前只读';
  const pop = decided ? '等你决定的口径会改到它，决定前只读' : `在${unit ? `「${unit}」的` : ''}写入范围里，${until}`;
  if (state === 'assistant_running') {
    return { state, chip: SHORT[state], pop, title: '助手正在改这部分内容，这一轮结束前只读', sub: '', action: { kind: 'conversation', label: '回到对话' } };
  }
  if (state === 'pending_confirm') {
    return { state, chip: SHORT[state], pop, title: '这部分内容和助手这一轮的修改有冲突，选好保留哪边前只读', sub: '', action: { kind: 'confirm', label: '去处理' } };
  }
  return { state, chip: SHORT[state] || '只读', pop, title: unit ? `自主构建正在做「${unit}」，完成前只读` : '自主构建正在改这部分内容，完成前只读', sub: '', action: { kind: 'run', label: '查看运行' } };
}

// 锁定条按钮去哪：运行中回到对话；合入冲突去待处理选保留哪边（F3）；自主构建去运行页
export function lockActionTarget(action, lock) {
  if (!action) return null;
  if (action.kind === 'run') return { page: 'l4-run', query: lock?.run_id ? { run: lock.run_id } : undefined };
  if (action.kind === 'confirm') return { page: 'inbox' };
  return { page: 'wb-l2', query: lock?.conversation_id ? { c: lock.conversation_id } : undefined };
}

// ---------- 结构化编辑：表单与草稿 ----------
export function formFromEntry(entry) {
  if (!entry) return null;
  return {
    name: entry.name || '',
    key: entry.key || '',
    description: entry.description || '',
    fields: (entry.fields || []).map((f) => ({
      name: f.name, column: f.column, type: f.type, unit: f.unit,
      evidence_anchor: f.evidence?.anchor ?? null,
      expert_confirmed: !!f.evidence?.expert_confirmed,
    })),
    // 本条目自己声明的关联：关系声明里去掉反向的（反向的在「被谁引用」里，归对方的文件管）
    relations: (entry.relations?.declared || []).map((r) => r.target.id)
      .filter((id, i, a) => a.indexOf(id) === i && !(entry.backlinks || []).some((b) => b.source?.id === id)),
    data_source: entry.data_source && entry.data_source.kind !== 'unset'
      ? { kind: entry.data_source.kind, value: entry.data_source.value ?? undefined }
      : { kind: 'none' },
    sections: Object.fromEntries((entry.sections || []).filter((s) => s.level >= 2).map((s) => [s.title, s.content_md])),
  };
}

const sameField = (a, b) => a.column === b.column && a.name === b.name && a.type === b.type
  && (a.evidence_anchor ?? null) === (b.evidence_anchor ?? null) && !!a.expert_confirmed === !!b.expert_confirmed;

/**
 * 草稿相对当前版本改了什么，返回一条条说法（放弃草稿弹窗、提交通过的提示用）。
 * 属性按英文名（真实列名）配对，列名改了算「修改」；名称、Key、描述、关联、数据来源、章节各算一处
 */
export function summarizeFormChanges(entry, form) {
  if (!entry || !form) return [];
  const base = formFromEntry(entry);
  const out = [];
  if (form.name !== base.name) out.push('修改名称');
  if (form.key !== base.key) out.push('修改 Key');
  const oldFields = base.fields;
  const newFields = form.fields || [];
  const byName = new Map(oldFields.map((f) => [f.name, f]));
  const kept = new Set();
  newFields.forEach((f) => {
    const o = byName.get(f.name) || oldFields.find((x) => x.column && x.column === f.column);
    if (!o) out.push(`新增属性「${f.name || f.column || '未命名'}」`);
    else {
      kept.add(o);
      if (!sameField(o, f)) out.push(`修改属性「${f.name}」`);
    }
  });
  oldFields.filter((f) => !kept.has(f)).forEach((f) => out.push(`删除属性「${f.name}」`));
  if (form.description !== base.description) out.push('修改描述');
  const rel = (a) => [...(a || [])].sort().join(',');
  if (rel(form.relations) !== rel(base.relations)) out.push('修改关联对象');
  const ds = (d) => `${d?.kind || 'none'}:${d?.value || ''}`;
  if (ds(form.data_source) !== ds(base.data_source)) out.push('修改数据来源');
  const secs = new Set([...Object.keys(base.sections || {}), ...Object.keys(form.sections || {})]);
  secs.forEach((t) => { if ((base.sections?.[t] || '') !== (form.sections?.[t] || '')) out.push(`修改「${t}」`); });
  return out;
}

export function formsEqual(a, b) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

// 提交通过时「改动：…」那一句；新增属性带上证据的业务说法
export function commitSummary(changes, form, evidenceLabel) {
  if (!changes.length) return '';
  const list = changes.map((c) => {
    const m = c.match(/^新增属性「(.+)」$/);
    if (!m) return c;
    const f = (form?.fields || []).find((x) => x.name === m[1]);
    const label = f?.evidence_anchor ? evidenceLabel?.(f.evidence_anchor) : null;
    return label ? `${c}（证据：${label}）` : c;
  });
  return `改动：${list.join('、')}。`;
}

// 未通过时的那一句：「属性「x」缺少…；属性「y」…（警告）。」
export function rejectSummary(result) {
  const errs = (result?.errors || []).map((f) => f.message);
  const warns = (result?.warnings || []).map((f) => `${f.message}（警告）`);
  const all = [...errs, ...warns];
  return all.length ? `${all.join('；')}。` : '';
}

// Finding.field（如 fields[2].column）→ 单元格标红与下面的小字
export function fieldErrorMap(result) {
  const out = {};
  [...(result?.errors || [])].forEach((f) => { if (f.field) out[f.field] = f.hint || '需要修正'; });
  return out;
}

// ---------- 证据 ----------
export const CONFIDENCE_CHIP = {
  declared: ['系统元数据', 'teal'],
  observed: ['数据观察', 'blue'],
  inferred: ['推断', 'amber'],
};

// 图谱侧栏属性表里的短标签：库表证据取来源简称（推断的直接写「推断」），资料取章节号
export function evidenceShortLabel(cell) {
  if (!cell || cell.missing || !cell.label) return '';
  if (cell.confidence === 'inferred') return '推断';
  const label = String(cell.label);
  if (cell.scheme === 'db') return label.split(' · ')[0];
  const sec = label.match(/§[\d.]+/);
  if (sec) return sec[0];
  const lead = label.match(/^(\d+)\s*(\S{2})/);
  if (lead) return `${lead[1]} ${lead[2]}`;
  const part = label.match(/第\s*\d+\s*[节条章]/);
  return part ? part[0] : label.slice(0, 6);
}

// 锚点 → 资料里的章节号，用在正文里的证据小标签（如 file:…#p:11.4 → §11.4）
export function anchorChipLabel(anchor) {
  const a = String(anchor || '');
  const p = a.match(/#p:([\d.]+)$/);
  if (p) return /\./.test(p[1]) ? `§${p[1]}` : `第 ${p[1]} 节`;
  if (a.startsWith('db:')) return a.split('/').pop().split('.').slice(-2).join('.');
  // 其余只说资料名（F4b-paths）：去掉目录、片段和扩展名，文本版（resource/text/…/合同.pdf.md）映射回原资料名
  const path = a.replace(/^file:/, '').split('#')[0];
  const name = path.split('/').pop().replace(/\.md$/, (m) => (path.startsWith('resource/text/') ? '' : m));
  return name.replace(/\.[^.]+$/, '') || name || a;
}

// ---------- 原文编辑 ----------
export function fmtBytes(n) {
  if (n == null) return '';
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1).replace(/\.0$/, '')}MB`;
  if (n >= 1024) return `${Math.round(n / 1024)}KB`;
  return `${n}B`;
}

// 打开原文时要显示的提示：恢复草稿、文件已被助手改过（草稿基于的版本和文件当前版本不一致）、大文件只读
export function rawNotices(file, draft) {
  if (!file) return { restored: null, changed: null, big: null };
  const big = file.truncated ? { shown: 512 * 1024, size: file.size } : null;
  const restored = !big && draft && draft.content != null ? { at: draft.updated_at } : null;
  const changed = !big && draft && draft.base_revision && draft.base_revision !== file.revision
    ? { by: file.last_change?.source || null, at: file.last_change?.at || null, version_label: file.last_change?.version_label || null }
    : null;
  return { restored, changed, big };
}

export function hhmm(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// 流程图概况：「8 个 · 1 个判断」
export function flowchartNodeSummary(model) {
  const nodes = model?.nodes || [];
  const dec = nodes.filter((n) => n.type === 'decision').length;
  return `${nodes.length} 个${dec ? ` · ${dec} 个判断` : ''}`;
}

// ArtifactRow（AR-02）→ 条目表的行；refs 计数取 ST-03 同 key 的行
export function artifactToRow(a, fallback) {
  const v = a.status?.value;
  return {
    id: fallback?.id || `flowchart:${a.key}`,
    kind: 'flowchart',
    key: a.key,
    name: a.title,
    refs_out: fallback?.refs_out ?? 0,
    refs_in: fallback?.refs_in ?? 0,
    status: v === 'draft' ? 'draft' : v === 'stale' || a.stale ? 'stale' : v === 'pending' ? 'pending' : 'confirmed',
    needs_review: false,
    locked: !!a.locked,
    plugin: fallback?.plugin || { name: 'diagram', label: '图示' },
  };
}

// 把 ST-03 的流程图行换成 AR-02 的（图示包的列表是流程图的权威来源）；AR-02 拿不到时保留 ST-03 的
export function mergeFlowchartRows(rows, artifacts) {
  if (!artifacts) return rows;
  const old = new Map(rows.filter((r) => r.kind === 'flowchart').map((r) => [r.key, r]));
  const fc = artifacts.map((a) => artifactToRow(a, old.get(a.key)));
  const out = [];
  let placed = false;
  rows.forEach((r) => {
    if (r.kind !== 'flowchart') { out.push(r); return; }
    if (!placed) { out.push(...fc); placed = true; }
  });
  if (!placed) {
    const at = out.findIndex((r) => r.kind === 'script');
    out.splice(at < 0 ? out.length : at, 0, ...fc);
  }
  return out;
}

// ---------- 流程图预览（FlowchartModel → SVG 坐标，同设计稿 fcCanvas 的形状尺寸） ----------
const HALF = { start: [52, 19], end: [52, 19], decision: [72, 36], data: [58, 18], step: [66, 21] };
const clip = (n, dx, dy) => {
  const [hw, hh] = HALF[n.type] || HALF.step;
  if (!dx && !dy) return [n.x, n.y];
  const t = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity);
  return [n.x + dx * t, n.y + dy * t];
};

export function flowPreviewLayout(model, pad = 40) {
  const nodes = (model?.nodes || []).filter((n) => Number.isFinite(n.x) && Number.isFinite(n.y));
  if (!nodes.length) return null;
  const at = new Map(nodes.map((n) => [n.id, n]));
  let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
  nodes.forEach((n) => {
    const [hw, hh] = HALF[n.type] || HALF.step;
    x0 = Math.min(x0, n.x - hw); y0 = Math.min(y0, n.y - hh);
    x1 = Math.max(x1, n.x + hw); y1 = Math.max(y1, n.y + hh);
  });
  const edges = (model.edges || []).map((e) => {
    const a = at.get(e.from);
    const b = at.get(e.to);
    if (!a || !b) return null;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const [sx, sy] = clip(a, dx, dy);
    const [tx, ty] = clip(b, -dx, -dy);
    return { id: e.id, type: e.type || 'seq', label: e.label || '', x1: sx, y1: sy, x2: tx, y2: ty };
  }).filter(Boolean);
  return {
    viewBox: `${Math.floor(x0 - pad)} ${Math.floor(y0 - pad)} ${Math.ceil(x1 - x0 + pad * 2)} ${Math.ceil(y1 - y0 + pad * 2)}`,
    nodes: nodes.map((n) => ({ ...n, hw: (HALF[n.type] || HALF.step)[0], hh: (HALF[n.type] || HALF.step)[1] })),
    edges,
  };
}

// 被引用条目的说法：relation 是关系码时翻成中文，已经是中文就原样用
export const RELATION_LABEL = {
  operation_input_object: '读取', operation_output_object: '输出', operation_support_object: '参考',
  applies_to_object: '约束', object_relation: '关联', calls_operation: '调用', uses_rule: '引用',
  uses_metric: '引用', uses_object: '统计', script_uses_object: '读写', uses_script: '调用脚本', references: '引用',
};
export const relationText = (r) => RELATION_LABEL[r] || r || '';

// 被谁引用的一行：条目（EntryRef）或插件产物（ArtifactRef）
export function backlinkView(source) {
  if (source?.type) {
    const scenario = source.type === 'trace.scenario';
    const flow = source.type === 'diagram.flowchart';
    return { name: source.title, kind: scenario ? '场景' : flow ? '流程图' : '产物', icon: scenario ? 'flask' : flow ? 'branch' : 'file', artifact: true };
  }
  const icon = { operation: 'flow', rule: 'scale', metric: 'gauge', object: 'cube', script: 'code' }[source?.kind] || 'link';
  return { name: source?.name || '', kind: kindLabel(source?.kind), icon, artifact: false };
}

// EV-29 的解析结果 → 证据列里的业务说法（「09 修订通知 第 2 条」「ERP · 标准上限」）
export function resolutionLabel(res) {
  if (!res || !res.resolved) return null;
  if (res.scheme === 'db') {
    const src = String(res.source_label || '').replace(/^业务系统\s*/, '');
    return `${src} · ${String(res.location_label || '').split(' · ').pop()}`;
  }
  const doc = String(res.material?.name || res.source_label || '').replace(/\.\w+$/, '').replace(/^(\d+)-/, '$1 ');
  return `${doc} ${res.location_label || ''}`.trim();
}

// ---------- 填证据：先选资料、再选位置（R-01 → R-15） ----------
// 能引用的资料要有文本：本身是文本，或文本版已转好；正在转的列在后面、点不了；暂不支持、转换失败、流程图不列
const PENDING_TEXT = new Set(['converting', 'waiting']);
export function evidenceMaterials(groups, q = '') {
  const all = (groups || []).flatMap((g) => g.items || []).filter((m) => m.text);
  const title = (m) => m.title || String(m.name || m.path).replace(/\.[^.]+$/, '');
  const ready = (m) => m.text.state === 'native' || !!m.text.has_text;
  const needle = q.trim().toLowerCase();
  const rows = all
    .filter((m) => ready(m) || PENDING_TEXT.has(m.text.state))
    .map((m) => ({ path: m.path, title: title(m), type: m.type_label || '', ready: ready(m) }))
    .filter((m) => !needle || m.title.toLowerCase().includes(needle));
  const byTitle = (a, b) => a.title.localeCompare(b.title, 'zh');
  return [...rows.filter((m) => m.ready).sort(byTitle), ...rows.filter((m) => !m.ready).sort(byTitle)];
}

// R-15 的位置按「位置」连续分组（页、工作表、小节标题）；没有位置说法的一组不带标题
export function positionGroups(items) {
  const out = [];
  (items || []).forEach((it) => {
    const last = out[out.length - 1];
    if (last && last.location === (it.location || '')) last.items.push(it);
    else out.push({ location: it.location || '', items: [it] });
  });
  return out;
}

// 选中后单元格里的说法，和 EV-29 解析出来的一样（resolutionLabel），不用再等一次解析
export const evidencePickLabel = (name, location) => resolutionLabel({ resolved: true, scheme: 'file', material: { name }, location_label: location });
