const TYPE_META = {
  md: ['MD', 'document'], markdown: ['MD', 'document'],
  yaml: ['YAML', 'data'], yml: ['YAML', 'data'], json: ['JSON', 'data'], csv: ['CSV', 'data'],
  js: ['JS', 'code'], jsx: ['JSX', 'code'], ts: ['TS', 'code'], tsx: ['TSX', 'code'],
  py: ['PY', 'code'], sh: ['SH', 'code'], sql: ['SQL', 'code'],
  png: ['IMG', 'asset'], jpg: ['IMG', 'asset'], jpeg: ['IMG', 'asset'], gif: ['IMG', 'asset'], webp: ['IMG', 'asset'], svg: ['SVG', 'asset'],
  pdf: ['PDF', 'asset'], zip: ['ZIP', 'asset'],
};

function normalizePath(path) {
  return String(path || '').replaceAll('\\', '/').replace(/^\/+|\/+$/g, '');
}

function typeMeta(path) {
  const filename = path.split('/').at(-1) || '';
  const dot = filename.lastIndexOf('.');
  const extension = dot > 0 ? filename.slice(dot + 1).toLowerCase() : '';
  const [typeLabel, typeKind] = TYPE_META[extension] ?? [extension.slice(0, 4).toUpperCase() || 'FILE', 'file'];
  return { typeLabel, typeKind };
}

function compareNodes(left, right) {
  if (left.type !== right.type) return left.type === 'directory' ? -1 : 1;
  return left.name.localeCompare(right.name, 'zh-CN', { numeric: true, sensitivity: 'base' });
}

export function buildPackageFileTree(files, binaryPaths = [], overriddenPaths = []) {
  const binary = new Set(binaryPaths.map(normalizePath));
  const overridden = new Set(overriddenPaths.map(normalizePath));
  const root = { children: new Map() };

  for (const input of files) {
    const path = normalizePath(input);
    if (!path) continue;
    const segments = path.split('/').filter(Boolean);
    let parent = root;
    let parentPath = '';
    segments.forEach((name, index) => {
      const itemPath = parentPath ? `${parentPath}/${name}` : name;
      const isFile = index === segments.length - 1;
      if (isFile) {
        parent.children.set(`file:${name}`, {
          type: 'file', name, path: itemPath,
          binary: binary.has(itemPath), modified: overridden.has(itemPath),
          ...typeMeta(itemPath),
        });
      } else {
        const key = `directory:${name}`;
        if (!parent.children.has(key)) {
          parent.children.set(key, { type: 'directory', name, path: itemPath, children: new Map() });
        }
        parent = parent.children.get(key);
        parentPath = itemPath;
      }
    });
  }

  const finish = (items) => [...items.values()].map((node) => {
    if (node.type === 'file') return node;
    const children = finish(node.children).sort(compareNodes);
    return {
      type: 'directory', name: node.name, path: node.path, children,
      fileCount: children.reduce((total, child) => total + (child.type === 'directory' ? child.fileCount : 1), 0),
      modifiedCount: children.reduce((total, child) => total + (child.type === 'directory' ? child.modifiedCount : Number(child.modified)), 0),
    };
  });
  return finish(root.children).sort(compareNodes);
}

export function directoryPathsForFile(path) {
  const parts = normalizePath(path).split('/').filter(Boolean).slice(0, -1);
  return parts.map((_, index) => parts.slice(0, index + 1).join('/'));
}

export function listDirectoryPaths(tree) {
  const paths = [];
  const visit = (nodes) => nodes.forEach((node) => {
    if (node.type !== 'directory') return;
    paths.push(node.path);
    visit(node.children);
  });
  visit(tree);
  return paths;
}
