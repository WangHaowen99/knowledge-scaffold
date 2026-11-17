// 按声明画的对话卡片（契约 4.7 artifact、ADR-0055）的纯逻辑：卡片的形状由包的 views.<类型>.card 决定，
// 服务端已按声明取好值（标题、副标题、chips、fields）；这里只决定画成什么、双击打开什么。
// 兜底卡（没有专用组件的 card_type）也走这里：有声明画声明，没有就只有标题。

export const GENERIC_ICON = {
  material_map: 'map', blueprint: 'layers', change_list: 'refresh', run_report: 'rocket', research_summary: 'filetext',
  impact: 'target', regression: 'flask', autobuild_start: 'rocket', slice_progress: 'layers',
  auto_decision: 'bot', suspension: 'pause', build_choice: 'compass', upload_prompt: 'upload', artifact: 'layers',
};
// 没带 open 的卡默认去哪一页
export const GENERIC_PAGE = {
  material_map: 'wb-materials', blueprint: 'wb-blueprint', change_list: 'incremental', run_report: 'l4-run', impact: 'incremental',
  regression: 'incremental', autobuild_start: 'l4-run', slice_progress: 'l4-run',
  auto_decision: 'inbox', suspension: 'inbox',
};
const ITEMS_MAX = 4;

// 平台的专用卡片组件（card.component）：认得就用它画，不认得按通用卡画
export function cardComponent(card, known) {
  const name = card?.component;
  return name && known.includes(name) ? name : null;
}

// 双击打开什么：产物（按 views.detail 选编辑器或详情）> 页面 > 不能打开
export function cardOpen(card) {
  const open = card?.open;
  if (open?.kind === 'dialog' && open.target === 'artifact') {
    const ref = open.ref || card.artifact;
    if (ref?.type && ref.key) return { kind: 'artifact', ref };
  }
  if (open?.kind === 'page' && open.target) return { kind: 'page', page: open.target };
  const page = GENERIC_PAGE[card?.card_type];
  return page ? { kind: 'page', page } : null;
}

const clean = (list) => (Array.isArray(list) ? list : [])
  .filter((x) => x && typeof x.text === 'string' && x.text.trim())
  .slice(0, ITEMS_MAX);

/**
 * 卡片要画的内容：{icon, eyebrow, title, sub, status, chips: [{key, text, tone}], fields: [[标签, 值]], plugin, locked}。
 * locked：写入范围锁里有这份产物（调用方按 CS-06 实时算出来的）。卡片在对话历史里，旧卡上存的 locked 不看，锁解除后马上恢复。
 */
export function declaredCardView(card, { locked = false } = {}) {
  const chips = clean(card?.chips).map((c) => ({ key: c.field || c.text, text: c.text, tone: c.tone || 'gray' }));
  const fields = clean(card?.fields).map((f) => [f.label || f.field || '', f.text]);
  return {
    icon: card?.plugin ? 'puzzle' : GENERIC_ICON[card?.card_type] || 'filetext',
    eyebrow: card?.eyebrow || '',
    title: card?.title || card?.artifact?.title || card?.artifact?.key || '',
    sub: card?.subtitle || '',
    status: card?.status?.key ? card.status : null,
    chips,
    fields,
    plugin: card?.plugin?.label || null,
    locked: !!locked,
  };
}

// 写入范围锁里有没有这张卡的产物（流程图卡与通用产物卡同一个口径）
export function cardLocked(card, scope) {
  const ref = card?.artifact;
  if (!ref || !scope || scope.state === 'none') return false;
  return (scope.artifacts || []).some((a) => a.type === ref.type && a.key === ref.key);
}
