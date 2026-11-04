// 资料分析与建模蓝图的纯逻辑：资料地图的状态文案、出处标记、核验排序、蓝图的回答与叫法（契约 R-07、R-09、BP-01、BP-02）

// 工作台地址里 ?dialog= 能直接打开的弹窗（next 只给 build 用：L4 是开始自主构建前的预览）
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
// F8 起不分段：蓝图只记统一叫法与待定问题，保存只带改过的叫法和回答（mode=draft，同待处理里回答）
export function blueprintPayload(bp, edit) {
  const body = { base_revision: bp.revision, mode: 'draft' };
  const terms = (bp.terms?.ambiguous || []).filter((t) => edit.terms[t.id] && edit.terms[t.id] !== t.chosen).map((t) => ({ term_id: t.id, chosen: edit.terms[t.id] }));
  if (terms.length) body.term_choices = terms;
  const answers = (bp.questions || []).filter((q) => edit.answers[q.id] && edit.answers[q.id] !== q.answer?.option_id).map((q) => ({ question_id: q.id, option_id: edit.answers[q.id] }));
  if (answers.length) body.answers = answers;
  return body;
}

export const isDirty = (body) => ['term_choices', 'answers'].some((k) => body[k] !== undefined);

// 资料和建模目标对不上时的那个问题（kind=goal）：不挡建模，没答之前按原目标建（F8）
export const goalQuestion = (questions = []) => questions.find((q) => q.kind === 'goal') || null;
export const goalPending = (card) => Boolean(card?.goal_question && !card.goal_question.answered);
