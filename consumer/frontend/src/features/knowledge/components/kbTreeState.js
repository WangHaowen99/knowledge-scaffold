const EXPANSION_STORAGE_PREFIX = 'mw_kb_tree_expanded';

const normalizedWorkspaceId = (workspaceId) => String(workspaceId || '').trim();

export const kbTreeExpansionStorageKey = (workspaceId) => (
  `${EXPANSION_STORAGE_PREFIX}:${encodeURIComponent(normalizedWorkspaceId(workspaceId))}`
);

export const packageExpansionKey = (packageId) => (
  `package:${String(packageId || '').trim()}`
);

export const directoryExpansionKey = (packageId, version, path) => (
  `directory:${String(packageId || '').trim()}:${String(version || '').trim()}:${String(path || '').trim()}`
);

export const versionCollapseKey = (packageId, version) => (
  `version-collapsed:${String(packageId || '').trim()}:${String(version || '').trim()}`
);

export const isVersionExpanded = (selected, stateKeys, packageId, version) => (
  selected && !stateKeys.has(versionCollapseKey(packageId, version))
);

export function packageVersions(pkg) {
  const activeVersion = String(pkg?.active_version || pkg?.version || '').trim();
  const versions = [activeVersion, pkg?.version, ...(pkg?.versions || [])]
    .map((version) => String(version || '').trim())
    .filter(Boolean);
  return [...new Set(versions)].sort((left, right) => {
    if (left === activeVersion) return -1;
    if (right === activeVersion) return 1;
    return right.localeCompare(left, undefined, { numeric: true });
  });
}

export function packageDisplayLabel(pkg) {
  const name = String(pkg?.name || '').trim();
  const id = String(pkg?.id || '').trim();
  if (name && id && name !== id) return `${name}（${id}）`;
  return name || id;
}

export function readKbTreeExpansion(storage, workspaceId) {
  if (!storage || !normalizedWorkspaceId(workspaceId)) return null;
  try {
    const raw = storage.getItem(kbTreeExpansionStorageKey(workspaceId));
    if (raw === null) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.some((key) => typeof key !== 'string')) return null;
    return new Set(parsed);
  } catch {
    return null;
  }
}

export function writeKbTreeExpansion(storage, workspaceId, expandedKeys) {
  if (!storage || !normalizedWorkspaceId(workspaceId)) return false;
  try {
    storage.setItem(
      kbTreeExpansionStorageKey(workspaceId),
      JSON.stringify([...expandedKeys].sort()),
    );
    return true;
  } catch {
    return false;
  }
}

export function toggleExpandedKey(expandedKeys, key) {
  const next = new Set(expandedKeys);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}

export function buildVisibleTreeRows(entries, expandedKeys, packageId, version, depth = 0) {
  const rows = [];
  for (const entry of entries || []) {
    const children = Array.isArray(entry.children) ? entry.children : [];
    const expansionKey = directoryExpansionKey(packageId, version, entry.path);
    const hasChildren = entry.type === 'dir' && children.length > 0;
    const expanded = hasChildren && expandedKeys.has(expansionKey);
    rows.push({ entry, depth, expansionKey, expanded, hasChildren });
    if (expanded) {
      rows.push(...buildVisibleTreeRows(
        children,
        expandedKeys,
        packageId,
        version,
        depth + 1,
      ));
    }
  }
  return rows;
}
