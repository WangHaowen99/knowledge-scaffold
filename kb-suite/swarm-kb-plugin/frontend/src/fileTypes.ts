/** Why a file of this name cannot go to a backend that declares `fileTypes`; null when it can (or nothing is declared). */
export function unsupportedType(name: string, fileTypes: string[] | undefined): string | null {
  if (!fileTypes?.length) return null;
  const dot = name.lastIndexOf('.');
  const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  if (ext && fileTypes.includes(ext)) return null;
  const allowed = fileTypes.map((t) => t.toUpperCase()).join('、');
  return ext ? `不支持 .${ext} 文件，这个知识库支持 ${allowed}` : `文件没有扩展名，无法识别类型；这个知识库支持 ${allowed}`;
}
