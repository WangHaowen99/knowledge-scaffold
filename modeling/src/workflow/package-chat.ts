import { Type } from '@earendil-works/pi-ai';
import { buildLlm, buildTaskLlm } from '../llm/provider.js';
import { AgentRunTimeoutError, makeAgent, runAgent } from './agent.js';
import { assertObjectDataMappings } from './data-mapping.js';
import { specMetricSchema, specObjectSchema, specOperationSchema, specRuleSchema, specScriptSchema } from './tools.js';
import { packageIntentQuestions, validateSpecIntegrity } from './spec-validation.js';
import type {
  CoverageResult,
  DataDictTable,
  PackageSpec,
  Project,
  SpecMetric,
  SpecObject,
  SpecOperation,
  SpecRule,
  SpecScript,
} from '../types.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { getPiAuthoringAdapter } from '../authoring/pi-adapter.js';
import { attachKnowledgeChangeEvidence } from './knowledge-evidence.js';
import { renderPackage } from './package-render.js';
import { canonicalizePackageSpec } from './package-spec.js';
import { DATA_PROVIDER_SCRIPT_CONTRACT } from './script-safety.js';
import { MAX_CHANGE_PLAN_DESCRIPTION_LENGTH } from '../ai-construction/messages.js';
import { nextValidationCandidateVersion } from './package-version-lifecycle.js';

// 知识包工作区的建模 agent —— 咨询直接回答，修改走两段式确认：
// ① plan：只分析影响面（改哪些对象/操作、怎么改、风险），返回结构化计划等专家确认；咨询类直接文字回答；
// ② apply：确认后只生成计划内资源补丁 → 原子合并 → 落新版本。

export interface ChatTurn { role: 'user' | 'assistant'; text: string; }
export interface ChangePlanItem {
  targetKind: 'object' | 'operation' | 'rule' | 'metric' | 'script';
  targetKey: string;
  action: 'add' | 'modify' | 'remove';
  description: string;
}
export interface ChangePlan { summary: string; changes: ChangePlanItem[]; risks?: string[]; }
export interface AtomicResourcePatch {
  objects?: SpecObject[];
  operations?: SpecOperation[];
  rules?: SpecRule[];
  metrics?: SpecMetric[];
  scripts?: SpecScript[];
}
export interface ChatPlanResult {
  reply?: string;
  plan?: ChangePlan;
  interaction?: { id: string; runId: string; baseRevision?: string };
}
export interface PackageChatPlanOptions {
  purpose?: 'package_chat' | 'script_generation';
  intent?: string;
  interactionTitle?: string;
  metadata?: Record<string, unknown>;
}
export interface ChatApplyResult {
  reply: string;
  version?: string;
  revision?: string;
  candidateReview?: { id: string };
}
export interface PackageApplyCandidate {
  baseRevision: string;
  version: string;
  spec: PackageSpec;
  coverage: CoverageResult;
  files: Record<string, string>;
}
export type PackageApplyProgressPhase =
  | 'preparing'
  | 'generating'
  | 'validating'
  | 'retrying'
  | 'saving'
  | 'waiting_review'
  | 'completed';
export interface PackageApplyProgress {
  eventKey: string;
  phase: PackageApplyProgressPhase;
  summary: string;
  detail?: string;
  percent: number;
  attempt?: number;
  maxAttempts?: number;
  batch?: number;
  totalBatches?: number;
}
export interface PackageApplyOptions {
  maxCandidateAttempts?: number;
  validateCandidate?: (candidate: PackageApplyCandidate) => Promise<CandidateQualityDecision>;
  completedBatches?: Record<string, AtomicResourcePatch>;
  onBatchCompleted?: (batchKey: string, patch: AtomicResourcePatch) => void;
  onProgress?: (progress: PackageApplyProgress) => void | Promise<void>;
  createExpertReview?: (
    candidate: PackageApplyCandidate,
    decision: CandidateQualityDecision,
  ) => Promise<{ id: string }>;
}

export interface CandidateQualityDecision {
  accepted: boolean;
  feedback: string;
  disposition?: 'accepted' | 'expert_review' | 'rejected';
  reviewScore?: number;
}

export class PackageCandidateValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PackageCandidateValidationError';
  }
}

export class PackageCandidateRejectedError extends Error {
  readonly code = 'PACKAGE_CANDIDATE_REJECTED';
  readonly feedback: string;

  constructor(
    message: string,
    feedback: string,
  ) {
    super(message);
    this.name = 'PackageCandidateRejectedError';
    this.feedback = String(feedback).trim().slice(0, 3_000) || '候选内容未通过校验';
  }
}

function candidateValidationError(error: unknown): PackageCandidateValidationError {
  if (error instanceof PackageCandidateValidationError) return error;
  return new PackageCandidateValidationError(error instanceof Error ? error.message : String(error));
}

export type CandidateQualityLoopResult<T> =
  | { status: 'accepted'; candidate: T; decision: CandidateQualityDecision; attempts: number }
  | { status: 'expert_review'; candidate: T; decision: CandidateQualityDecision; attempts: number };

function plannedResourceId(change: ChangePlanItem): string {
  return `${change.targetKind}:${change.targetKey}`;
}

const changeActionLabel: Record<ChangePlanItem['action'], string> = {
  add: '新增',
  modify: '修改',
  remove: '删除',
};

