export function resolvePackageFileTarget(files = [], target = '') {
  const value = String(target ?? '').trim();
  if (!value) return '';
  return files.find((path) => path === value)
    ?? files.find((path) => path.endsWith(`/${value}.md`))
    ?? files.find((path) => path.endsWith(`/${value}`))
    ?? files.find((path) => path.includes(value))
    ?? '';
}
