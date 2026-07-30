import type {
  AnalysisResult,
  ArtifactCandidate,
  PackageSpec,
  SpecMetric,
  SpecObject,
  SpecOperation,
  SpecRule,
} from '../types.js';
import { sourceRefsFor } from './source-references.js';

export interface MaterialEvidenceCatalog {
  documents?: Array<{ id: string; filename: string }>;
  tables?: Array<{ name: string; documentId?: string }>;
}

function unique(values: Array<string | undefined>): string[] {
  return [...new Set(values.map((value) => String(value ?? '').trim()).filter(Boolean))];
}

function identity(value: string): string {
  return value.normalize('NFKC').toLocaleLowerCase('zh-CN').replace(/[^\p{Letter}\p{Number}]+/gu, '');
}

function objectEvidence(object: SpecObject): string[] {
  return unique(sourceRefsFor(object).flatMap((source) => {
    if (source.kind === 'table') return [
      `table:${source.table}`,
      ...(source.documentId ? [`material-revision:${source.documentId}`] : []),
    ];
    const anchors = source.anchors?.map((anchor) => anchor.heading || anchor.quote) ?? [];
    const references = anchors.length
      ? anchors.map((anchor) => `material:${source.filename}#${anchor}`)
      : [`material:${source.filename}`];
    return [...references, ...(source.documentId ? [`material-revision:${source.documentId}`] : [])];
  }));
}

function candidateEvidence(
  candidate: ArtifactCandidate | undefined,
  catalog: MaterialEvidenceCatalog | undefined,
): string[] {
  if (!candidate) return [];
  return unique([
    ...candidate.sources.map((source) => `material:${source}`),
    ...candidate.sources.flatMap((source) => catalog?.documents
      ?.filter((document) => document.filename === source)
      .map((document) => `material-revision:${document.id}`) ?? []),
    ...(candidate.evidence ? [`anchor:${candidate.evidence}`] : []),
  ]);
}

function attachCandidateEvidence<T extends SpecRule | SpecMetric>(
  resources: T[],
  candidates: ArtifactCandidate[],
  catalog?: MaterialEvidenceCatalog,
): T[] {
  const unused = new Set(candidates.map((_, index) => index));
  return resources.map((resource, resourceIndex) => {
    if (resource.evidence?.length) return resource;
    const key = identity(resource.title || resource.key);
    let candidateIndex = candidates.findIndex((candidate, index) => (
      unused.has(index) && identity(candidate.name) === key
    ));
    if (candidateIndex < 0) candidateIndex = [...unused][resourceIndex] ?? [...unused][0] ?? -1;
    if (candidateIndex >= 0) unused.delete(candidateIndex);
    return { ...resource, evidence: candidateEvidence(candidates[candidateIndex], catalog) };
  });
}

function attachObjectDocumentRevisions(
  object: SpecObject,
  catalog: MaterialEvidenceCatalog | undefined,
): SpecObject {
  if (!object.sourceRefs?.length || !catalog) return object;
  return {
    ...object,
    sourceRefs: object.sourceRefs.map((source) => {
      if (source.documentId) return source;
      if (source.kind === 'document') {
        const document = catalog.documents?.find((item) => item.filename === source.filename);
        return document ? { ...source, documentId: document.id } : source;
      }
      const table = catalog.tables?.find((item) => item.name === source.table && item.documentId);
      return table?.documentId ? { ...source, documentId: table.documentId } : source;
    }),
  };
}

function attachOperationEvidence(
  operations: SpecOperation[],
  diagrams: Array<{ id: string; nodes: Array<{ id: string; kind: string }> }>,
): SpecOperation[] {
  const diagramsByQuestionId = new Map(diagrams.map((diagram) => [
    `process_${diagram.id}`,
    diagram,
  ]));
  return operations.map((operation) => {
    if (operation.evidence?.length) return operation;
    const diagram = operation.competencyQuestionId
      ? diagramsByQuestionId.get(operation.competencyQuestionId)
      : undefined;
    if (!diagram) {
      const confirmedTarget = operation.competencyQuestionId || operation.competencyQuestion;
      return confirmedTarget
        ? { ...operation, evidence: [`confirmed-process:${operation.competencyQuestionId || 'interview'}`] }
        : operation;
    }
    return {
      ...operation,
      evidence: unique([
        `diagram:${diagram.id}`,
        ...diagram.nodes
          .filter((node) => node.kind === 'action' || node.kind === 'decision')
          .map((node) => `flow-node:${node.id}`),
      ]),
    };
  });
}

