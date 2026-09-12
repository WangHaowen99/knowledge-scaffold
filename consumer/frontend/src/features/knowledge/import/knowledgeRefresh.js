export function nextKnowledgeRevision(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value + 1 : 1;
}

export function resolveKnowledgeVersionOverride(current, followActive = false) {
  return followActive ? null : current;
}

export function treeContainsEntry(entries, path, type = '') {
  if (!path) {
    return true;
  }
  return (entries || []).some(
    (entry) =>
      (entry.path === path && (!type || entry.type === type)) ||
      treeContainsEntry(entry.children, path, type),
  );
}

export function reconcileTreeSelection(entries, previous, fallbackDir = '') {
  if (previous?.file && treeContainsEntry(entries, previous.file, 'file')) {
    return previous;
  }
  if (previous?.dir && treeContainsEntry(entries, previous.dir, 'dir')) {
    return { dir: previous.dir, file: null };
  }
  return { dir: fallbackDir, file: null };
}
