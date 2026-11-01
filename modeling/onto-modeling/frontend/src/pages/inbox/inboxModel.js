// 待处理（设计稿 inbox）的纯函数：筛选带、类型样式、时间文字、排序、上下条、键盘、「确认并下一条」要发的动作

// 筛选带：键与 IN-01 的 filter 一致
export const FILTERS = [
  ['all', '全部'], ['autobuild_review', '自主构建复核'], ['decision', '决策'], ['evidence_conflict', '证据冲突'],
  ['change_list', '变更清单'], ['stale', '过期'], ['resumable', '续跑'], ['feedback', '外部反馈'],
];

// 类型 → 设计稿的样式类（k-*）与图标
export const KIND_STYLE = {
  decision: ['decision', 'hand'],
  evidence_conflict: ['conflict', 'alert'],
  change_list: ['change', 'refresh'],
  review: ['review', 'bot'],
  suspension: ['suspend', 'pause'],
  stale: ['stale', 'refresh'],
  resumable: ['resume', 'history'],
  feedback: ['feedback', 'inbox'],
};
export const kindStyle = (kind) => KIND_STYLE[kind] || ['resume', 'inbox'];

const pad2 = (n) => String(n).padStart(2, '0');
export const hhmm = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};

// 列表与详情里的时间：自主构建的写「运行 #3 · 15:42」，其余写相对时间
export function ageText(item, now = Date.now()) {
  if (item?.run) return item.run.at ? `运行 #${item.run.run_no} · ${hhmm(item.run.at)}` : `运行 #${item.run.run_no}`;
  const t = Date.parse(item?.created_at || '');
  if (Number.isNaN(t)) return '';
  const min = Math.max(0, Math.round((now - t) / 60000));
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  if (min < 6 * 60) return `${Math.round(min / 60)} 小时前`;
  const d = new Date(t);
  const today = new Date(now);
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, today)) return `今天 ${hhmm(item.created_at)}`;
  const y = new Date(now);
  y.setDate(y.getDate() - 1);
  if (sameDay(d, y)) return '昨天';
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export const SORTS = [['default', '默认顺序'], ['newest', '最新在前'], ['slice', '按切片']];
export function sortItems(items, sort) {
  const list = [...(items || [])];
  if (sort === 'newest') return list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  // 按切片：没有切片的放最后
  if (sort === 'slice') return list.sort((a, b) => (!a.slice?.name - !b.slice?.name) || (a.slice?.name || '').localeCompare(b.slice?.name || '', 'zh'));
  return list;
}

// 上下条：到头就停
export function stepId(items, cur, dir) {
  if (!items?.length) return null;
  const i = items.findIndex((x) => x.id === cur);
  if (i < 0) return items[0].id;
  return items[Math.max(0, Math.min(items.length - 1, i + dir))].id;
}

// 处理完一项后去哪：服务端给的下一条 > 列表里的下一条 > 上一条
export function afterResolve(items, cur, next) {
  const left = (items || []).filter((x) => x.id !== cur);
  if (next && left.some((x) => x.id === next)) return next;
  const i = (items || []).findIndex((x) => x.id === cur);
  return (items || []).slice(i + 1).find((x) => x.id !== cur)?.id || left[left.length - 1]?.id || null;
}

