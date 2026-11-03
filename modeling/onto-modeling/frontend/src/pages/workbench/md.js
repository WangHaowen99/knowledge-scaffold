// 助手正文的两种内联标记：[[名称]] 条目链接、[^n] 出处角标。先改写成普通链接，再交给 Markdown 渲染
export function preprocessMd(text) {
  return String(text || '')
    .replace(/\[\[([^\]]+?)\]\]/g, (_, name) => `[${name.replace(/[[\]]/g, '')}](#wl:${encodeURIComponent(name)})`)
    .replace(/\[\^(\d+)\]/g, (_, n) => `[${n}](#ct:${n})`);
}

export const stripLinks = (s) => String(s || '').replace(/\[\[|\]\]/g, '');

// 拆成纯文本、条目链接、角标三种片段（单行内容用，不走 Markdown）
export function inlineTokens(text) {
  const out = [];
  const re = /\[\[([^\]]+?)\]\]|\[\^(\d+)\]/g;
  let pos = 0;
  let m;
  const s = String(text || '');
  while ((m = re.exec(s))) {
    if (m.index > pos) out.push({ t: 'text', v: s.slice(pos, m.index) });
    if (m[1] !== undefined) out.push({ t: 'link', v: m[1] });
    else out.push({ t: 'cite', v: Number(m[2]) });
    pos = m.index + m[0].length;
  }
  if (pos < s.length) out.push({ t: 'text', v: s.slice(pos) });
  return out;
}
