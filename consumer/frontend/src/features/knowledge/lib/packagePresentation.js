export function packageDisplayLabel(pkg) {
  const name = String(pkg?.name || '').trim();
  const id = String(pkg?.id || '').trim();
  if (name && id && name !== id) {
    return `${name}（${id}）`;
  }
  return name || id;
}
