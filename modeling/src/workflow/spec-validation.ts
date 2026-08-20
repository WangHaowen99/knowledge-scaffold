import type {
  AnalysisResult, CoverageResult, PackageSpec, SpecMetric, SpecObject, SpecOperation, SpecRule,
} from '../types.js';
import { normalizeKey, validateSpecKeys } from './keys.js';
import {
  validateGeneratedScriptExecutionContract,
  validateGeneratedScriptSecurity,
  validateGeneratedScriptSyntax,
} from './script-safety.js';
import { findKnowledgeEvidenceProblems } from './knowledge-evidence.js';
import {
  describeObjectAliasConflict,
  findObjectAliasConflicts,
} from './object-alias-validation.js';

export const normalizeCompetencyQuestion = (value: string) => value.trim().replace(/\s+/g, '').replace(/[？?。！!]$/g, '');

export const questionsMatch = (operationQuestion: string | undefined, question: string) =>
  !!operationQuestion && normalizeCompetencyQuestion(operationQuestion) === normalizeCompetencyQuestion(question);

export interface CompetencyQuestionRef {
  id: string;
  question: string;
}

export interface BoundOperationBatch {
  operations: SpecOperation[];
  issues: string[];
}

/**
 * 新生成内容按稳定 ID 绑定领域问题；旧模型输出的原文认领仍可兼容。
 * 最终问题文本始终由服务端回填，模型没有修改原文的权限。
 */
export function bindOperationCompetencyQuestions(
  questions: CompetencyQuestionRef[],
  operations: SpecOperation[],
): BoundOperationBatch {
  const byId = new Map(questions.map((question) => [question.id, question]));
  const byText = new Map<string, CompetencyQuestionRef[]>();
  for (const question of questions) {
    const key = normalizeCompetencyQuestion(question.question);
    byText.set(key, [...(byText.get(key) ?? []), question]);
  }

  const issues: string[] = [];
  const bound = operations.map((operation) => {
    let question = operation.competencyQuestionId
      ? byId.get(operation.competencyQuestionId)
      : undefined;
    if (operation.competencyQuestionId && !question) {
      issues.push(`操作 ${operation.key} 认领了当前批次不存在的领域问题 ID ${operation.competencyQuestionId}`);
      return operation;
    }
    if (!question && operation.competencyQuestion) {
      const matches = byText.get(normalizeCompetencyQuestion(operation.competencyQuestion)) ?? [];
      if (matches.length === 1) question = matches[0];
      else if (matches.length > 1) issues.push(`操作 ${operation.key} 按文字认领的领域问题不唯一，必须提交问题 ID`);
    }
    if (!question) {
      issues.push(`操作 ${operation.key} 未认领当前批次的领域问题 ID`);
      return operation;
    }
    return {
      ...operation,
      competencyQuestionId: question.id,
      competencyQuestion: question.question,
    };
  });
  return { operations: bound, issues };
}

function findDuplicates(keys: string[]): string[] {
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const key of keys) {
    if (seen.has(key)) duplicates.add(key);
    seen.add(key);
  }
  return [...duplicates];
}

type ObjectCandidate = AnalysisResult['candidateObjects'][number];
export type ObjectCandidateRef = ObjectCandidate & { id: string };

/** 进入对象生成计划的逻辑候选必须逐项认领，不能再被模型静默省略。 */
export function validateObjectBatch(
  candidates: ObjectCandidateRef[],
  objects: SpecObject[],
  existingObjects: SpecObject[] = [],
): string[] {
  const issues: string[] = [];
  const candidateIds = new Set(candidates.map((candidate) => candidate.id));
  for (const object of objects) {
    if (object.candidateId && !candidateIds.has(object.candidateId)) {
      issues.push(`对象「${object.title || object.key}」认领了当前批次不存在的候选 ID ${object.candidateId}`);
    }
  }
  const claimed = new Set(objects.map((object) => object.candidateId).filter(Boolean));
  for (const candidate of candidates) {
    if (!claimed.has(candidate.id)) issues.push(`对象候选「${candidate.name}」未生成，也没有进入专家处理`);
  }
  const submittedKeys = new Set(objects.map((object) => object.key));
  for (const conflict of findObjectAliasConflicts([...existingObjects, ...objects])) {
    const involvesSubmittedObject = conflict.kind === 'shared_alias'
      ? conflict.objectKeys.some((key) => submittedKeys.has(key))
      : conflict.aliasObjectKeys.some((key) => submittedKeys.has(key))
        || submittedKeys.has(conflict.identityObjectKey);
    if (involvesSubmittedObject) issues.push(describeObjectAliasConflict(conflict));
  }
  return issues;
}

