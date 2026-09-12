/** Read all references to this material, including JSON/YAML quoted references. */
export function materialReferences(content, material) {
  const sources = [...new Set([material.id, material.sourceKey, material.filename].filter(Boolean))]
    .sort((a, b) => b.length - a.length);
  const references = [];
  const text = String(content || '').replace(/\\u([\da-f]{4})/giu, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
  const pattern = new RegExp(`material:(${sources.map(escapeRegex).join('|')})(?=#|[\\s"'\x60,;\\]})]|$)(?:#([^\\s"'\x60，。；、,;()（）\\]}>]+))?`, 'gu');
  if (!sources.length) return references;
  for (const match of text.matchAll(pattern)) {
    const anchor = (match[2] || '').replace(/\\+$/u, '');
    const reference = `material:${match[1]}${anchor ? `#${anchor}` : ''}`;
    if (!references.some((item) => item.reference === reference)) references.push({ reference, anchor });
  }
  return references;
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
}

/** Hide Markdown frontmatter while preserving source offsets used by evidence links. */
export function materialMarkdown(content) {
  return String(content || '').replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/u,
    (frontmatter) => frontmatter.replace(/[^\r\n]/gu, ' '));
}

function comparableHeading(value) {
  return String(value || '').normalize('NFKC').replace(/^§/u, '').toLowerCase()
    .replace(/[*_`~#\s\p{P}]/gu, '');
}

function headingsIn(text) {
  const headings = [];
  let fence = null;
  for (const match of text.matchAll(/^.*$/gm)) {
    const line = match[0].replace(/\r$/u, '');
    const delimiter = line.match(/^ {0,3}(`{3,}|~{3,})/u)?.[1];
    if (delimiter) {
      if (!fence) fence = delimiter;
      else if (delimiter[0] === fence[0] && delimiter.length >= fence.length) fence = null;
      continue;
    }
    if (fence) continue;
    const heading = line.match(/^ {0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*$/u);
    if (heading) headings.push({ start: match.index, depth: heading[1].length, title: heading[2] });
  }
  return headings;
}

/** A missing anchor must never silently fall back to the beginning of the document. */
export function materialEvidenceSelection(material, locations = []) {
  const text = materialMarkdown(material.textContent);
  const headings = headingsIn(text);
  const ranges = [];
  const tableNames = [];
  const missing = [];
  let wholeDocument = false;
  for (const location of locations) {
    const anchor = String(location?.anchor || '').trim();
    const table = anchor.match(/^(?:table|sheet)=(.+)$/iu)?.[1];
    if (table && (material.tables || []).some((item) => item.name === table)) {
      tableNames.push(table);
      continue;
    }
    const numeric = anchor.match(/^(?:offset[=:]|@)(\d+)$/iu)?.[1];
    const explicitOffset = location?.offset;
    const offset = numeric !== undefined ? Number(numeric)
      : !anchor && explicitOffset !== undefined && explicitOffset !== null ? Number(explicitOffset) : null;
    if (!anchor && offset === null) { wholeDocument = true; continue; }
    if (offset !== null) {
      if (Number.isInteger(offset) && offset >= 0 && offset < text.length && text.slice(offset).trim()) {
        ranges.push({ start: offset, end: Math.min(text.length, offset + 120) });
      } else missing.push(anchor || '指定文字位置');
      continue;
    }
    const headingIndex = headings.findIndex((heading) => comparableHeading(heading.title) === comparableHeading(anchor));
    if (headingIndex >= 0) {
      const heading = headings[headingIndex];
      const next = headings.slice(headingIndex + 1).find((item) => item.depth <= heading.depth);
      ranges.push({ start: heading.start, end: next?.start ?? text.length });
      continue;
    }
    const candidates = [anchor, anchor.replace(/^§/u, '')].filter(Boolean);
    const candidate = candidates.find((value) => text.includes(value));
    if (candidate) {
      const start = text.indexOf(candidate);
      ranges.push({ start, end: start + candidate.length });
    } else missing.push(anchor);
  }
  return { ranges, tableNames: [...new Set(tableNames)], missing, wholeDocument };
}

/** Highlight parsed text nodes, preserving lists, tables and inline Markdown structure. */
export function rehypeMaterialHighlights({ ranges = [], source = '' } = {}) {
  return (tree) => {
    const transform = (parent) => {
      if (!Array.isArray(parent.children)) return;
      parent.children = parent.children.flatMap((node) => {
        const start = node.position?.start?.offset;
        const end = node.position?.end?.offset;
        if (node.type !== 'text' || start === undefined || end === undefined || !node.value.trim()) {
          transform(node);
          return [node];
        }
        const hits = ranges.filter((range) => range.start < end && range.end > start);
        if (!hits.length) return [node];
        // Escapes and entities have different source/rendered lengths: mark the whole text node.
        const exact = source.slice(start, end) === node.value;
        const intervals = hits.map((range) => exact
          ? { start: Math.max(0, range.start - start), end: Math.min(node.value.length, range.end - start) }
          : { start: 0, end: node.value.length }).sort((a, b) => a.start - b.start);
        const merged = [];
        for (const interval of intervals) {
          const last = merged.at(-1);
          if (last && interval.start <= last.end) last.end = Math.max(last.end, interval.end);
          else merged.push({ ...interval });
        }
        const result = [];
        let cursor = 0;
        for (const interval of merged) {
          if (interval.start > cursor) result.push({ type: 'text', value: node.value.slice(cursor, interval.start) });
          result.push({ type: 'element', tagName: 'mark', properties: { className: ['wb-evidence-mark'] },
            children: [{ type: 'text', value: node.value.slice(interval.start, interval.end) }] });
          cursor = interval.end;
        }
        if (cursor < node.value.length) result.push({ type: 'text', value: node.value.slice(cursor) });
        return result;
      });
    };
    transform(tree);
  };
}
