// 页面上还有没保存的编辑（对比编辑器等）：各编辑器按名字登记，有任何一个没保存时不发起版本回退
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
