// 增量更新（设计稿 incremental，契约 3.14 INC-01～INC-05、4.7 change_list / impact / regression / round_changes）
// 的纯逻辑：把握与只读判断、执行前的确认计划、弹窗的编辑行、卡片文字、进入页面时去哪条对话。

export const CONFIDENCE = { high: ['高', 'teal'], medium: ['中', 'amber'], low: ['低', 'red'] };

export const KIND_ICON = { object: 'cube', operation: 'flow', rule: 'scale', metric: 'gauge', script: 'code' };
export const KIND_LABEL = { object: '对象', operation: '操作', rule: '规则', metric: '指标', script: '脚本' };

export const isDone = (p) => p.state === 'accepted' || p.state === 'rewritten';
export const isLowPending = (p) => p.confidence === 'low' && !isDone(p);

// 「接受高把握项 N」：只算还没处理的高把握条目
export const highPending = (points) => (points || []).filter((p) => p.confidence === 'high' && !isDone(p));

// 把握低的悬浮说明（标注 incremental-017）：只给后端的原因，没有原因时写「把握低」
export function lowPop(p) {
  return (p?.confidence_note || '').trim() || '把握低';
}

// 清单卡的副标题：「来自 09-修订通知」（状态看表格的「把握」列和「需你确认」标记）
export const sourceName = (source) => String(source?.path || '').split('/').pop().replace(/\.[^.]+$/, '') || source?.label || '变更资料';

export function listSubtitle(cl) {
  return `来自 ${sourceName(cl?.source)}`;
}

/**
 * 「确认清单并执行」之前要做的事（INC-04 要求全部条目已接受或已改写，低把握的必须单独确认过）：
 * - blocked：还没确认的低把握条目，要打开弹窗单独确认，不能替用户接受；
 * - accept：其余还没确认的条目，一并接受（INC-03 允许中、高把握批量）。
 */
export function executePlan(points) {
  const list = points || [];
  return {
    blocked: list.filter(isLowPending),
    accept: list.filter((p) => !isDone(p) && p.confidence !== 'low').map((p) => p.id),
  };
}

// 表格里的三列：改写过的条目，取值列显示改写后的说法
export function pointCells(p) {
  const joined = [p.condition, p.value, p.scope].filter(Boolean).join(' ');
  const rewritten = p.rewritten_text && p.rewritten_text !== joined ? p.rewritten_text : null;
  return { condition: p.condition || '', value: rewritten || p.value || '', scope: p.scope || '' };
}

// 出处列只放节号：清单整份都来自同一份资料，「09 §2」→「§2」
export const shortAnchor = (label) => String(label || '').replace(/^\S+\s+(?=§)/, '');

// 清单「出处」一栏：有锚点才画证据角标，没有就空着（没带参考资料时影响分析的条目都没有锚点，不是「手动补充」）
export const sourceCell = (row) => (row?.anchor ? { anchor: row.anchor, label: shortAnchor(row.anchor_label) } : null);

// ---------- 弹窗：逐条修改清单 ----------
let draftSeq = 0;
// 弹窗里可编辑的行：key 稳定，新增行没有 id
export function toRows(points) {
  return (points || []).map((p) => {
    const c = pointCells(p);
    return {
      key: p.id, id: p.id, n: p.n, condition: c.condition, value: c.value, scope: c.scope,
      anchor: p.anchor, anchor_label: p.anchor_label, confidence: p.confidence, confidence_note: p.confidence_note,
      state: p.state, origin: { condition: c.condition, value: c.value, scope: c.scope },
    };
  });
}

export function newRow(rows) {
  draftSeq += 1;
  return {
    key: `new-${draftSeq}`, id: null, n: (rows || []).length + 1, condition: '', value: '', scope: '',
    anchor: '', anchor_label: '', confidence: null, confidence_note: null, state: 'new', origin: null,
  };
}

const trimmed = (r) => ({ condition: (r.condition || '').trim(), value: (r.value || '').trim(), scope: (r.scope || '').trim() });

export function rowChanged(r) {
  if (!r.origin) return true;
  const t = trimmed(r);
  return t.condition !== r.origin.condition || t.value !== r.origin.value || t.scope !== r.origin.scope;
}

export const isDirty = (rows) => (rows || []).some(rowChanged);

export function validateRow(r) {
  const t = trimmed(r);
  if (!t.condition) return '条件不能为空';
  if (!t.value && !t.scope) return '取值和范围至少写一项';
  return null;
}