// 键盘：J/K 上下条、1–4 选择、E 改写、↵ 确认并下一条、Esc 取消改写；输入框里只认 ↵ 和 Esc
export function keyAction(e, { editing = false } = {}) {
  const k = (e.key || '').toLowerCase();
  if (editing || e.inInput) {
    if (k === 'escape') return { type: 'cancel' };
    if (k === 'enter' && !e.shiftKey && !e.isComposing) return { type: 'confirm' };
    return null;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return null;
  if (k === 'j') return { type: 'move', dir: 1 };
  if (k === 'k') return { type: 'move', dir: -1 };
  if (/^[1-4]$/.test(k)) return { type: 'pick', index: Number(k) - 1 };
  if (k === 'e') return { type: 'rewrite' };
  if (k === 'enter') return { type: 'confirm' };
  if (k === 'escape') return { type: 'cancel' };
  return null;
}

// 自主构建采用的那个选项（改判时的对照）
export function adoptedIndex(options, chosen) {
  const list = options || [];
  const byFlag = list.findIndex((o) => o.recommended);
  if (byFlag >= 0) return byFlag;
  return list.findIndex((o) => chosen && chosen.startsWith(o.label));
}

// 详情里要选的选项：决策、证据冲突、需复核、挂起
export function optionsOf(item, detail) {
  if (!item || !detail) return [];
  if (item.kind === 'decision') return detail.options || [];
  if (item.kind === 'evidence_conflict') return detail.question?.options || [];
  if (item.kind === 'review' && !detail.group) return detail.options || [];
  if (item.kind === 'suspension') return detail.options || [];
  return [];
}

/**
 * 「确认并下一条」（↵）要发的动作。sel：选中的选项下标；text：改写的内容（null 表示没在改写）。
 * 返回 {action, body} 交给 IN-03，或 {error} 提示用户。
 */
export function confirmPlan(item, detail, sel, text) {
  if (!item) return { error: '没有选中的事项' };
  const opts = optionsOf(item, detail);
  const custom = text != null && text.trim() ? { custom_text: text.trim() } : null;
  const picked = sel != null && opts[sel] && !opts[sel].custom ? { option_id: opts[sel].id } : null;
  const answer = (action) => {
    if (custom) return { action, body: custom };
    if (picked) return { action, body: picked };
    return { error: sel != null && opts[sel]?.custom ? '先写下你的方向' : '先选一个选项，或按 E 写下你的方向' };
  };
  switch (item.kind) {
    case 'decision': return answer('answer');
    case 'evidence_conflict': return answer('arbitrate');
    case 'change_list': {
      const pending = (detail?.points || []).filter((p) => p.state === 'pending').length;
      return pending ? { error: `还有 ${pending} 条变更点没确认` } : { action: 'execute', body: {} };
    }
    case 'review': {
      if (detail?.group) return { action: 'accept', body: {} };
      const adopted = adoptedIndex(opts, detail?.chosen);
      if (!custom && (sel == null || sel === adopted)) return { action: 'accept', body: {} };
      return answer('rejudge');
    }
    case 'suspension':
      if (!opts.length) return { error: '先选：上传文本版，或先按通用要求' };
      return answer('answer');
    case 'stale': return { action: 'refresh', body: {} };
    case 'resumable': return { action: 'resume', body: {} };
    case 'feedback': return { action: 'send_to_evolution', body: {} };
    default: return item.actions?.includes('dismiss') ? { action: 'dismiss', body: {} } : { error: '这一项要在对应的页面里处理' };
  }
}

// 出处的短标签：「§9.2」「品类表 D12」「ERP」
export function anchorLabel(anchor) {
  const s = String(anchor || '');
  if (s.startsWith('db:')) return '数据库';
  const m = s.match(/#p:([\d.]+)/);
  if (m) return `§${m[1]}`;
  const c = s.match(/#cell:([^!]+)!(\w+)/);
  if (c) return `${c[1]} ${c[2]}`;
  const name = s.split('/').pop().split('#')[0].replace(/^\d+-/, '').replace(/\.\w+$/, '');
  return name || s;
}

// 汇总条副标题：「15:10 – 17:02 · 全部切片完成 · 费用 ¥86」
const RUN_STATE_TEXT = {
  completed: '全部切片完成', stopped: '在安全点停下', converted: '已转为陪伴', budget_exhausted: '预算用完，在安全点停下', failed: '出错停下',
  running: '运行中', pausing: '正在暂停', paused: '已暂停', stopping: '停在安全点', converting: '转为陪伴', queued: '排队中',
};
export function summarySub(s) {
  if (!s) return '';
  const span = s.ended_at ? `${hhmm(s.started_at)} – ${hhmm(s.ended_at)}` : `${hhmm(s.started_at)} 开始`;
  const cost = s.cost?.amount != null ? `费用 ${s.cost.currency === 'USD' ? '$' : '¥'}${Math.round(s.cost.amount)}` : '';
  return [span, RUN_STATE_TEXT[s.state] || '', cost].filter(Boolean).join(' · ');
}

// 「今天的进度」里列出来的几类：决策、自主构建复核、过期
export function todayRows(today) {
  const r = today?.remaining_by_filter || {};
  return [
    ['决策', r.decision, (n) => `${n} 剩余`],
    ['自主构建复核', r.autobuild_review, (n) => `${n} 剩余，高置信可一键接受`],
    ['过期', r.stale, (n) => `${n} 剩余，可一次刷新`],
  ].filter(([, n]) => n > 0).map(([a, n, fn]) => [a, fn(n)]);
}

// 影响列里的条目名：规则只写编号（R-11），其余写名称
export const impactName = (x) => (x?.kind === 'rule' && /^R-\d+/.test(x.name || '') ? x.name.match(/^R-\d+/)[0] : x?.name || x?.title || '');

export const changePointText = (p) => p.rewritten_text || [p.condition, p.value, p.scope].filter(Boolean).join(' ');
export const CONFIDENCE_LABEL = { high: ['高', 'blue'], medium: ['中', 'amber'], low: ['低', 'red'] };