/**
 * 一个资源最终只能提交一份完整定义。模型可以把同一资源的多个修改点拆成多条计划，
 * 这里在专家确认前合并同类动作，并在应用旧计划时再次兜底。相互冲突的动作不能静默合并。
 */
export function normalizeChangePlan(plan: ChangePlan): ChangePlan {
  const summary = String(plan.summary ?? '').trim();
  if (!summary || summary.length > 1_000) {
    throw new Error('方案摘要不能为空且不能超过 1000 字');
  }
  if (!Array.isArray(plan.changes) || plan.changes.length < 1 || plan.changes.length > 30) {
    throw new Error('建模调整方案必须包含 1 到 30 项变更');
  }
  const merged = new Map<string, ChangePlanItem>();
  const descriptions = new Map<string, Set<string>>();
  for (const change of plan.changes) {
    const id = plannedResourceId(change);
    const existing = merged.get(id);
    const description = change.description.trim();
    if (!description || description.length > MAX_CHANGE_PLAN_DESCRIPTION_LENGTH) {
      throw new Error(`资源 ${id} 的变更说明不能为空且不能超过 ${MAX_CHANGE_PLAN_DESCRIPTION_LENGTH} 字`);
    }
    if (!existing) {
      merged.set(id, { ...change, description });
      descriptions.set(id, new Set(description ? [description] : []));
      continue;
    }
    if (existing.action !== change.action) {
      throw new Error(
        `同一资源 ${id} 同时安排了“${changeActionLabel[existing.action]}”和“${changeActionLabel[change.action]}”，请重新规划为一个明确动作`,
      );
    }
    const parts = descriptions.get(id)!;
    if (description) parts.add(description);
    existing.description = [...parts].join('；');
  }
  return {
    ...plan,
    summary,
    changes: [...merged.values()],
    ...(plan.risks ? { risks: [...plan.risks] } : {}),
  };
}

function changesAreLinked(left: ChangePlanItem, right: ChangePlanItem): boolean {
  return left.description.includes(right.targetKey)
    || right.description.includes(left.targetKey);
}

function connectedChangeGroups(plan: ChangePlan, indexes: number[]): number[][] {
  const eligible = new Set(indexes);
  const links = new Map(indexes.map((index) => [index, new Set<number>()]));
  for (const left of indexes) {
    for (const right of indexes) {
      if (right <= left) continue;
      const leftChange = plan.changes[left];
      const rightChange = plan.changes[right];
      if (!changesAreLinked(leftChange, rightChange)) continue;
      links.get(left)!.add(right);
      links.get(right)!.add(left);
    }
  }
  const visited = new Set<number>();
  const groups: number[][] = [];
  for (const start of indexes) {
    if (visited.has(start)) continue;
    const stack = [start];
    const group: number[] = [];
    visited.add(start);
    while (stack.length) {
      const current = stack.pop()!;
      group.push(current);
      for (const neighbor of links.get(current) ?? []) {
        if (!eligible.has(neighbor) || visited.has(neighbor)) continue;
        visited.add(neighbor);
        stack.push(neighbor);
      }
    }
    groups.push(group.sort((a, b) => a - b));
  }
  return groups;
}

export function partitionAtomicChangePlan(plan: ChangePlan): ChangePlan[] {
  if (plan.changes.length < 2) return [plan];
  const allIndexes = plan.changes.map((_change, index) => index);
  const generatedScriptIndexes = allIndexes.filter((index) => {
    const change = plan.changes[index];
    return change.targetKind === 'script' && change.action !== 'remove';
  });
  const generatedOperationIndexes = allIndexes.filter((index) => {
    const change = plan.changes[index];
    return change.targetKind === 'operation' && change.action !== 'remove';
  });

  let groups: number[][];
  if (generatedScriptIndexes.length <= 1) {
    groups = connectedChangeGroups(plan, allIndexes);
  } else {
    const scripts = new Set(generatedScriptIndexes);
    const operations = new Set(generatedOperationIndexes);
    const remainingGroups = connectedChangeGroups(
      plan,
      allIndexes.filter((index) => !scripts.has(index)),
    );
    groups = [
      ...generatedScriptIndexes.map((index) => [index]),
      ...remainingGroups.filter((group) => !group.some((index) => operations.has(index))),
      ...remainingGroups.filter((group) => group.some((index) => operations.has(index))),
    ];
  }

  return groups.map((group, index) => ({
    summary: groups.length === 1 ? plan.summary : `${plan.summary}（第 ${index + 1}/${groups.length} 批）`,
    changes: group.map((item) => plan.changes[item]),
    risks: plan.risks,
  }));
}

