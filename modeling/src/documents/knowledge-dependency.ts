/**
 * 知识资源之间的引用关系，以及「改动一处会波及哪些」的传播。
 *
 * 现在资料一变就按里程碑序号一刀切：受影响点之后的阶段全部推倒重来
 * （material-impact.ts 的 `WHERE milestone.ordinal >= ?`）。改一份流程资料，
 * 知识结构、确定性能力、完成确认全部作废——用户只能从头走一遍。
 *
 * 但引用关系在 spec 里是显式的：operation.usesRules / usesMetrics / relatedObjects、
 * operation.script.key、script.boundOperation。有这些就能算出「到底哪些内容需要复核」，
 * 而不是把整段推倒。这是增量建模的地基，也让「改动资料」本身不再是灾难。
 */
import type { PackageSpec } from '../types.js';
import type { AffectedKnowledgeKind, AffectedKnowledgeResource } from './material-impact.js';

export type KnowledgeRef = AffectedKnowledgeResource;

const refId = (ref: KnowledgeRef) => `${ref.kind}:${ref.key}`;

/**
 * 谁直接引用了这个资源。
 *
 * operation 与 script 是相互引用的（operation.script.key ↔ script.boundOperation），
 * 所以传播必须带访问集合，否则会绕死循环。
 */
export function directDependents(spec: PackageSpec, ref: KnowledgeRef): KnowledgeRef[] {
  const dependents: KnowledgeRef[] = [];
  for (const operation of spec.operations ?? []) {
    const uses = ref.kind === 'rule' ? operation.usesRules
      : ref.kind === 'metric' ? operation.usesMetrics
        : ref.kind === 'object' ? operation.relatedObjects
          : undefined;
    if (uses?.includes(ref.key)) dependents.push({ kind: 'operation', key: operation.key });
    // 对象关系记作 `对象.关系`，改动关系等同于改动它所属的对象
    if (ref.kind === 'relation' && operation.relatedObjects?.includes(ref.key.split('.')[0])) {
      dependents.push({ kind: 'operation', key: operation.key });
    }
    if (ref.kind === 'script' && operation.script?.key === ref.key) {
      dependents.push({ kind: 'operation', key: operation.key });
    }
  }
  for (const script of spec.scripts ?? []) {
    if (ref.kind === 'operation' && script.boundOperation === ref.key) {
      dependents.push({ kind: 'script', key: script.key });
    }
  }
  return dependents;
}

/**
 * 从改动点出发算出全部受波及的资源（含改动点本身）。
 *
 * 用广度优先而不是递归：引用关系里有环（operation ↔ script），递归会栈溢出。
 */
export function propagateImpact(
  spec: PackageSpec,
  seeds: KnowledgeRef[],
): KnowledgeRef[] {
  const seen = new Map<string, KnowledgeRef>();
  const queue: KnowledgeRef[] = [];
  for (const seed of seeds) {
    if (seen.has(refId(seed))) continue;
    seen.set(refId(seed), seed);
    queue.push(seed);
  }
  while (queue.length) {
    const current = queue.shift()!;
    for (const dependent of directDependents(spec, current)) {
      if (seen.has(refId(dependent))) continue;
      seen.set(refId(dependent), dependent);
      queue.push(dependent);
    }
  }
  return [...seen.values()];
}

/** 只要波及的部分，不含改动点本身——用来告诉用户「除了你改的，还有这些要复核」 */
export function additionalImpact(spec: PackageSpec, seeds: KnowledgeRef[]): KnowledgeRef[] {
  const seedIds = new Set(seeds.map(refId));
  return propagateImpact(spec, seeds).filter((ref) => !seedIds.has(refId(ref)));
}

const KIND_LABEL: Record<AffectedKnowledgeKind, string> = {
  object: '业务对象', relation: '对象关系', operation: '业务操作',
  rule: '规则', metric: '指标', script: '脚本',
};

/** 给用户看的影响面说明。按类型归并，避免列出几十条散装 key */
export function describeImpact(refs: KnowledgeRef[]): string {
  if (!refs.length) return '没有其他内容受影响';
  const byKind = new Map<AffectedKnowledgeKind, string[]>();
  for (const ref of refs) {
    const bucket = byKind.get(ref.kind) ?? [];
    bucket.push(ref.key);
    byKind.set(ref.kind, bucket);
  }
  return [...byKind.entries()]
    .map(([kind, keys]) => {
      const shown = keys.slice(0, 3).join('、');
      const rest = keys.length > 3 ? ` 等 ${keys.length} 个` : '';
      return `${KIND_LABEL[kind]}：${shown}${rest}`;
    })
    .join('；');
}
