import type { DatabaseSync } from 'node:sqlite';

export interface CandidateChangeRow {
  path: string;
  kind: 'write' | 'delete';
  content: string | null;
}

export function isNoOpCandidateChange(
  change: CandidateChangeRow,
  baseFiles: Record<string, string>,
): boolean {
  return change.kind === 'write'
    ? baseFiles[change.path] === change.content
    : !Object.hasOwn(baseFiles, change.path);
}

export function removeNoOpCandidateChanges<T extends CandidateChangeRow>(
  db: DatabaseSync,
  projectId: string,
  runId: string,
  changes: readonly T[],
  baseFiles: Record<string, string>,
): T[] {
  const removedPaths = new Set(changes
    .filter((change) => isNoOpCandidateChange(change, baseFiles))
    .map((change) => change.path));
  if (!removedPaths.size) return [...changes];
  const remove = db.prepare(`DELETE FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? AND path = ?`);
  for (const path of removedPaths) remove.run(projectId, runId, path);
  return changes.filter((change) => !removedPaths.has(change.path));
}