export async function generateAtomicResourcePatchInBatches(
  plan: ChangePlan,
  generate: (batch: ChangePlan, index: number, total: number) => Promise<AtomicResourcePatch>,
  progress: {
    completed?: Record<string, AtomicResourcePatch>;
    onCompleted?: (batchKey: string, patch: AtomicResourcePatch) => void;
  } = {},
): Promise<AtomicResourcePatch> {
  const batches = partitionAtomicChangePlan(plan);
  const merged: AtomicResourcePatch = {};
  const submitted = new Set<string>();
  for (const [index, batch] of batches.entries()) {
    if (batch.changes.every((change) => change.action === 'remove')) continue;
    const batchKey = atomicChangeBatchKey(batch);
    const patch = progress.completed?.[batchKey] ?? await generate(batch, index, batches.length);
    try {
      assertAtomicBatchPatch(batch, patch);
    } catch (error) {
      throw candidateValidationError(error);
    }
    for (const kind of ['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[]) {
      const property = resourceProperty[kind];
      const resources = resourcesOf(patch, kind);
      if (!resources.length) continue;
      for (const resource of resources) {
        const id = `${kind}:${resource.key}`;
        if (submitted.has(id)) {
          throw new PackageCandidateValidationError(`模型跨批次重复提交资源 ${id}`);
        }
        submitted.add(id);
      }
      (merged[property] as Array<{ key: string }> | undefined) ??= [];
      (merged[property] as Array<{ key: string }>).push(...resources);
    }
    if (!progress.completed?.[batchKey]) progress.onCompleted?.(batchKey, patch);
  }
  const planned = new Set(plan.changes.filter((change) => change.action !== 'remove').map(plannedResourceId));
  const missing = [...planned].filter((id) => !submitted.has(id));
  if (missing.length) {
    throw new PackageCandidateValidationError(`模型未提交计划内资源：${missing.join('、')}`);
  }
  return merged;
}

export async function runCandidateQualityLoop<T>(input: {
  maxAttempts: number;
  generate: (attempt: number, feedback: string) => Promise<T>;
  validate: (candidate: T) => Promise<CandidateQualityDecision>;
  onProgress?: (event: {
    eventKey: string;
    phase: 'generating' | 'validating' | 'retrying';
    attempt: number;
    maxAttempts: number;
    feedback?: string;
  }) => void | Promise<void>;
}): Promise<CandidateQualityLoopResult<T>> {
  const maxAttempts = Math.max(1, Math.min(5, Math.trunc(input.maxAttempts) || 1));
  const report = async (event: {
    eventKey: string;
    phase: 'generating' | 'validating' | 'retrying';
    attempt: number;
    maxAttempts: number;
    feedback?: string;
  }) => {
    try { await input.onProgress?.(event); } catch { /* 进度通道异常不影响知识包修复 */ }
  };
  let feedback = '';
  let bestExpertReview: CandidateQualityLoopResult<T> | undefined;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    let candidate: T;
    try {
      await report({
        eventKey: `attempt-${attempt}-generating`,
        phase: 'generating', attempt, maxAttempts, ...(feedback ? { feedback } : {}),
      });
      candidate = await input.generate(attempt, feedback);
    } catch (error) {
      if (error instanceof AgentRunTimeoutError) throw error;
      const reason = (error instanceof Error ? error.message : String(error)).trim().slice(0, 3_000)
        || '候选补丁生成失败';
      if (attempt >= maxAttempts) {
        if (error instanceof PackageCandidateValidationError) {
          throw new PackageCandidateRejectedError(
            `候选修订连续 ${maxAttempts} 次生成失败。最后原因：${reason}`,
            reason,
          );
        }
        throw new Error(`候选修订连续 ${maxAttempts} 次生成失败。最后原因：${reason}`);
      }
      feedback = `上一候选生成失败：${reason}\n请严格只提交计划内资源，保留计划外资源不变。`;
      await report({ eventKey: `attempt-${attempt}-retrying`, phase: 'retrying', attempt, maxAttempts, feedback });
      continue;
    }
    await report({ eventKey: `attempt-${attempt}-validating`, phase: 'validating', attempt, maxAttempts });
    const decision = await input.validate(candidate);
    if (decision.accepted) {
      return { status: 'accepted', candidate, decision, attempts: attempt };
    }
    if (decision.disposition === 'expert_review') {
      const review = { status: 'expert_review' as const, candidate, decision, attempts: attempt };
      const score = Number.isFinite(decision.reviewScore) ? Number(decision.reviewScore) : Number.POSITIVE_INFINITY;
      const previousScore = Number.isFinite(bestExpertReview?.decision.reviewScore)
        ? Number(bestExpertReview?.decision.reviewScore)
        : Number.POSITIVE_INFINITY;
      if (!bestExpertReview || score < previousScore) bestExpertReview = review;
    }
    feedback = decision.feedback.trim() || '候选修订没有改善当前阻断问题，请重新生成更小、更保守的补丁。';
    if (attempt < maxAttempts) {
      await report({ eventKey: `attempt-${attempt}-retrying`, phase: 'retrying', attempt, maxAttempts, feedback });
    }
  }
  if (bestExpertReview) return { ...bestExpertReview, attempts: maxAttempts };
  throw new PackageCandidateRejectedError(
    `候选修订连续 ${maxAttempts} 次未通过质量门禁。最后反馈：${feedback}`,
    feedback,
  );
}

const specBody = (spec: PackageSpec) => JSON.stringify({
  objects: spec.objects, operations: spec.operations,
  rules: spec.rules ?? [], metrics: spec.metrics ?? [], scripts: spec.scripts ?? [],
});
const historyText = (history: ChatTurn[]) =>
  history.slice(-6).map((t) => `${t.role === 'user' ? '专家' : '助手'}：${t.text}`).join('\n');

type ResourceKind = ChangePlanItem['targetKind'];
const PACKAGE_APPLY_TIMEOUT_MS = 600_000;
const PACKAGE_APPLY_MIN_TIMEOUT_MS = 30_000;
const PACKAGE_APPLY_MAX_TIMEOUT_MS = 1_800_000;

