export function displayEvidenceReferences(value, materials = []) {
  let text = String(value ?? '');
  for (const material of materials) {
    if (!material?.filename) continue;
    const identifiers = [material.id, material.sourceKey].filter(Boolean);
    for (const identifier of identifiers) {
      text = text.split(`material:${identifier}#`).join(`【${material.filename}】#`);
    }
  }
  return text.replace(/(【[^】]+】)#((?:offset=\d+)|(?:(?:table|sheet)=[^\s，。；、)\]}>]+))/giu,
    (_match, source, anchor) => `${source}#${readableEvidenceAnchor(anchor)}`);
}

const MATERIAL_PREFIX = 'material:';
const EVIDENCE_HREF_PREFIX = '#evidence:';
const ANCHOR_END = /[\s，。；、)\]}>]/u;

export function materialEvidence(reference, materials = []) {
  const normalized = String(reference ?? '').normalize('NFKC').trim();
  if (!normalized.startsWith(MATERIAL_PREFIX)) return null;
  const body = normalized.slice(MATERIAL_PREFIX.length);
  const hash = body.indexOf('#');
  const source = (hash >= 0 ? body.slice(0, hash) : body).trim();
  const anchor = (hash >= 0 ? body.slice(hash + 1) : '').trim();
  const material = materials.find((item) => [item?.id, item?.sourceKey, item?.filename].filter(Boolean).includes(source));
  return { reference: normalized, source, anchor, material };
}

export function evidenceLabel(reference, materials = []) {
  const located = materialEvidence(reference, materials);
  if (located) {
    const name = located.material?.filename || '资料原文';
    return located.anchor ? `《${name}》 · ${readableEvidenceAnchor(located.anchor)}` : `《${name}》原文`;
  }
  if (String(reference ?? '').startsWith('user-decision:')) return '当时的业务裁决';
  if (String(reference ?? '').startsWith('flow-node:')) return '流程中的业务依据';
  return '业务依据';
}

export function readableEvidenceAnchor(anchor) {
  const value = String(anchor ?? '').trim();
  const offset = value.match(/^offset=(\d+)$/iu)?.[1];
  if (offset) return `正文第 ${offset.replace(/\B(?=(\d{3})+(?!\d))/g, ',')} 字附近`;
  const table = value.match(/^(?:table|sheet)=(.+)$/iu)?.[1];
  return table ? `表：${table}` : value;
}

export function evidenceHref(reference) {
  return `${EVIDENCE_HREF_PREFIX}${encodeURIComponent(String(reference ?? '').normalize('NFKC').trim())}`;
}

export function evidenceReferenceFromHref(href) {
  const value = String(href ?? '');
  if (!value.startsWith(EVIDENCE_HREF_PREFIX)) return '';
  try {
    return decodeURIComponent(value.slice(EVIDENCE_HREF_PREFIX.length));
  } catch {
    return '';
  }
}

/** 把知识 Markdown 里的资料证据变成可见链接；只识别项目资料清单中的来源，避免误链普通文本。 */
export function linkEvidenceReferences(value, materials = []) {
  let text = String(value ?? '');
  const sources = materials.flatMap((material) => [material?.id, material?.sourceKey, material?.filename]
    .filter(Boolean)
    .map((source) => ({ source: String(source), material })))
    .sort((left, right) => right.source.length - left.source.length);
  const consumed = new Set();
  for (const { source, material } of sources) {
    const prefix = `${MATERIAL_PREFIX}${source}#`;
    if (consumed.has(prefix)) continue;
    consumed.add(prefix);
    let cursor = 0;
    while ((cursor = text.indexOf(prefix, cursor)) >= 0) {
      let end = cursor + prefix.length;
      while (end < text.length && !ANCHOR_END.test(text[end])) end += 1;
      const reference = text.slice(cursor, end);
      const label = evidenceLabel(reference, [material]).replaceAll('\\', '\\\\').replaceAll('[', '\\[').replaceAll(']', '\\]');
      const replacement = `[${label}](${evidenceHref(reference)})`;
      text = `${text.slice(0, cursor)}${replacement}${text.slice(end)}`;
      cursor += replacement.length;
    }
  }
  return text;
}