export interface KnowledgeBatchValidation {
  issues: string[];
  invalidReferenceCount: number;
}

/**
 * 规则和指标在并入整包前即时检查 key 与对象引用。
 * 这一步只校验知识生成产物，不读取或重新计算上游资料分析结果。
 */
export function validateKnowledgeBatch(
  batch: { rules: SpecRule[]; metrics: SpecMetric[] },
  available: { objects: SpecObject[]; rules: SpecRule[]; metrics: SpecMetric[] },
): KnowledgeBatchValidation {
  const issues: string[] = [];
  const objectKeys = new Set(available.objects.map((item) => normalizeKey(item.key)));
  const existingRuleKeys = new Set(available.rules.map((item) => normalizeKey(item.key)));
  const existingMetricKeys = new Set(available.metrics.map((item) => normalizeKey(item.key)));
  const ruleKeys = batch.rules.map((item) => normalizeKey(item.key));
  const metricKeys = batch.metrics.map((item) => normalizeKey(item.key));
  let invalidReferenceCount = 0;

  for (const key of findDuplicates(ruleKeys)) issues.push(`规则 key 重复：${key}`);
  for (const key of findDuplicates(metricKeys)) issues.push(`指标 key 重复：${key}`);
  for (const rule of batch.rules) {
    if (existingRuleKeys.has(normalizeKey(rule.key))) issues.push(`规则 key 重复：${rule.key}`);
    for (const objectKey of rule.appliesToObjects ?? []) {
      if (objectKeys.has(normalizeKey(objectKey))) continue;
      invalidReferenceCount += 1;
      issues.push(`规则 ${rule.key} 引用了不存在的对象 ${objectKey}`);
    }
  }
  for (const metric of batch.metrics) {
    if (existingMetricKeys.has(normalizeKey(metric.key))) issues.push(`指标 key 重复：${metric.key}`);
    for (const objectKey of metric.usesObjects ?? []) {
      if (objectKeys.has(normalizeKey(objectKey))) continue;
      invalidReferenceCount += 1;
      issues.push(`指标 ${metric.key} 引用了不存在的对象 ${objectKey}`);
    }
  }
  return { issues, invalidReferenceCount };
}

export interface OperationBatchValidation {
  issues: string[];
  uncoveredQuestions: string[];
  invalidReferenceCount: number;
}

/** 在操作批次入库前检查问题覆盖和所有资源引用，避免等到整包校验才丢弃整次生成。 */
export function validateOperationBatch(
  questions: CompetencyQuestionRef[],
  operations: SpecOperation[],
  available: { objects: SpecObject[]; rules: SpecRule[]; metrics: SpecMetric[] },
): OperationBatchValidation {
  const issues: string[] = [];
  const uncoveredQuestions = questions.filter((question) =>
    !operations.some((operation) => operation.competencyQuestionId === question.id))
    .map((question) => question.question);
  const expectedQuestionIds = new Set(questions.map((question) => question.id));
  for (const operation of operations) {
    if (!operation.competencyQuestionId || !expectedQuestionIds.has(operation.competencyQuestionId)) {
      issues.push(`操作 ${operation.key} 未按 ID 认领当前批次的领域问题`);
    }
  }

  const objectKeys = new Set(available.objects.map((item) => normalizeKey(item.key)));
  const ruleKeys = new Set(available.rules.map((item) => normalizeKey(item.key)));
  const metricKeys = new Set(available.metrics.map((item) => normalizeKey(item.key)));
  let invalidReferenceCount = 0;
  for (const operation of operations) {
    if (operation.script?.key) {
      invalidReferenceCount += 1;
      issues.push(`操作 ${operation.key} 在业务操作提取阶段引用了尚未生成的脚本 ${operation.script.key}；请移除 script，脚本将在后续确定性阶段单独生成`);
    }
    if ((operation.flow ?? []).some((step) => /\bscript\s*:/i.test(step))) {
      invalidReferenceCount += 1;
      issues.push(`操作 ${operation.key} 在业务操作提取阶段的 flow 中引用了尚未生成的脚本；请移除 script:<key>，脚本将在后续确定性阶段单独生成`);
    }
    for (const objectKey of operation.relatedObjects ?? []) {
      if (objectKeys.has(normalizeKey(objectKey))) continue;
      invalidReferenceCount += 1;
      issues.push(`操作 ${operation.key} 引用了不存在的对象 ${objectKey}`);
    }
    for (const ruleKey of operation.usesRules ?? []) {
      if (ruleKeys.has(normalizeKey(ruleKey))) continue;
      invalidReferenceCount += 1;
      issues.push(`操作 ${operation.key} 引用了不存在的规则 ${ruleKey}`);
    }
    for (const metricKey of operation.usesMetrics ?? []) {
      if (metricKeys.has(normalizeKey(metricKey))) continue;
      invalidReferenceCount += 1;
      issues.push(`操作 ${operation.key} 引用了不存在的指标 ${metricKey}`);
    }
  }
  return { issues, uncoveredQuestions, invalidReferenceCount };
}

