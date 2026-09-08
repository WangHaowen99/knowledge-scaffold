export const INTERNAL_PACKAGE_PATH_PREFIX = '.modeling/';

export function isInternalPackagePath(path: string): boolean {
  return path === '.modeling' || path.startsWith(INTERNAL_PACKAGE_PATH_PREFIX);
}

export function publicKnowledgePackageFiles<T>(files: Record<string, T>): Record<string, T> {
  return Object.fromEntries(
    Object.entries(files).filter(([path]) => !isInternalPackagePath(path)),
  );
}
