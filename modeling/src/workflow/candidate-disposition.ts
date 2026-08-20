import { createHash } from 'node:crypto';
import type {
  ArtifactCandidate,
  CandidateDisposition,
  SpecObject,
  SpecOperation,
  SpecMetric,
  SpecRule,
} from '../types.js';
import { tableSourcesFor } from './source-references.js';

export interface PlannedArtifactCandidate extends ArtifactCandidate {
  id: string;
}

const normalized = (value: string) => value.normalize('NFKC')
  .toLocaleLowerCase('zh-CN')
  .replace(/[^\p{L}\p{N}]/gu, '');

export function candidateId(candidate: ArtifactCandidate, index: number): string {
  const digest = createHash('sha1')
    .update(JSON.stringify([candidate.kind, candidate.name, candidate.sources]))
    .digest('hex')
    .slice(0, 12);
  return `artifact_${index + 1}_${digest}`;
}

export function planArtifactCandidates(candidates: ArtifactCandidate[]): {
  selected: PlannedArtifactCandidate[];
  dispositions: CandidateDisposition[];
};
export function planArtifactCandidates(
  candidates: ArtifactCandidate[],
  selectedCandidates: ArtifactCandidate[],
): { selected: PlannedArtifactCandidate[]; dispositions: CandidateDisposition[] };
export function planArtifactCandidates(
  candidates: ArtifactCandidate[],
  selectedCandidates: ArtifactCandidate[] = candidates,
): { selected: PlannedArtifactCandidate[]; dispositions: CandidateDisposition[] } {
  const selected: PlannedArtifactCandidate[] = [];
  const dispositions: CandidateDisposition[] = [];
  const selectedSignatures = new Set(selectedCandidates.map((candidate) => JSON.stringify([
    candidate.kind, normalized(candidate.name), [...candidate.sources].sort(), candidate.readiness,
  ])));
  const canonicalByKindAndName = new Map<string, string>();
  const entries = candidates.map((candidate, index) => {
    const signature = JSON.stringify([
      candidate.kind, normalized(candidate.name), [...candidate.sources].sort(), candidate.readiness,
    ]);
    return { candidate, index, signature, selected: selectedSignatures.has(signature) };
  }).sort((left, right) => Number(right.selected) - Number(left.selected) || left.index - right.index);
  entries.forEach(({ candidate, index, signature, selected: isSelected }) => {
    const id = candidateId(candidate, index);
    const key = `${candidate.kind}:${normalized(candidate.name)}`;
    const previous = canonicalByKindAndName.get(key);
    if (previous) {
      dispositions.push({
        candidateId: id, kind: candidate.kind, name: candidate.name,
        status: 'merged', mergedIntoCandidateId: previous,
        reason: '名称规范化后与已选候选重复，合并证据后由同一资源处理。',
      });
      return;
    }
    canonicalByKindAndName.set(key, id);
    if (candidate.readiness === 'needs_clarification') {
      dispositions.push({
        candidateId: id, kind: candidate.kind, name: candidate.name,
        status: 'needs_clarification', reason: '资料分析已标记为需要补充业务口径，本轮不编造事实。',
      });
      return;
    }
    if (!isSelected) {
      dispositions.push({
        candidateId: id, kind: candidate.kind, name: candidate.name,
        status: 'deferred', reason: '与当前确认流程和首版结构化数据范围无直接绑定，保留到后续增量建模。',
      });
      return;
    }
    const planned = { ...candidate, id };
    selected.push(planned);
  });
  return { selected, dispositions };
}

