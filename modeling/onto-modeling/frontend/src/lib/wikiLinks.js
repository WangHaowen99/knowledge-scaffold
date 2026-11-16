const WIKI_MARKUP = /\[\[([^\]\n]+)\]\]/g;
// 历史消息里被空白拆开的 `[文字] (#wiki:…)`；本分支前端用的前缀是 #wl:，两种都修
const BROKEN_WIKI_LINK = /\[([^\]\n]+)\]\s*\(#(?:wiki|wl):([^\s)]+)\)/g;

function wikiLink(prefix, name, label = name) {
  return {
    type: 'link',
    url: `${prefix}${encodeURIComponent(name)}`,
    children: [{ type: 'text', value: label }],
  };
}

function decodeWikiName(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

function splitWikiText(value, prefix) {
  const children = [];
  let offset = 0;
  const matches = [...value.matchAll(BROKEN_WIKI_LINK), ...value.matchAll(WIKI_MARKUP)]
    .sort((left, right) => left.index - right.index);

  for (const match of matches) {
    if (match.index < offset) continue;
    if (match.index > offset) children.push({ type: 'text', value: value.slice(offset, match.index) });

    if (match[0].startsWith('[[')) {
      const name = match[1].trim();
      if (name) children.push(wikiLink(prefix, name));
      else children.push({ type: 'text', value: match[0] });
    } else {
      const name = decodeWikiName(match[2]);
      if (name) children.push(wikiLink(prefix, name, match[1]));
      else children.push({ type: 'text', value: match[0] });
    }
    offset = match.index + match[0].length;
  }

  if (offset === 0) return null;
  if (offset < value.length) children.push({ type: 'text', value: value.slice(offset) });
  return children;
}

// Convert Wiki markup after Markdown parsing, so encoded targets never become visible source text.
// Also repairs historical messages where whitespace split `[label]` from its `(#wiki:...)` target.
// prefix：链接地址前缀，缺省 #wiki:（develop）；本分支对话里的条目链接用 #wl:
export function remarkWikiLinks({ prefix = '#wiki:' } = {}) {
  return function transform(tree) {
    function visit(node) {
      if (!node?.children || ['link', 'code', 'inlineCode', 'html'].includes(node.type)) return;
      const children = [];
      for (const child of node.children) {
        if (child.type === 'text') children.push(...(splitWikiText(child.value, prefix) || [child]));
        else {
          visit(child);
          children.push(child);
        }
      }
      node.children = children;
    }
    visit(tree);
  };
}