export function coverageFor(spec: PackageSpec, selectedQuestions: string[]): CoverageResult {
  const covered: CoverageResult['covered'] = [];
  const uncovered: string[] = [];
  for (const question of selectedQuestions) {
    const operation = spec.operations.find((item) => questionsMatch(item.competencyQuestion, question));
    if (operation) covered.push({ question, operation: operation.key });
    else uncovered.push(question);
  }
  return { covered, uncovered };
}

export function packageIntentQuestions(spec: PackageSpec): string[] {
  const seen = new Set<string>();
  const questions: string[] = [];
  for (const operation of spec.operations ?? []) {
    const question = operation.competencyQuestion?.normalize('NFKC').trim();
    if (!question) continue;
    const key = question.toLocaleLowerCase('zh-CN');
    if (seen.has(key)) continue;
    seen.add(key);
    questions.push(question);
  }
  return questions;
}

export interface DuplicateScriptEntrypoint {
  entrypoint: string;
  scriptKeys: string[];
}

export function duplicateScriptEntrypoints(spec: PackageSpec): DuplicateScriptEntrypoint[] {
  const grouped = new Map<string, { entrypoint: string; scriptKeys: string[] }>();
  for (const script of spec.scripts ?? []) {
    const entrypoint = script.entrypoint.normalize('NFKC').trim();
    if (!entrypoint) continue;
    const normalized = entrypoint.toLocaleLowerCase('en-US');
    const current = grouped.get(normalized);
    if (current) current.scriptKeys.push(script.key);
    else grouped.set(normalized, { entrypoint, scriptKeys: [script.key] });
  }
  return [...grouped.values()]
    .filter((item) => item.scriptKeys.length > 1)
    .map((item) => ({ ...item, scriptKeys: [...item.scriptKeys] }));
}

