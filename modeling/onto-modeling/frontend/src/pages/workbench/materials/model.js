// 资料分析与建模蓝图的纯逻辑：资料地图的状态文案、出处标记、核验排序、蓝图分段列表的编辑（契约 R-07、R-09、BP-01、BP-02）

// 工作台地址里 ?dialog= 能直接打开的弹窗（next 只给 build 用：L2 / L4）
export const MATERIAL_DIALOGS = ['material_map', 'verification', 'blueprint', 'build'];

// ---------- 资料用途（契约 PurposeOption，R-01 不在时用这份） ----------
export const PURPOSES = [
  { value: 'goal', label: '建模目标依据' },
  { value: 'schema', label: '数据 Schema' },
  { value: 'term', label: '术语表' },
  { value: 'evidence', label: '通用业务依据' },
  { value: 'auto', label: '由助手判断' },
];
export const purposeLabel = (v) => PURPOSES.find((p) => p.value === v)?.label || '由助手判断';

// 资料图标：表格类用表格图标（青色），PDF 用文件，其余文档用文本文件（蓝色）
export function fileIcon(kind) {
  if (kind === 'xls' || kind === 'csv') return { icon: 'table', table: true };
  if (kind === 'pdf') return { icon: 'file', table: false };
  return { icon: 'filetext', table: false };
}

// 按扩展名猜资料类型（上传后、服务端还没回 Material 时用）
export function kindOf(name) {
  const ext = (String(name).split('.').pop() || '').toLowerCase();
  if (/^xls[xm]?$/.test(ext)) return 'xls';
  if (ext === 'csv') return 'csv';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'drawio') return 'drawio';
  if (/^html?$/.test(ext)) return 'html';
  if (/^(png|jpe?g)$/.test(ext)) return 'image';
  if (/^(md|markdown|txt)$/.test(ext)) return 'text';
  return 'doc';
}

// 读取状态：读完 / 读取中 / 排队中 / 读了一部分
export function readState(item) {
  if (item.status === 'done' || (item.read_pct >= 100 && item.status !== 'reading')) return 'done';
  if (item.status === 'reading') return 'reading';
  if (item.status === 'queued') return 'queued';
  return 'partial';
}

// 每份资料下面的计数：对象、规则（表格是字段）、术语
export function countPairs(counts = {}) {
  const second = counts.fields != null ? ['字段', counts.fields] : ['规则', counts.rules ?? 0];
  return [['对象', counts.objects ?? 0], second, ['术语', counts.terms ?? 0]];
}

// 「读到 §10 · 还剩 §10–§12」→「读到 §10」
export const shortPosition = (position) => String(position || '').split(' · ')[0];

// 没读完的部分是 Sheet 时按 Sheet 计数
export function unreadText(blocked) {
  const parts = blocked?.unread_parts || [];
  if (!parts.length) return '还有部分没读';
  const sheets = parts.every((p) => /^Sheet/.test(p));
  return sheets ? `有 ${parts.length} 个 Sheet 还没读` : `有 ${parts.length} 处还没读`;
}

// 对话里迷你资料卡最后一行：只说最要紧的一件事
export function miniStatus(item) {
  if (item.blocked) return { tone: 'amber', icon: 'alert', text: unreadText(item.blocked) };
  if (item.contradiction) return { tone: 'amber', icon: 'alert', text: item.contradiction.replace(/\s*§[\d.]+\s*/g, '') };
  const st = readState(item);
  if (st === 'reading') return { tone: 'blue', spin: true, text: shortPosition(item.position) || '读取中' };
  if (st === 'queued') return { tone: 'muted', text: '排队中' };
  if (st === 'done') {
    const [label, n] = countPairs(item.counts)[0];
    return { tone: 'muted', icon: 'check', text: `读完 · ${label} ${n}` };
  }
  return { tone: 'muted', text: `已读 ${item.read_pct}%` };
}

// 上传卡里的格式说明：按扩展名归成用户认得的类别
const ACCEPT_GROUPS = [
  ['md', ['.md', '.markdown']], ['txt', ['.txt']], ['pdf', ['.pdf']], ['word', ['.doc', '.docx']],
  ['excel', ['.xls', '.xlsx', '.xlsm', '.csv']], ['drawio', ['.drawio']], ['html', ['.html', '.htm']], ['图片', ['.png', '.jpg', '.jpeg']],
];
export function acceptLabel(accept = [], maxBytes = 0) {
  const set = new Set(accept.map((x) => x.toLowerCase()));
  const names = ACCEPT_GROUPS.filter(([, exts]) => exts.some((e) => set.has(e))).map(([n]) => n);
  const mb = maxBytes ? Math.round(maxBytes / 1048576) : 0;
  return [names.join(' / '), mb ? `单个不超过 ${mb} MB` : ''].filter(Boolean).join(' · ');
}

