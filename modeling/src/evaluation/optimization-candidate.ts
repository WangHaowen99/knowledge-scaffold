import {
  PackageArtifactStoreError,
  type PackageArtifactFiles,
  type PackageArtifactStore,
  type PackageCandidateRevision,
} from '../artifacts/package-store.js';
import type {
  ArtifactChange,
} from '../artifacts/repository.js';
import type { CoverageResult, PackageSpec } from '../types.js';
import * as diagnosisRepo from './failure-diagnosis-repo.js';
import * as m4repo from '../workflow/m4-repo.js';
import { renderPackage } from '../workflow/package-render.js';

export type OptimizationCandidateStore = Pick<
  PackageArtifactStore,
  'createCandidateRevision' | 'rejectCandidateRevision'
>;

type OptimizationCandidateLifecycleStore = Pick<
  PackageArtifactStore,
  'getCandidateRevision' | 'readPackageFiles' | 'rejectCandidateRevision'
>;

export interface OptimizationCandidateDiagnosisLinkState {
  linked: boolean;
  current: boolean;
  reason?: string;
}

export interface OptimizationCandidateApplyResponse {
  candidateId: string;
  status: PackageCandidateRevision['status'];
  selected: number;
  basedRevisionOid: string;
}

export function optimizationCandidateApplyResponse(
  candidate: Pick<PackageCandidateRevision, 'id' | 'status'>,
  selected: number,
  basedRevisionOid: string,
): OptimizationCandidateApplyResponse {
  return {
    candidateId: candidate.id,
    status: candidate.status,
    selected,
    basedRevisionOid,
  };
}

export function optimizationCandidateDiagnosisLinkState(input: {
  projectId: string;
  candidateId: string;
  baseRevisionOid: string;
}): OptimizationCandidateDiagnosisLinkState {
  const suggestions = m4repo.listCandidateSuggestions(input.projectId, input.candidateId);
  if (!suggestions.length) return { linked: false, current: true };
  const roundIds = [...new Set(suggestions.map((suggestion) => suggestion.roundId))];
  if (roundIds.length !== 1) {
    return { linked: true, current: false, reason: '候选关联了不同评测轮次的优化建议' };
  }
  const diagnoses = diagnosisRepo.listFailureDiagnoses(input.projectId, roundIds[0]);
  const currentDiagnoses = new Map(diagnoses.map((diagnosis) => [diagnosis.id, diagnosis]));
  for (const suggestion of suggestions) {
    if (suggestion.status !== 'candidate') {
      return { linked: true, current: false, reason: '候选关联的优化建议已不再等待候选确认' };
    }
    if (suggestion.basedRevisionOid !== input.baseRevisionOid) {
      return { linked: true, current: false, reason: '候选关联的优化建议基线不一致' };
    }
    if (!(suggestion.diagnosisIds ?? []).length) {
      return { linked: true, current: false, reason: '候选关联的优化建议缺少失败诊断依据' };
    }
    for (const diagnosisId of suggestion.diagnosisIds ?? []) {
      const diagnosis = currentDiagnoses.get(diagnosisId);
      if (!diagnosis || diagnosis.basedRevisionOid !== suggestion.basedRevisionOid) {
        return { linked: true, current: false, reason: '候选关联的失败诊断已被重跑结果替换' };
      }
      if (diagnosis.resolutionStatus !== 'confirmed'
        || !diagnosis.packageOptimizable
        || diagnosis.blocking) {
        return { linked: true, current: false, reason: '候选关联的失败诊断不再允许修改知识包' };
      }
    }
  }
  return { linked: true, current: true };
}

export async function retireOptimizationCandidatesForDiagnosisRerun(input: {
  store: OptimizationCandidateLifecycleStore;
  projectId: string;
  roundId: string;
}): Promise<{ candidateIds: string[]; rejectedSuggestions: number }> {
  const candidateIds = [...new Set(m4repo.listSuggestions(input.projectId, input.roundId)
    .filter((suggestion) => ['pending', 'candidate'].includes(suggestion.status))
    .map((suggestion) => suggestion.candidateRevisionId)
    .filter((candidateId): candidateId is string => !!candidateId))];

  for (const candidateId of candidateIds) {
    let candidate: PackageCandidateRevision;
    try {
      candidate = await input.store.getCandidateRevision(input.projectId, candidateId);
    } catch (error) {
      if (error instanceof PackageArtifactStoreError && error.code === 'candidate_not_found') {
        m4repo.rejectCandidateRevision(input.projectId, candidateId);
        continue;
      }
      throw error;
    }
    if (candidate.status === 'pending') {
      try {
        candidate = await input.store.rejectCandidateRevision(input.projectId, {
          candidateId,
          note: '同一评测轮次已重跑失败诊断，旧优化候选不再有效',
          action: {
            cause: 'optimization_diagnosis_refreshed',
            summary: '重跑失败诊断后关闭旧优化候选',
            actorKind: 'system',
            actorName: '建模系统',
          },
        });
      } catch (error) {
        if (!(error instanceof PackageArtifactStoreError) || error.code !== 'candidate_not_pending') {
          throw error;
        }
        candidate = await input.store.getCandidateRevision(input.projectId, candidateId);
      }
    }
    if (candidate.status === 'accepted') {
      if (!candidate.acceptedRevision) throw new Error('已接受候选缺少正式工件修订');
      const accepted = await input.store.readPackageFiles(input.projectId, candidate.acceptedRevision);
      m4repo.markCandidateRevisionApplied(
        input.projectId, candidateId, accepted.spec.version, candidate.acceptedRevision,
      );
    } else {
      m4repo.rejectCandidateRevision(input.projectId, candidateId);
    }
  }

  return {
    candidateIds,
    rejectedSuggestions: m4repo.rejectOpenSuggestionsForRound(input.projectId, input.roundId),
  };
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
    .filter((path) => nextRendered[path] === undefined)
    .sort((left, right) => left.localeCompare(right));
  for (const path of removedManagedPaths) {
    if (current.files[path] !== undefined) {
      changes.push({ kind: 'delete', path: `package/${path}` });
    }
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
}): Promise<PackageCandidateRevision> {
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
