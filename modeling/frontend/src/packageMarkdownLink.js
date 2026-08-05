const EXTERNAL_SCHEME = /^[A-Za-z][A-Za-z\d+.-]*:/u;

export function isPackageMarkdownHref(href) {
  const value = String(href ?? '').trim();
  return Boolean(value)
    && !value.startsWith('#')
    && !value.startsWith('/')
    && !value.startsWith('\\')
    && !value.startsWith('//')
    && !EXTERNAL_SCHEME.test(value);
}

function decodedPath(href) {
  const pathOnly = String(href).split('#', 1)[0].split('?', 1)[0];
  try {
    return decodeURIComponent(pathOnly);
  } catch {
    return '';
  }
}

export function resolvePackageMarkdownTarget(currentPath, href, files = []) {
  if (!isPackageMarkdownHref(href)) return '';
  const relative = decodedPath(href);
  if (!relative || relative.includes('\\')) return '';
  const segments = String(currentPath ?? '').split('/').slice(0, -1).filter(Boolean);
  for (const segment of relative.split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..') {
      if (!segments.length) return '';
      segments.pop();
      continue;
    }
    segments.push(segment.normalize('NFC'));
  }
  const target = segments.join('/');
  return Array.isArray(files) && files.includes(target) ? target : '';
}
