const CJK_TEXT = /[\u3400-\u9fff]/u;

export function decodeLegacyArchiveName(value) {
  const name = String(value ?? '');
  if (!/[^\u0000-\u007f]/u.test(name)) return name;
  const bytes = [];
  for (const character of name) {
    const code = character.codePointAt(0) ?? 0;
    if (code > 0xff) return name;
    bytes.push(code);
  }
  try {
    const decoded = new TextDecoder('gb18030', { fatal: true }).decode(Uint8Array.from(bytes));
    return CJK_TEXT.test(decoded) ? decoded : name;
  } catch {
    return name;
  }
}

export function displayPackageEntryName(path) {
  const decoded = decodeLegacyArchiveName(path).replace(/\/$/u, '');
  const parts = decoded.split('/');
  const base = parts.pop() || decoded;
  if (base === 'index.md' && parts.length) return `${parts[parts.length - 1]}/index.md`;
  return base;
}