// ---------- 出处 ----------
// 锚点没带标签时的兜底：file:resource/others/02-门店补货规则.docx#p:9.2 →「门店补货规则 §9.2」
export function anchorLabel(anchor) {
  const s = String(anchor || '');
  const m = s.match(/^(\w+):(.*?)(?:#(.*))?$/);
  if (!m) return s;
  const [, scheme, path, frag = ''] = m;
  if (scheme === 'db') return path.split('/').pop();
  const base = path.split('/').pop().replace(/\.\w+$/, '').replace(/^\d+-/, '');
  const loc = frag.replace(/^p:/, '§').replace(/^L(\d+)$/, '第 $1 行');
  return loc ? `${base} ${loc}` : base;
}

// file:resource/others/x.docx#p:9.2 → { scheme: 'file', path: 'resource/others/x.docx', fragment: 'p:9.2' }
export function parseAnchor(anchor) {
  const m = String(anchor || '').match(/^(\w+):([^#]*)(?:#(.*))?$/);
  return m ? { scheme: m[1], path: m[2], fragment: m[3] || null } : { scheme: null, path: '', fragment: null };
}

// 正文里的 **加粗** 和 [^n] 角标拆成片段
export function anchorTokens(text) {
  const out = [];
  const re = /\*\*(.+?)\*\*|\[\^(\d+)\]/g;
  let pos = 0;
  let m;
  const s = String(text || '');
  while ((m = re.exec(s))) {
    if (m.index > pos) out.push({ t: 'text', v: s.slice(pos, m.index) });
    if (m[1] != null) out.push({ t: 'bold', v: m[1] });
    else out.push({ t: 'cite', n: Number(m[2]) });
    pos = m.index + m[0].length;
  }
  if (pos < s.length) out.push({ t: 'text', v: s.slice(pos) });
  return out;
}

// ---------- 核验（R-09） ----------
export const VERIFY_STATUS = {
  unverifiable: { label: '来源无法核验', tone: 'red', rank: 0 },
  pending: { label: '已生成，待核验', tone: 'gray', rank: 1 },
  passed: { label: '通过', tone: 'teal', icon: 'check', rank: 2 },
};

// 有问题的在前；通过的默认只露几条，其余收起
export function verificationRows(items = [], { passedShown = 2, expanded = false } = {}) {
  const sorted = [...items].sort((a, b) => (VERIFY_STATUS[a.status]?.rank ?? 3) - (VERIFY_STATUS[b.status]?.rank ?? 3));
  const notPassed = sorted.filter((x) => x.status !== 'passed');
  const passed = sorted.filter((x) => x.status === 'passed');
  const shown = expanded ? passed : passed.slice(0, passedShown);
  return { rows: [...notPassed, ...shown], hidden: passed.length - shown.length };
}

export function verificationNote(items = []) {
  const reasons = items.filter((x) => x.status === 'unverifiable').map((x) => x.reason).filter(Boolean);
  if (!reasons.length) return '';
  return `${reasons.join('；')}。`;
}

// ---------- 建模蓝图（BP-01 / BP-02） ----------
// 编辑用的分段：带上原始顺序，判断位置是否动过
export function editableSlices(slices = []) {
  return slices.map((s, i) => ({ ...s, depends_on: [...(s.depends_on || [])], origin: i, merged_from: s.merged_from || undefined }));
}

export function moveSlice(list, from, to) {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return list;
  const next = [...list];
  const [x] = next.splice(from, 1);
  next.splice(to, 0, x);
  return next;
}

let seq = 0;
const newId = () => `new-${Date.now().toString(36)}-${(seq += 1)}`;

// 合并：在第一段的位置放一段新的，依赖取并集（去掉被合并的段），其他段对被合并段的依赖改指向新段
export function mergeSlices(list, ids) {
  const picked = list.filter((s) => ids.includes(s.id));
  if (picked.length < 2) return { list, merged: null };
  const id = newId();
  const merged = {
    id,
    name: picked.map((s) => s.name).join('、'),
    core_flow: picked.map((s) => s.core_flow).filter(Boolean).join(' · '),
    scale: {
      objects: picked.reduce((n, s) => n + (s.scale?.objects || 0), 0),
      rules: picked.reduce((n, s) => n + (s.scale?.rules || 0), 0),
    },
    depends_on: [...new Set(picked.flatMap((s) => s.depends_on || []))].filter((d) => !ids.includes(d)),
    excluded: false,
    merged_from: picked.flatMap((s) => s.merged_from || [s.id]),
    order_label: '',
    origin: -1,
  };
  const at = list.findIndex((s) => ids.includes(s.id));
  const rest = list.filter((s) => !ids.includes(s.id)).map((s) => {
    const deps = (s.depends_on || []).map((d) => (ids.includes(d) ? id : d));
    return { ...s, depends_on: [...new Set(deps)] };
  });
  rest.splice(at, 0, merged);
  return { list: rest, merged };
}

export function toggleExcluded(list, ids) {
  const allExcluded = list.filter((s) => ids.includes(s.id)).every((s) => s.excluded);
  return list.map((s) => (ids.includes(s.id) ? { ...s, excluded: !allExcluded } : s));
}

export function renameSlice(list, id, name) {
  const n = String(name || '').trim();
  if (!n) return list;
  return list.map((s) => (s.id === id ? { ...s, name: n } : s));
}

export function addSlice(list, name = '新分段') {
  const s = { id: newId(), name, core_flow: '', scale: { objects: 0, rules: 0 }, depends_on: [], excluded: false, order_label: '', origin: -1 };
  return { list: [...list, s], added: s };
}

// 依赖显示成当前序号：「无」或「1、2」
export function depLabel(slice, list) {
  const deps = (slice.depends_on || []).map((d) => list.findIndex((s) => s.id === d)).filter((i) => i >= 0).map((i) => i + 1).sort((a, b) => a - b);
  return deps.length ? deps.join('、') : '无';
}

// 依赖的段排在了后面（保存时服务端会拒绝：slice_invalid）
export function depAfter(slice, list) {
  const at = list.findIndex((s) => s.id === slice.id);
  return (slice.depends_on || []).some((d) => list.findIndex((s) => s.id === d) > at);
}

// 顺序标签：位置没动用服务端给的推荐，动过就按新位置写「第 n 个」
export function orderLabel(slice, index) {
  if (slice.excluded) return { text: '已排除', tone: 'faint' };
  const text = slice.origin === index && slice.order_label ? slice.order_label : `第 ${index + 1} 个`;
  if (text === '推荐先做') return { text, tone: 'blue' };
  if (text === '建议最后做') return { text, tone: 'amber' };
  return { text, tone: 'faint' };
}

// BP-02 的分段列表（BlueprintSliceInput）：顺序就是数组顺序
export function sliceInputs(list) {
  return list.map((s) => {
    const x = { id: s.id, name: s.name, core_flow: s.core_flow || '', depends_on: s.depends_on || [], excluded: !!s.excluded };
    if (s.merged_from?.length) x.merged_from = s.merged_from;
    return x;
  });
}

export function sameSlices(a = [], b = []) {
  return JSON.stringify(sliceInputs(a)) === JSON.stringify(sliceInputs(b));
}

// 整份调整请求：只带改过的部分
export function blueprintPayload(bp, edit, mode) {
  const body = { base_revision: bp.revision, mode };
  if (!sameSlices(edit.slices, bp.slices)) body.slices = sliceInputs(edit.slices);
  const terms = (bp.terms?.ambiguous || []).filter((t) => edit.terms[t.id] && edit.terms[t.id] !== t.chosen).map((t) => ({ term_id: t.id, chosen: edit.terms[t.id] }));
  if (terms.length) body.term_choices = terms;
  const answers = (bp.questions || []).filter((q) => edit.answers[q.id] && edit.answers[q.id] !== q.answer?.option_id).map((q) => ({ question_id: q.id, option_id: edit.answers[q.id] }));
  if (answers.length) body.answers = answers;
  return body;
}

export const isDirty = (body) => ['slices', 'term_choices', 'answers'].some((k) => body[k] !== undefined);

// 资料和建模目标对不上时的那个问题（kind=goal）：没答之前不能确认蓝图（F4b）
export const goalQuestion = (questions = []) => questions.find((q) => q.kind === 'goal') || null;
export const goalPending = (card) => Boolean(card?.goal_question && !card.goal_question.answered);

// 「确认后怎么建」（R3）并进蓝图卡底部：按卡上的 confirm_options 画（草案时给，自主构建登记了才有 L4，已确认时为空）；
// 早于 B4d 合并的旧卡没有这个字段，只给「一段一段来」
const DEFAULT_CONFIRM = [{ mode: 'L2', label: '确认，一段一段来' }];
export const confirmOptions = (card) => (card?.state === 'confirmed' ? [] : (Array.isArray(card?.confirm_options) ? card.confirm_options : DEFAULT_CONFIRM));