export function resolvePackageApplyTimeoutMs(configured: string | undefined): number {
  if (configured === undefined || configured.trim() === '') return PACKAGE_APPLY_TIMEOUT_MS;
  const parsed = Number(configured);
  if (!Number.isFinite(parsed)) return PACKAGE_APPLY_TIMEOUT_MS;
  return Math.max(
    PACKAGE_APPLY_MIN_TIMEOUT_MS,
    Math.min(PACKAGE_APPLY_MAX_TIMEOUT_MS, Math.trunc(parsed)),
  );
}

export function nextPackageChatVersion(
  current: Pick<PackageSpec, 'version' | 'status'>,
): string {
  return nextValidationCandidateVersion({
    currentVersion: current.version,
    currentStatus: current.status ?? 'draft',
  });
}

const resourceProperty: Record<ResourceKind, keyof AtomicResourcePatch> = {
  object: 'objects',
  operation: 'operations',
  rule: 'rules',
  metric: 'metrics',
  script: 'scripts',
};

const resourceSchema: Record<ResourceKind, () => unknown> = {
  object: specObjectSchema,
  operation: specOperationSchema,
  rule: specRuleSchema,
  metric: specMetricSchema,
  script: specScriptSchema,
};

export function atomicChangeBatchKey(batch: ChangePlan): string {
  return batch.changes
    .filter((change) => change.action !== 'remove')
    .map(plannedResourceId)
    .sort()
    .join('|');
}

