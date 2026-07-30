import type { PackageArtifactFiles, PackageRevisionAction } from '../artifacts/package-store.js';
import type { ArtifactChange } from '../artifacts/repository.js';
import type { CoverageResult, PackageSpec } from '../types.js';
import { renderPackage } from '../workflow/package-render.js';

export interface OptimizationCandidateChangeGroupInput {
  id: string;
  summary: string;
  changes: readonly ArtifactChange[];
  validation?: { errors?: readonly string[]; warnings?: readonly string[] };
}

export interface OptimizationCandidateStore {
  createCandidateRevision(projectId: string, input: {
    expectedRevision: string;
    groups: readonly OptimizationCandidateChangeGroupInput[];
    action: PackageRevisionAction;
  }): Promise<{ id: string; status: string }>;
  rejectCandidateRevision(projectId: string, input: {
    candidateId: string;
    note?: string;
    action: PackageRevisionAction;
  }): Promise<unknown>;
}

function jsonFile(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

export function buildOptimizationArtifactChanges(
  current: PackageArtifactFiles,
  spec: PackageSpec,
  coverage: CoverageResult,
): ArtifactChange[] {
  if (spec.version !== current.spec.version) {
    throw new Error('优化候选不能旁路提升语义版本');
  }
  const changes: ArtifactChange[] = [];
  const currentSpec = jsonFile(current.spec);
  const nextSpec = jsonFile(spec);
  if (currentSpec !== nextSpec) {
    changes.push({ kind: 'write', path: 'model/package-spec.json', content: nextSpec });
  }
  const currentCoverage = jsonFile(current.coverage);
  const nextCoverage = jsonFile(coverage);
  if (currentCoverage !== nextCoverage) {
    changes.push({ kind: 'write', path: 'model/coverage.json', content: nextCoverage });
  }

  const currentRendered = renderPackage(current.spec);
  const nextRendered = renderPackage(spec);
  const removedManagedPaths = Object.keys(currentRendered)
    .filter((path) => nextRendered[path] === undefined);
  if (removedManagedPaths.length) {
    throw new Error(`优化候选不能删除知识包文件：${removedManagedPaths.join('、')}`);
  }
  for (const [path, content] of Object.entries(nextRendered).sort(([left], [right]) => left.localeCompare(right))) {
    if (current.files[path] !== content) {
      changes.push({ kind: 'write', path: `package/${path}`, content });
    }
  }
  if (!changes.length) throw new Error('优化结果与当前工件修订完全相同');
  return changes;
}

export async function createOptimizationCandidate(input: {
  store: OptimizationCandidateStore;
  projectId: string;
  current: PackageArtifactFiles;
  spec: PackageSpec;
  coverage: CoverageResult;
  suggestionIds: string[];
  summary: string;
  linkCandidate(candidateId: string): void;
}): Promise<{ id: string; status: string }> {
  const candidate = await input.store.createCandidateRevision(input.projectId, {
    expectedRevision: input.current.revision.oid,
    groups: [{
      id: 'optimization',
      summary: `落实所选 ${input.suggestionIds.length} 条优化建议`,
      changes: buildOptimizationArtifactChanges(input.current, input.spec, input.coverage),
      validation: { errors: [], warnings: [] },
    }],
    action: {
      cause: 'optimization_candidate',
      summary: input.summary,
      actorKind: 'agent',
      actorName: '智能助手',
    },
  });
  try {
    input.linkCandidate(candidate.id);
  } catch (error) {
    await input.store.rejectCandidateRevision(input.projectId, {
      candidateId: candidate.id,
      note: '关联优化建议失败，候选已自动关闭',
      action: {
        cause: 'optimization_candidate_link_failed',
        summary: '关闭未能关联建议的优化候选',
        actorKind: 'system',
        actorName: '建模系统',
      },
    });
    throw error;
  }
  return candidate;
}
