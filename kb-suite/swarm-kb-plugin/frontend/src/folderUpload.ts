// Directory inputs include the selected root and every subdirectory in webkitRelativePath.
export function parentUploadFolder(file: Pick<File, 'name' | 'webkitRelativePath'>): string {
  const path = file.webkitRelativePath;
  const parts = path.split('/');
  if (
    parts.length < 2 || parts[parts.length - 1] !== file.name ||
    /^[a-z]:/i.test(path) || /[\\\x00-\x1f\x7f]/.test(path) ||
    parts.some((part) => !part || part === '.' || part === '..')
  ) {
    throw new Error('无法读取文件的相对路径，请重新选择文件夹');
  }
  return parts.slice(0, -1).join('/');
}