export function validateSpecIntegrity(
  spec: PackageSpec,
  selectedQuestions: string[] = [],
  analysis?: AnalysisResult,
  options: { validateScriptExecutionContract?: boolean } = {},
): CoverageResult {
  const errors: string[] = [];
  const objectKeys = new Set((spec.objects ?? []).map((item) => normalizeKey(item.key)));
  const ruleKeys = new Set((spec.rules ?? []).map((item) => normalizeKey(item.key)));
  const metricKeys = new Set((spec.metrics ?? []).map((item) => normalizeKey(item.key)));
  const operationKeys = new Set((spec.operations ?? []).map((item) => normalizeKey(item.key)));
  const scriptKeys = new Set((spec.scripts ?? []).map((item) => normalizeKey(item.key)));

  for (const [label, keys] of [
    ['对象', (spec.objects ?? []).map((item) => normalizeKey(item.key))],
    ['操作', (spec.operations ?? []).map((item) => normalizeKey(item.key))],
    ['规则', (spec.rules ?? []).map((item) => normalizeKey(item.key))],
    ['指标', (spec.metrics ?? []).map((item) => normalizeKey(item.key))],
    ['脚本', (spec.scripts ?? []).map((item) => normalizeKey(item.key))],
  ] as const) {
    for (const key of findDuplicates(keys)) errors.push(`${label} key 重复：${key}`);
  }

  for (const conflict of findObjectAliasConflicts(spec.objects ?? [])) {
    errors.push(describeObjectAliasConflict(conflict));
  }

  for (const conflict of duplicateScriptEntrypoints(spec)) {
    errors.push(`脚本入口文件冲突：${conflict.entrypoint} 被 ${conflict.scriptKeys.join('、')} 共同使用；每个脚本必须使用唯一 entrypoint`);
  }

  for (const object of spec.objects ?? []) {
    for (const relation of object.relations ?? []) {
      if (!objectKeys.has(normalizeKey(relation.target))) errors.push(`对象 ${object.key} 引用了不存在的对象 ${relation.target}`);
    }
  }

  for (const operation of spec.operations ?? []) {
    for (const objectKey of operation.relatedObjects ?? []) {
      if (!objectKeys.has(normalizeKey(objectKey))) errors.push(`操作 ${operation.key} 引用了不存在的对象 ${objectKey}`);
    }
    for (const ruleKey of operation.usesRules ?? []) {
      if (!ruleKeys.has(normalizeKey(ruleKey))) errors.push(`操作 ${operation.key} 引用了不存在的规则 ${ruleKey}`);
    }
    for (const metricKey of operation.usesMetrics ?? []) {
      if (!metricKeys.has(normalizeKey(metricKey))) errors.push(`操作 ${operation.key} 引用了不存在的指标 ${metricKey}`);
    }
    if (operation.script && !scriptKeys.has(normalizeKey(operation.script.key))) {
      errors.push(`操作 ${operation.key} 绑定了不存在的脚本 ${operation.script.key}`);
    }
  }

  for (const rule of spec.rules ?? []) {
    for (const objectKey of rule.appliesToObjects ?? []) {
      if (!objectKeys.has(normalizeKey(objectKey))) errors.push(`规则 ${rule.key} 引用了不存在的对象 ${objectKey}`);
    }
  }

  for (const metric of spec.metrics ?? []) {
    for (const objectKey of metric.usesObjects ?? []) {
      if (!objectKeys.has(normalizeKey(objectKey))) errors.push(`指标 ${metric.key} 引用了不存在的对象 ${objectKey}`);
    }
  }

  for (const script of spec.scripts ?? []) {
    if (!operationKeys.has(normalizeKey(script.boundOperation))) {
      errors.push(`脚本 ${script.key} 绑定了不存在的操作 ${script.boundOperation}`);
    }
    const operation = spec.operations.find((item) => normalizeKey(item.key) === normalizeKey(script.boundOperation));
    if (operation?.script && normalizeKey(operation.script.key) !== normalizeKey(script.key)) {
      errors.push(`脚本 ${script.key} 与操作 ${script.boundOperation} 的 script 绑定不一致`);
    }
    if (!/^[A-Za-z0-9_.-]+\.py$/.test(script.entrypoint) || script.entrypoint.includes('..')) {
      errors.push(`脚本 ${script.key} 的 entrypoint 非法：${script.entrypoint}`);
    }
    if (script.runtime !== 'python') errors.push(`脚本 ${script.key} 仅支持 python runtime`);
    if (!(script.timeoutSeconds > 0 && script.timeoutSeconds <= 300)) {
      errors.push(`脚本 ${script.key} 的 timeoutSeconds 必须在 1-300 秒之间`);
    }
    if (!script.sourceCode.trim()) errors.push(`脚本 ${script.key} 缺少实现代码`);
    try {
      validateGeneratedScriptSyntax(script);
      validateGeneratedScriptSecurity(script);
      if (options.validateScriptExecutionContract !== false) {
        validateGeneratedScriptExecutionContract(script);
      }
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error));
    }
  }

  if (analysis) errors.push(...findKnowledgeEvidenceProblems(spec));

  const coverage = coverageFor(spec, selectedQuestions);

  if (errors.length) throw new Error(`知识包规格不完整：\n- ${errors.join('\n- ')}`);
  validateSpecKeys(spec);
  return coverage;
}
