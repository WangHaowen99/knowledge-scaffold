// 全部命令（S-18）在输入框「/」菜单和 ⌘K 里的同一套筛选：组就是导航栏节点（组名是节点名），条目叫法和导航栏、建议条一样；
// 执行时发条目的完整命令文本（flow.catalogCommand）。

// 输入框里的「/」查询：以斜杠开头时斜杠后面的字是筛选词，否则不是查询（null）
export function menuQuery(text) {
  const t = String(text ?? '');
  return t.startsWith('/') ? t.slice(1).trim() : null;
}

// 按名字筛：不分大小写，名字里含筛选词就留；没有条目的组不留
export function filterCatalog(catalog, q) {
  const want = String(q || '').trim().toLowerCase();
  return (catalog?.groups || [])
    .map((g) => ({
      id: g.id,
      label: g.label,
      items: (g.items || []).filter((c) => c.available !== false && (!want || String(c.label || '').toLowerCase().includes(want))),
    }))
    .filter((g) => g.items.length);
}

export const flatItems = (groups) => (groups || []).flatMap((g) => g.items);

// ↑↓ 在可选条目之间移动，停在两头
export function moveSelection(sel, delta, count) {
  if (!count) return 0;
  return Math.min(count - 1, Math.max(0, sel + delta));
}

// 回车执行哪一条：选中的那条；没有匹配时为 null，按原文发送
export function menuPick(groups, sel) {
  return flatItems(groups)[sel] || null;
}

/**
 * 菜单开着时这个键做什么（输入框的 onKeyDown 先问它，返回 null 就照常处理）：
 * - Esc：菜单里有条目就只关菜单，不让工作台当成打断；
 * - ↑↓：斜杠查询（filtering）时在条目之间移动，停在两头；
 * - 回车（不带 Shift、不在输入法组字中）：斜杠查询时执行选中的那条；没有匹配时菜单是空的，返回 null，按原文发送。
 * 从按钮打开、框里是别的字时只有 Esc 归菜单，回车照常发框里的字。
 */
export function menuKey({ key, shift = false, composing = false }, {
  open, filtering, count, sel,
}) {
  if (!open || !count) return null;
  if (key === 'Escape') return { type: 'close' };
  if (!filtering) return null;
  if (key === 'ArrowDown' || key === 'ArrowUp') return { type: 'move', sel: moveSelection(sel, key === 'ArrowDown' ? 1 : -1, count) };
  if (key === 'Enter' && !shift && !composing && sel >= 0 && sel < count) return { type: 'run', index: sel };
  return null;
}
