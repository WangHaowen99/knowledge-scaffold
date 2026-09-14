export function resolvePackageFileTarget(files = [], target = '') {
  const value = String(target ?? '').trim();
  if (!value) return '';

  const typed = value.match(/^(object|operation|rule|metric|script|mapping)s?:(.+)$/i);
  const semanticRoot = value.split(/\.(?:fields|operations|rules|metrics|scripts|mappings)\./i)[0];
  const keys = [...new Set([
    value,
    typed?.[2]?.trim(),
    semanticRoot !== value ? semanticRoot : '',
  ].filter(Boolean))];
  const group = (() => { if (typed?.[1]) return `${typed[1].toLowerCase()}${(() => { if (typed[1].toLowerCase().endsWith('s')) return ''; return 's'; })()}`; return ((() => { if (/^(objects|operations|rules|metrics|scripts|mappings)$/i.test(value)) return value.toLowerCase(); return ''; })()); })();

  return files.find((path) => path === value)
    ?? keys.flatMap((key) => [
      files.find((path) => path.endsWith(`/${key}.md`)),
      files.find((path) => path.endsWith(`/${key}.yaml`)),
      files.find((path) => path.endsWith(`/${key}.yml`)),
      files.find((path) => path.endsWith(`/${key}`)),
    ]).find(Boolean)
    ?? (group ? files.find((path) => path === `${group}/index.md`) : undefined)
    ?? (group ? files.find((path) => path.startsWith(`${group}/`)) : undefined)
    ?? keys.flatMap((key) => files.filter((path) => path.includes(key))).at(0)
    ?? '';
}
