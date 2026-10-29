const sources = new Set();

export function setWorkspaceDirty(source, dirty) {
  if (dirty) sources.add(source);
  else sources.delete(source);
}

export function clearWorkspaceDirty() {
  sources.clear();
}

export function hasWorkspaceDirty() {
  return sources.size > 0;
}

export function workspaceDirtySources() {
  return [...sources].sort();
}