export function attachGeneratedKnowledgeEvidence(
  spec: PackageSpec,
  analysis: AnalysisResult | undefined,
  diagrams: Array<{ id: string; nodes: Array<{ id: string; kind: string }> }> = [],
  materialCatalog?: MaterialEvidenceCatalog,
): PackageSpec {
  const candidates = analysis?.candidateArtifacts ?? [];
  const ruleCandidates = candidates.filter((candidate) => candidate.kind === 'rule');
  const metricCandidates = candidates.filter((candidate) => candidate.kind === 'metric');
  const objects = spec.objects.map((rawObject) => {
    const object = attachObjectDocumentRevisions(rawObject, materialCatalog);
    const evidence = objectEvidence(object);
    return {
      ...object,
      relations: object.relations?.map((relation) => (
        relation.evidence?.length ? relation : { ...relation, evidence }
      )),
    };
  });
  const operations = attachOperationEvidence(spec.operations, diagrams);
  const scripts = spec.scripts?.map((script) => (
    script.evidence?.length
      ? script
      : { ...script, evidence: [`operation:${script.boundOperation}`, 'contract:user-confirmed'] }
  ));
  return {
    ...spec,
    objects,
    operations,
    rules: attachCandidateEvidence(spec.rules ?? [], ruleCandidates, materialCatalog),
    metrics: attachCandidateEvidence(spec.metrics ?? [], metricCandidates, materialCatalog),
    ...(scripts ? { scripts } : {}),
  };
}

export function attachKnowledgeChangeEvidence(
  spec: PackageSpec,
  changes: Array<{
    targetKind: 'object' | 'operation' | 'rule' | 'metric' | 'script';
    targetKey: string;
    evidence: string[];
  }>,
): PackageSpec {
  const byTarget = new Map<string, string[]>();
  for (const change of changes) {
    const target = `${change.targetKind}:${change.targetKey}`;
    byTarget.set(target, unique([...(byTarget.get(target) ?? []), ...change.evidence]));
  }
  const objects = spec.objects.map((object) => {
    const evidence = byTarget.get(`object:${object.key}`) ?? [];
    if (!evidence.length) return object;
    return {
      ...object,
      relations: object.relations?.map((relation) => ({
        ...relation,
        evidence: unique([...(relation.evidence ?? []), ...evidence]),
      })),
    };
  });
  const mergeResource = <T extends { key: string; evidence?: string[] }>(
    kind: string,
    resources: T[] | undefined,
  ) => resources?.map((resource) => {
    const evidence = byTarget.get(`${kind}:${resource.key}`) ?? [];
    return evidence.length
      ? { ...resource, evidence: unique([...(resource.evidence ?? []), ...evidence]) }
      : resource;
  });
  return {
    ...spec,
    objects,
    operations: mergeResource('operation', spec.operations) ?? [],
    rules: mergeResource('rule', spec.rules),
    metrics: mergeResource('metric', spec.metrics),
    scripts: mergeResource('script', spec.scripts),
  };
}

export function findKnowledgeEvidenceProblems(spec: PackageSpec): string[] {
  const problems: string[] = [];
  for (const object of spec.objects) {
    // 外部输入或专家定义对象允许暂时没有资料来源；是否需要补证据作为检查发现呈现，
    // 不再阻断知识包草案生成。
    if (sourceRefsFor(object).length && !objectEvidence(object).length) {
      problems.push(`object:${object.key} 缺少资料证据`);
    }
    for (const relation of object.relations ?? []) {
      if (!relation.evidence?.some((item) => item.trim())) {
        problems.push(`relation:${object.key}.${relation.relation} 缺少关系证据`);
      }
    }
  }
  for (const operation of spec.operations) {
    if (!operation.evidence?.some((item) => item.trim())) problems.push(`operation:${operation.key} 缺少流程或访谈证据`);
  }
  for (const rule of spec.rules ?? []) {
    if (!rule.evidence?.some((item) => item.trim())) problems.push(`rule:${rule.key} 缺少规则证据`);
  }
  for (const metric of spec.metrics ?? []) {
    if (!metric.evidence?.some((item) => item.trim())) problems.push(`metric:${metric.key} 缺少口径证据`);
  }
  for (const script of spec.scripts ?? []) {
    if (!script.evidence?.some((item) => item.trim())) problems.push(`script:${script.key} 缺少契约或测试证据`);
  }
  return problems;
}