export function validateKnowledgeCandidateDispositions(
  candidates: PlannedArtifactCandidate[],
  rules: SpecRule[],
  metrics: SpecMetric[],
  dispositions: CandidateDisposition[],
): string[] {
  const issues: string[] = [];
  const candidateIds = new Set(candidates.map((candidate) => candidate.id));
  const seen = new Set<string>();
  for (const disposition of dispositions) {
    if (!candidateIds.has(disposition.candidateId)) {
      issues.push(`处置记录引用了当前批次不存在的候选 ${disposition.candidateId}`);
    }
    if (seen.has(disposition.candidateId)) issues.push(`候选 ${disposition.candidateId} 重复提交处置记录`);
    seen.add(disposition.candidateId);
  }
  const byCandidateId = new Map(dispositions.map((item) => [item.candidateId, item]));
  const ruleKeys = new Set(rules.map((item) => item.key));
  const metricKeys = new Set(metrics.map((item) => item.key));
  for (const candidate of candidates) {
    const disposition = byCandidateId.get(candidate.id);
    if (!disposition) {
      issues.push(`候选「${candidate.name}」没有生成、合并、延期、待澄清或排除记录`);
      continue;
    }
    if (disposition.kind !== candidate.kind || disposition.name !== candidate.name) {
      issues.push(`候选「${candidate.name}」的处置记录与候选身份不一致`);
    }
    if (disposition.status === 'generated') {
      const keys = disposition.resourceKeys ?? [];
      const resources = candidate.kind === 'rule' ? ruleKeys : metricKeys;
      if (!keys.length || keys.some((key) => !resources.has(key))) {
        issues.push(`候选「${candidate.name}」标记为已生成，但没有引用本批实际生成的资源 key`);
      }
    }
    if (candidate.readiness === 'extractable'
      && (disposition.status === 'deferred' || disposition.status === 'excluded')) {
      const generic = /(?:留待|后续|增量建模|首版|当前(?:确认)?流程(?:未使用|无直接)|暂不处理)/u;
      const hasConcreteBoundary = /(?:对象|字段|流程步骤|业务步骤|目标|输入|输出|场景|角色|证据|口径)/u
        .test(disposition.reason);
      if (disposition.reason.trim().length < 20 || generic.test(disposition.reason) || !hasConcreteBoundary) {
        issues.push(`候选「${candidate.name}」的延期/排除理由过于笼统，必须说明与当前对象、流程步骤或建模目标不匹配的具体原因`);
      }
    }
    if (disposition.status === 'merged' && (
      !disposition.mergedIntoCandidateId
      || disposition.mergedIntoCandidateId === candidate.id
      || !candidateIds.has(disposition.mergedIntoCandidateId)
    )) {
      issues.push(`候选「${candidate.name}」标记为合并，但没有指定当前批次内的归并目标`);
    }
    if (!disposition.reason.trim()) issues.push(`候选「${candidate.name}」的处置原因不能为空`);
  }
  return issues;
}

/**
 * 防止模型在当前批次之外凭空新增规则/指标。资源可以把一个候选拆成
 * 多个更精确的资源（例如一个候选拆成“数量”和“金额”两个指标），但
 * 每个提交资源都必须被某条 generated 处置记录明确认领。
 */
export function validateGeneratedKnowledgeResources(
  rules: Array<{ key: string; title: string }>,
  metrics: Array<{ key: string; title: string }>,
  dispositions: CandidateDisposition[],
): string[] {
  const generatedKeys = new Set(dispositions
    .filter((item) => item.status === 'generated')
    .flatMap((item) => item.resourceKeys ?? []));
  return [
    ...rules.filter((item) => !generatedKeys.has(item.key))
      .map((item) => `规则 ${item.key} 未被任何候选处置记录认领，不能作为无来源新规则提交`),
    ...metrics.filter((item) => !generatedKeys.has(item.key))
      .map((item) => `指标 ${item.key} 未被任何候选处置记录认领，不能作为无来源新指标提交`),
  ];
}

function matchingResourceKeys(
  candidate: ArtifactCandidate,
  resources: Array<{ key: string; title: string; sources?: string[] }>,
): string[] {
  const candidateName = normalized(candidate.name);
  const sourceSet = new Set(candidate.sources);
  return resources.filter((resource) => {
    const resourceName = normalized(resource.title);
    if (candidateName && resourceName && (
      candidateName === resourceName
      || candidateName.includes(resourceName)
      || resourceName.includes(candidateName)
    )) return true;
    return resource.sources?.some((source) => sourceSet.has(source)) ?? false;
  }).map((resource) => resource.key);
}

export function dispositionForObjectAndOperationCandidates(
  candidates: ArtifactCandidate[],
  objects: SpecObject[],
  operations: SpecOperation[],
): CandidateDisposition[] {
  const objectResources = objects.map((object) => ({
    key: object.key,
    title: object.title,
    sources: tableSourcesFor(object).map((source) => source.table),
  }));
  const operationResources = operations.map((operation) => ({ key: operation.key, title: operation.title }));
  const dispositions: CandidateDisposition[] = [];
  candidates.forEach((candidate, index) => {
    if (candidate.kind !== 'object' && candidate.kind !== 'operation') return [];
    const id = candidateId(candidate, index);
    const matches = matchingResourceKeys(
      candidate,
      candidate.kind === 'object' ? objectResources : operationResources,
    );
    if (matches.length) {
      dispositions.push({
        candidateId: id, kind: candidate.kind, name: candidate.name,
        status: 'generated' as const, resourceKeys: matches,
        reason: candidate.kind === 'object'
          ? '已由物理 Sheet 对象或首版逻辑对象覆盖。'
          : '已由确认流程生成的 operation 覆盖。',
      });
      return;
    }
    dispositions.push({
      candidateId: id, kind: candidate.kind, name: candidate.name,
      status: candidate.readiness === 'needs_clarification'
        ? 'needs_clarification' as const
        : 'deferred' as const,
      reason: candidate.readiness === 'needs_clarification'
        ? '资料分析已标记为需要补充业务口径，本轮不编造事实。'
        : '与当前确认流程和首版 Schema 无直接匹配，保留到后续增量建模。',
    });
  });
  return dispositions;
}
