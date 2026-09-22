export const MAX_DISPLAY_NAME_LENGTH = 200;

export interface NameConflict {
  originalName: string;
  attemptedName: string;
  candidates: string[];
}

export function normalizedDisplayName(value: unknown): string {
  return String(value ?? '').trim().normalize('NFKC').toLowerCase();
}

export function displayNameCandidate(baseName: string, sequence: number): string {
  const suffix = `（${sequence}）`;
  const availableLength = Math.max(1, MAX_DISPLAY_NAME_LENGTH - [...suffix].length);
  const prefix = [...baseName].slice(0, availableLength).join('').trimEnd();
  return `${prefix}${suffix}`;
}

/**
 * 检测 attemptedName 是否与 occupiedNames 冲突；冲突时基于 originalName 生成可选的候选名称。
 * 候选名称始终以原始名称为基准，改名失败后再次弹窗时候选保持稳定。
 */
export function detectNameConflict(
  occupiedNames: Iterable<string>,
  attemptedName: string,
  originalName: string,
): NameConflict | null {
  const occupied = new Set([...occupiedNames].map(normalizedDisplayName));
  const attempted = String(attemptedName ?? '').trim();
  if (!occupied.has(normalizedDisplayName(attempted))) return null;

  const original = String(originalName ?? '').trim();
  const baseName = original.replace(/\s*[（(]\d+[）)]$/u, '').trim() || original;
  const candidates: string[] = [];
  for (let sequence = 2; sequence < 10_002 && candidates.length < 3; sequence += 1) {
    const candidate = displayNameCandidate(baseName, sequence);
    if (!occupied.has(normalizedDisplayName(candidate))) candidates.push(candidate);
  }
  return { originalName: original, attemptedName: attempted, candidates };
}