// INC-02 的请求体：完整列表；新增条目不带 id；改过的条目带上改写后的说法
export function toInputs(rows) {
  return (rows || []).map((r) => {
    const t = trimmed(r);
    const out = { condition: t.condition, value: t.value, scope: t.scope, anchor: r.anchor || '' };
    if (r.id) out.id = r.id;
    if (r.id && rowChanged(r)) out.rewritten_text = [t.condition, t.value, t.scope].filter(Boolean).join(' ');
    return out;
  });
}

// 弹窗底部的说明（标注 incremental-008）：「第 2 行在编辑；第 3 行把握低，必须单独确认。」
export function dialogNote(rows, editing) {
  const parts = [];
  const ed = (rows || []).filter((r) => editing?.has?.(r.key)).map((r) => r.n);
  if (ed.length) parts.push(`第 ${ed.join('、')} 行在编辑`);
  const low = (rows || []).filter((r) => r.confidence === 'low' && !isDone(r) && !rowChanged(r)).map((r) => r.n);
  if (low.length) parts.push(`第 ${low.join('、')} 行把握低，必须单独确认`);
  return parts.length ? `${parts.join('；')}。` : '';
}

// ---------- 影响面、回归 ----------
export const secs = (ms) => `${((ms || 0) / 1000).toFixed(1)} 秒`;

export function phasesText(phases) {
  return (phases || []).map((p) => `${p.label} ${secs(p.duration_ms)}`).join(' · ');
}

export function refName(ref) {
  return ref?.name || ref?.title || ref?.key || '';
}

export function regressionSummary(card) {
  const rows = card?.rows || [];
  const passed = card?.passed ?? rows.filter((r) => r.result === 'pass').length;
  const total = card?.total ?? rows.length;
  return {
    passed, total, failed: total - passed,
    affected: rows.filter((r) => r.type === 'affected').length,
    guard: rows.filter((r) => r.type === 'guard').length,
  };
}

// 「场景 6 临时加量缺…」→「场景 6」：交给助手修时对话里那句话用短名
export function scenarioShort(ref) {
  const name = refName(ref);
  return (/^场景 ?\d+/.exec(name) || [name])[0];
}

// 「交给助手修」发的命令（S-20）：增量更新命令带上要修的场景
export function fixCommand(changeListId, scenarios, conversationId) {
  const list = [].concat(scenarios || []);
  return {
    command: '/ontology-update',
    args: { ...(changeListId ? { change_list_id: changeListId } : {}), fix_scenarios: list.map((s) => s.key) },
    text: `修复${list.map(scenarioShort).join('、')}`,
    origin: { page: 'wb-l2', label: '增量更新' },
    ...(conversationId ? { conversation_id: conversationId } : {}),
  };
}

// 「这一轮改了什么」逐行：图标、动词、带 [[ ]] 的说明
const VERB = { mod: ['edit', '修改'], add: ['plus', '新增'], del: ['trash', '删除'], test: ['flask', '刷新'] };
export function roundLines(card) {
  return (card?.groups || []).flatMap((g) => g.items.map((it) => {
    const [icon, verb] = it.change ? VERB[it.change] || VERB.mod : ['more', ''];
    return { icon, verb, text: it.text || '' };
  }));
}

// 还有场景没通过时，版本旁边的提示（标注 incremental-006；F3 起一轮结束就合入，没有「确认后」）
export function remainingNote(card) {
  const n = card?.remaining_failures || 0;
  return n ? `不通过的 ${n} 个场景已留在待处理` : '';
}

// ---------- 进入增量更新时去哪条对话 ----------
// IN-01（filter=change_list）里第一份还没执行的变更清单
export function changeListIdFromInbox(items) {
  const hit = (items || []).find((it) => it.kind === 'change_list' && typeof it.id === 'string');
  return hit ? hit.id.slice('change_list:'.length) : null;
}

// 已执行的清单不在待处理里（IN-01 只列没执行的）；升级前留下的、还没合入的变更集（F3 起一轮结束就合入）
// 落到它所在的对话（CS-01 state=pending_confirm 的 conversation_id，还在对话列表里的）
export function pendingChangesetConversation(changesets, conversations) {
  const ids = new Set((conversations || []).map((c) => c.id));
  const hit = (changesets || []).find((cs) => cs.source === 'assistant' && cs.conversation_id && ids.has(cs.conversation_id));
  return hit ? hit.conversation_id : null;
}

// 对话里最近一张变更点清单卡
export function latestChangeListCard(state) {
  const keys = state?.order || [];
  for (let i = keys.length - 1; i >= 0; i -= 1) {
    const it = state.items[keys[i]];
    if (it?.kind === 'card' && it.card?.card_type === 'change_list') return it.card;
  }
  return null;
}