export function buildAtomicPatchParameters(batch: ChangePlan) {
  const properties: Record<string, unknown> = {};
  for (const kind of ['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[]) {
    const count = batch.changes.filter((change) => (
      change.targetKind === kind && change.action !== 'remove'
    )).length;
    if (!count) continue;
    properties[resourceProperty[kind]] = Type.Array(resourceSchema[kind]() as any, {
      minItems: count,
      maxItems: count,
    });
  }
  return Type.Object(properties as any, { additionalProperties: false });
}

function assertAtomicBatchPatch(batch: ChangePlan, patch: AtomicResourcePatch): void {
  const expected = new Set(batch.changes
    .filter((change) => change.action !== 'remove')
    .map(plannedResourceId));
  const actual: string[] = [];
  for (const kind of ['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[]) {
    actual.push(...resourcesOf(patch, kind).map((resource) => `${kind}:${resource.key}`));
  }
  const duplicate = actual.find((id, index) => actual.indexOf(id) !== index);
  if (duplicate) throw new Error(`模型重复提交了当前批次资源 ${duplicate}`);
  const unexpected = actual.find((id) => !expected.has(id));
  if (unexpected) throw new Error(`模型提交了当前批次计划外资源 ${unexpected}`);
  const missing = [...expected].filter((id) => !actual.includes(id));
  if (missing.length) throw new Error(`模型未提交计划内资源：${missing.join('、')}`);
}

function resourcesOf(spec: PackageSpec | AtomicResourcePatch, kind: ResourceKind): Array<{ key: string }> {
  return (spec[resourceProperty[kind]] ?? []) as Array<{ key: string }>;
}

function normalizedResourceIdentity(value: unknown): string {
  return String(value ?? '').normalize('NFKC').trim().replace(/\s+/g, ' ').toLocaleLowerCase('zh-CN');
}

/**
 * 模型有时会把界面标题填进 targetKey。修改/删除必须先归一到当前知识包的稳定 key，
 * 否则专家已经确认的方案会到执行阶段才报“目标不存在”，重试也只会重复旧错误。
 */
export function resolveChangePlanTargets(current: PackageSpec, input: ChangePlan): ChangePlan {
  const plan = normalizeChangePlan(input);
  const changes = plan.changes.map((change) => {
    if (change.action === 'add') return change;
    const resources = resourcesOf(current, change.targetKind) as Array<{ key: string; title?: string }>;
    const rawTarget = change.targetKey.trim();
    const withoutKindPrefix = rawTarget.replace(
      new RegExp(`^${change.targetKind}\\s*[:：]\\s*`, 'iu'),
      '',
    );
    const exact = resources.find((resource) => resource.key === rawTarget)
      ?? resources.find((resource) => resource.key === withoutKindPrefix);
    if (exact) return { ...change, targetKey: exact.key };

    const normalizedTarget = normalizedResourceIdentity(withoutKindPrefix);
    const titleMatches = resources.filter((resource) => (
      normalizedResourceIdentity(resource.title) === normalizedTarget
    ));
    if (titleMatches.length === 1) return { ...change, targetKey: titleMatches[0].key };
    if (titleMatches.length > 1) {
      throw new Error(
        `计划中的${change.action === 'modify' ? '修改' : '删除'}目标标题不唯一：${change.targetKind}:${rawTarget}`,
      );
    }
    throw new Error(
      `计划中的${change.action === 'modify' ? '修改' : '删除'}目标不存在：${change.targetKind}:${rawTarget}`,
    );
  });
  return normalizeChangePlan({ ...plan, changes });
}

export function applyAtomicResourcePatch(
  current: PackageSpec,
  plan: ChangePlan,
  patch: AtomicResourcePatch,
): Pick<PackageSpec, 'objects' | 'operations' | 'rules' | 'metrics' | 'scripts'> {
  const result: AtomicResourcePatch = {};
  for (const kind of ['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[]) {
    const property = resourceProperty[kind];
    const changes = plan.changes.filter((change) => change.targetKind === kind);
    if (!changes.length && (current as any)[property] === undefined) continue;
    const currentItems = resourcesOf(current, kind);
    const currentKeys = new Set(currentItems.map((item) => item.key));
    const rawSubmitted = resourcesOf(patch, kind);
    const singleModify = changes.length === 1 && changes[0].action === 'modify'
      ? changes[0]
      : undefined;
    const submitted = singleModify
      && rawSubmitted.length === 1
      && rawSubmitted[0].key !== singleModify.targetKey
      && !currentKeys.has(rawSubmitted[0].key)
      ? [{ ...rawSubmitted[0], key: singleModify.targetKey }]
      : rawSubmitted;
    const staleModify = changes.find((change) => (
      change.action === 'modify' && !currentKeys.has(change.targetKey)
    ));
    if (staleModify) {
      throw new Error(`计划中的修改目标不存在：${kind}:${staleModify.targetKey}`);
    }
    const staleAdd = changes.find((change) => (
      change.action === 'add' && currentKeys.has(change.targetKey)
    ));
    if (staleAdd) {
      throw new Error(`计划中的新增目标已经存在：${kind}:${staleAdd.targetKey}`);
    }
    const staleRemove = changes.find((change) => (
      change.action === 'remove' && !currentKeys.has(change.targetKey)
    ));
    if (staleRemove) {
      throw new Error(`计划中的删除目标不存在：${kind}:${staleRemove.targetKey}`);
    }
    const allowed = new Set(changes
      .filter((change) => change.action !== 'remove')
      .map((change) => change.targetKey));
    const seenSubmitted = new Set<string>();
    const duplicate = submitted.find((item) => {
      if (seenSubmitted.has(item.key)) return true;
      seenSubmitted.add(item.key);
      return false;
    });
    if (duplicate) {
      throw new Error(`模型重复提交了 ${kind}:${duplicate.key}，已拒绝应用`);
    }
    const unexpected = submitted.find((item) => !allowed.has(item.key));
    if (unexpected) {
      throw new Error(`模型提交了计划外的 ${kind}:${unexpected.key}，已拒绝应用`);
    }
    const replacements = new Map(submitted.map((item) => [item.key, item]));
    const missing = [...allowed].find((key) => !replacements.has(key));
    if (missing) {
      throw new Error(`模型未提交计划内的 ${kind}:${missing}，已拒绝应用`);
    }
    const unchanged = changes.find((change) => {
      if (change.action !== 'modify') return false;
      const before = currentItems.find((item) => item.key === change.targetKey);
      const after = replacements.get(change.targetKey);
      return JSON.stringify(before) === JSON.stringify(after);
    });
    if (unchanged) {
      throw new Error(`计划内的 ${kind}:${unchanged.targetKey} 未产生实际变化，已拒绝应用`);
    }
    const next = currentItems
      .filter((item) => !changes.some((change) => (
        change.action === 'remove' && change.targetKey === item.key
      )))
      .map((item) => replacements.get(item.key) ?? item);
    for (const change of changes.filter((item) => item.action === 'add')) {
      const addition = replacements.get(change.targetKey);
      if (addition) next.push(addition);
    }
    result[property] = next as any;
  }
  return result as Pick<PackageSpec, 'objects' | 'operations' | 'rules' | 'metrics' | 'scripts'>;
}

export function buildAtomicChangeContext(current: PackageSpec, plan: ChangePlan) {
  const targets = plan.changes.map((change) => ({
    ...change,
    current: resourcesOf(current, change.targetKind)
      .find((item) => item.key === change.targetKey) ?? null,
  }));
  return {
    targets,
    referenceIndex: {
      objects: current.objects.map((object) => ({
        key: object.key,
        title: object.title,
        fields: object.fields.map((field) => field.key),
        relations: (object.relations ?? []).map((relation) => ({
          relation: relation.relation,
          target: relation.target,
        })),
      })),
      operations: current.operations.map((operation) => ({
        key: operation.key,
        title: operation.title,
        relatedObjects: operation.relatedObjects,
        usesRules: operation.usesRules ?? [],
        usesMetrics: operation.usesMetrics ?? [],
        script: operation.script?.key,
      })),
      rules: (current.rules ?? []).map((rule) => ({
        key: rule.key,
        title: rule.title,
        appliesToObjects: rule.appliesToObjects ?? [],
      })),
      metrics: (current.metrics ?? []).map((metric) => ({
        key: metric.key,
        title: metric.title,
        usesObjects: metric.usesObjects ?? [],
      })),
      scripts: (current.scripts ?? []).map((script) => ({
        key: script.key,
        title: script.title,
        boundOperation: script.boundOperation,
      })),
    },
  };
}

// ---- 阶段一：影响分析（不改包） ----
export async function packageChatPlan(
  project: Project,
  history: ChatTurn[],
  message: string,
  options: PackageChatPlanOptions = {},
): Promise<ChatPlanResult> {
  const latest = await getPackageArtifactStore().current(project.id);
  if (!latest) throw new Error('尚未生成知识包');

  const cap: { plan?: ChangePlan } = {};
  const submitPlan = {
    name: 'submit_change_plan',
    label: '提交建模调整计划',
    description: '当专家要求调整知识包时调用：提交影响分析计划（改哪些对象/操作/规则/指标/脚本、怎么改、有什么风险），等专家确认后另行应用。仅咨询/解释类问题不要调用。',
    parameters: Type.Object({
      summary: Type.String(),
      changes: Type.Array(Type.Object({
        targetKind: Type.Union([
          Type.Literal('object'), Type.Literal('operation'), Type.Literal('rule'), Type.Literal('metric'),
          Type.Literal('script'),
        ]),
        targetKey: Type.String(),
        action: Type.Union([Type.Literal('add'), Type.Literal('modify'), Type.Literal('remove')]),
        description: Type.String({ maxLength: MAX_CHANGE_PLAN_DESCRIPTION_LENGTH }),
      })),
      risks: Type.Optional(Type.Array(Type.String())),
    }),
    async execute(_id: string, params: any) {
      cap.plan = resolveChangePlanTargets(latest.spec, params as ChangePlan);
      return { content: [{ type: 'text' as const, text: '计划已提交，等待专家确认。' }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包建模助手（package_chat_plan）。你需要理解当前知识包，回答建模问题，并协助设计或调整对象、操作、规则、指标与脚本。',
    '严格限定对话范围：只回答当前建模任务、知识包内容、证据、校验结果与受控修改相关问题。',
    '对闲聊或无关请求不展开作答，简短说明后引导专家回到当前任务，并给出不超过 3 个可选的建模下一步。',
    '规则：① 咨询/解释类问题直接文字回答，不调用工具；',
    '② 要求修改时【只做影响分析，不直接改】：用 submit_change_plan 提交计划——每个资源只提交一条 change；',
    '同一资源有多个修改点时合并写入该 change 的 description，不得把同一资源拆成多条 change。',
    'change 需包含目标对象/操作/规则/指标/脚本 key、add/modify/remove 和具体修改内容。',
    '材料中的适用条件/判断门槛/例外应修缮 rule，公式/统计粒度/边界应修缮 metric，不要全部写进 operation。',
    '多步骤精确计算应修缮 script，并同时计划修改绑定它的 operation；脚本不得内置某条用例的答案。',
    '删除 operation 时必须同时规划删除或重新绑定其 script；删除 script 时必须同时规划移除 operation.script 引用。',
    '列出级联风险：改口径会影响哪些操作、是否与典型问题路由冲突、remove 是否破坏领域问题覆盖。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  const body = [
    '【当前规格】', specBody(latest.spec),
    historyText(history) ? `【此前对话】\n${historyText(history)}` : '',
    `【专家消息】\n${message}`,
  ].filter(Boolean).join('\n');

  const agent = makeAgent(buildLlm(), sys, [submitPlan] as any);
  const messages = await runAgent(agent, body);

  if (cap.plan?.changes?.length) {
    const adapter = getPiAuthoringAdapter();
    const purpose = options.purpose ?? 'package_chat';
    const run = adapter.start(project.id, options.intent ?? '修改知识包', latest.revision.oid, {
      message,
      step: 'review_plan',
      purpose,
    }).run;
    const interaction = adapter.request(project.id, run.id, 'change_plan', {
      purpose,
      title: options.interactionTitle ?? '确认建模调整',
      summary: cap.plan.summary,
      changes: cap.plan.changes.map((change, index) => ({ id: String(index), ...change })),
      risks: cap.plan.risks ?? [],
      message,
      history: history.map((turn) => ({ role: turn.role, text: turn.text })),
      ...(options.metadata ?? {}),
    }, latest.revision.oid).interaction;
    return {
      plan: cap.plan,
      interaction: { id: interaction.id, runId: run.id, baseRevision: interaction.baseRevision },
    };
  }

  const lastText = [...messages].reverse().find((m: any) => m?.role === 'assistant');
  const text = lastText
    ? (typeof (lastText as any).content === 'string'
      ? (lastText as any).content
      : ((lastText as any).content ?? []).map((c: any) => c.text ?? '').join(''))
    : '';
  return { reply: text || '（agent 未给出答复）' };
}

// ---- 阶段二：确认后应用 ----
export async function packageChatApply(
  project: Project,
  history: ChatTurn[],
  message: string,
  plan: ChangePlan,
  tables: DataDictTable[],
  options: PackageApplyOptions = {},
): Promise<ChatApplyResult> {
  const report = async (progress: PackageApplyProgress) => {
    try { await options.onProgress?.(progress); } catch { /* 展示进度失败不阻断实际修复 */ }
  };
  await report({
    eventKey: 'preparing',
    phase: 'preparing', summary: '正在读取已确认方案和当前知识包',
    detail: '准备资料来源、资源依赖和质量校验上下文。', percent: 5,
  });
  const artifactStore = getPackageArtifactStore();
  const latest = await artifactStore.current(project.id);
  if (!latest) throw new Error('尚未生成知识包');
  try {
    plan = resolveChangePlanTargets(latest.spec, plan);
    if (!plan.changes.length) throw new Error('修缮计划没有可执行的资源变更');
  } catch (error) {
    const feedback = error instanceof Error ? error.message : String(error);
    throw new PackageCandidateRejectedError('已确认的修改方案无法应用', feedback);
  }

  const sys = [
    '你是知识包建模助手（package_chat_apply）。专家已确认建模调整计划，请把计划逐条落实到规格上，',
    '用 submit_resource_patch 只提交计划内 add/modify 资源的完整定义；remove 不要返回资源。',
    '不得提交、重写或补全计划外资源。引用索引只用于理解依赖，不能作为额外修改授权。',
    'modify 必须保留目标资源的 key，专家文本中的“重命名”只能解释为调整 title/description/caliber 等展示与口径；不得改 key 或新建替代资源。',
    'modify 必须保留目标资源中未被计划要求改变的字段、证据和来源引用，并产生真实内容变化。',
    '新增 script 时必须提交完整的 python 脚本规格：boundOperation、entrypoint、参数、输出、边界和 sourceCode；',
    'sourceCode 必须使用 argparse 接收结构化输入并输出 JSON，不得访问网络、任意文件系统、子进程或动态执行代码。',
    DATA_PROVIDER_SCRIPT_CONTRACT,
    '脚本与 operation 必须双向绑定：script.boundOperation 指向 operation，operation.script.key 指回 script，并在 flow 中明确调用。',
    '每个对象都必须保留资料来源：数据对象的 sourceRefs 引用真实 Sheet，字段通过 sourceColumn 对应真实列；产品、场景等语义对象引用原始文档及证据位置。',
    '对象 primaryKey 只能填写 fields[].key，不得填写 sourceColumn、alias 或原始列名。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  const needsGeneratedPatch = plan.changes.some((change) => change.action !== 'remove');
  const spec = latest.spec;
  const version = nextPackageChatVersion(latest.spec);
  const timeoutMs = resolvePackageApplyTimeoutMs(process.env.MODELING_PACKAGE_APPLY_TIMEOUT_MS);
  const maxCandidateAttempts = options.validateCandidate ? (options.maxCandidateAttempts ?? 3) : 1;
  const quality = await runCandidateQualityLoop<PackageApplyCandidate>({
    maxAttempts: maxCandidateAttempts,
    onProgress: async (event) => {
      if (event.phase === 'generating') {
        await report({
          eventKey: event.eventKey,
          phase: 'generating',
          summary: `正在生成第 ${event.attempt}/${event.maxAttempts} 次修复候选`,
          detail: event.feedback
            ? '上一候选未通过，将依据失败原因重新生成更小、更保守的补丁。'
            : '模型正在逐批落实专家已确认的资源变更。',
          percent: event.attempt === 1 ? 12 : Math.min(48, 18 + event.attempt * 10),
          attempt: event.attempt, maxAttempts: event.maxAttempts,
        });
      } else if (event.phase === 'validating') {
        await report({
          eventKey: event.eventKey,
          phase: 'validating',
          summary: `正在校验第 ${event.attempt}/${event.maxAttempts} 次修复候选`,
          detail: '正在比较修复前后的结构、映射、脚本与语义问题。',
          percent: Math.min(78, 62 + event.attempt * 4),
          attempt: event.attempt, maxAttempts: event.maxAttempts,
        });
      } else {
        await report({
          eventKey: event.eventKey,
          phase: 'retrying',
          summary: `第 ${event.attempt}/${event.maxAttempts} 次候选未通过，准备重试`,
          detail: event.feedback?.slice(0, 1_000) || '将根据质量校验反馈重新生成候选。',
          percent: Math.min(55, 22 + event.attempt * 10),
          attempt: event.attempt, maxAttempts: event.maxAttempts,
        });
      }
    },
    generate: async (attempt, qualityFeedback) => {
      const patch = needsGeneratedPatch
        ? await generateAtomicResourcePatchInBatches(plan, async (batch, index, total) => {
          await report({
            eventKey: `attempt-${attempt}-batch-${index + 1}-started`,
            phase: 'generating',
            summary: `正在生成第 ${index + 1}/${total} 批资源补丁`,
            detail: batch.changes.map((change) => (
              `${change.action === 'add' ? '新增' : change.action === 'modify' ? '修改' : '删除'} ${change.targetKind}:${change.targetKey}`
            )).join('；'),
            percent: Math.min(58, 18 + Math.round(((index + 1) / Math.max(1, total)) * 36)),
            attempt, maxAttempts: maxCandidateAttempts, batch: index + 1, totalBatches: total,
          });
          const cap: { patch?: AtomicResourcePatch } = {};
          const submitPatch = {
            name: 'submit_resource_patch',
            label: '提交计划内资源补丁',
            description: '只提交当前批次已确认计划中 add/modify 的完整资源；不得提交未列入当前批次的资源，remove 资源无需返回。',
            parameters: buildAtomicPatchParameters(batch),
            async execute(_id: string, params: any) {
              cap.patch = params as AtomicResourcePatch;
              return { content: [{ type: 'text' as const, text: '当前批次资源补丁已提交。' }], details: {}, terminate: true };
            },
          };
          const batchRequirements = batch.changes.map((change) => (
            `- ${plannedResourceId(change)} · ${change.action}：${change.description}`
          )).join('\n');
          const body = [
            total > 1 ? `【批次进度】${index + 1}/${total}` : '',
            '【原子变更上下文】', JSON.stringify(buildAtomicChangeContext(latest.spec, batch)),
            total > 1 ? `【整体诉求摘要】\n${plan.summary}` : `【专家原始诉求】\n${message}`,
            `【当前批次已确认要求】\n${batchRequirements}`,
            total === 1 && historyText(history) ? `【最近对话】\n${historyText(history)}` : '',
            qualityFeedback ? `【上一候选未通过质量门禁】\n${qualityFeedback}` : '',
            qualityFeedback
              ? '请根据校验反馈生成更小、更保守的补丁；只修复确认目标，不得改写无关语义。'
              : '',
            '请只提交当前批次计划内资源补丁。',
          ].filter(Boolean).join('\n');
          const agent = makeAgent(buildTaskLlm(), sys, [submitPatch] as any);
          await runAgent(agent, body, undefined, { timeoutMs });
          if (!cap.patch) {
            throw new PackageCandidateValidationError(
              `agent 未产出第 ${index + 1}/${total} 批计划内资源补丁，请重试`,
            );
          }
          try {
            applyAtomicResourcePatch(spec, batch, cap.patch);
          } catch (error) {
            throw candidateValidationError(error);
          }
          await report({
            eventKey: `attempt-${attempt}-batch-${index + 1}-completed`,
            phase: 'generating',
            summary: `第 ${index + 1}/${total} 批资源补丁已提交`,
            detail: `已完成 ${batch.changes.map((change) => `${change.targetKind}:${change.targetKey}`).join('、')}，继续处理后续资源。`,
            percent: Math.min(60, 22 + Math.round(((index + 1) / Math.max(1, total)) * 38)),
            attempt, maxAttempts: maxCandidateAttempts, batch: index + 1, totalBatches: total,
          });
          return cap.patch;
        }, {
          completed: options.completedBatches,
          onCompleted: options.onBatchCompleted,
        })
        : {};
      let revisedResources: ReturnType<typeof applyAtomicResourcePatch>;
      try {
        revisedResources = applyAtomicResourcePatch(spec, plan, patch);
      } catch (error) {
        throw candidateValidationError(error);
      }
      const revisedWithDecisionEvidence = attachKnowledgeChangeEvidence(
        { ...spec, version, status: 'review', ...revisedResources },
        plan.changes.map((change) => ({
          targetKind: change.targetKind,
          targetKey: change.targetKey,
          evidence: [`user-decision:${plan.summary}`],
        })),
      );
      const newSpec: PackageSpec = canonicalizePackageSpec(revisedWithDecisionEvidence);
      await report({
        eventKey: `attempt-${attempt}-structure-validating`,
        phase: 'validating', summary: '正在执行候选结构与映射校验',
        detail: '检查计划边界、资源引用、数据映射和领域问题覆盖。', percent: 60,
        attempt, maxAttempts: maxCandidateAttempts,
      });
      try {
        assertObjectDataMappings(newSpec, tables);
      } catch (error) {
        throw candidateValidationError(error);
      }
      const coverage = validateSpecIntegrity(newSpec, packageIntentQuestions(newSpec));
      return {
        baseRevision: latest.revision.oid,
        version,
        spec: newSpec,
        coverage,
        files: renderPackage(newSpec),
      };
    },
    validate: options.validateCandidate ?? (async () => ({ accepted: true, feedback: '' })),
  });
  if (quality.status === 'expert_review') {
    await report({
      eventKey: 'waiting-expert-review',
      phase: 'waiting_review', summary: '这份修改还需要你确认后才能使用',
      detail: '系统已经准备好修改内容和前后差异。当前知识包不会自动改变，请查看候选卡片后选择使用、继续调整或不使用。', percent: 88,
    });
    if (!options.createExpertReview) {
      throw new Error(`候选修订需要专家复核，但当前入口未配置复核流程。${quality.decision.feedback}`);
    }
    const review = await options.createExpertReview(quality.candidate, quality.decision);
    return {
      reply: `自动修复候选仍有语义争议，已保留为待专家复核候选。请查看失败原因和差异后决定采用、退回修改或放弃。`,
      candidateReview: review,
    };
  }
  const candidate = quality.candidate;
  const newSpec = candidate.spec;
  const coverage = candidate.coverage;
  await report({
    eventKey: 'saving-version',
    phase: 'saving', summary: '候选已通过，正在保存新的知识包版本',
    detail: '正在写入修订、渲染文件并刷新项目状态。', percent: 92,
  });
  const saved = await artifactStore.savePackageVersion(project.id, {
    expectedRevision: latest.revision.oid,
    spec: newSpec,
    coverage,
    note: `内容修改：${plan.summary.slice(0, 60)}`,
    action: {
      cause: 'agent_update', summary: plan.summary.slice(0, 500), actorKind: 'agent', actorName: '智能助手',
    },
  });
  const { getAuthoringState } = await import('../authoring/state.js');
  getAuthoringState().apply({
    kind: 'publish_event', projectId: project.id, type: 'package.revision.created',
    payload: { revisionId: saved.revision.oid, cause: 'agent_update', packageVersion: version },
  });
  await report({
    eventKey: 'completed',
    phase: 'completed', summary: `知识包 v${version} 已生成`,
    detail: '修复结果已保存，正在收敛后续校验和建模任务状态。', percent: 100,
  });
  return {
    reply: `已按计划修订并生成新版本 v${version}（对象 ${newSpec.objects.length} / 操作 ${newSpec.operations.length} / 规则 ${newSpec.rules?.length ?? 0} / 指标 ${newSpec.metrics?.length ?? 0} / 脚本 ${newSpec.scripts?.length ?? 0}）。左侧文件树已是最新内容。`,
    version,
    revision: saved.revision.oid,
  };
}
