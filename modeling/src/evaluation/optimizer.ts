import { Type } from '@earendil-works/pi-ai';
import { buildTaskLlm, type LlmDeps } from '../llm/provider.js';
import { makeAgent, runAgent } from '../workflow/agent.js';
import { specMetricSchema, specObjectSchema, specOperationSchema, specRuleSchema, specScriptSchema } from '../workflow/tools.js';
import { questionsMatch, validateSpecIntegrity } from '../workflow/spec-validation.js';
import type { CheckFinding } from '../workflow/package-check.js';
import type { PackageSpec, Project } from '../types.js';
import type { EvalResult } from '../workflow/m3-repo.js';
import type { OptSuggestion } from '../workflow/m4-repo.js';
import type { FailureDiagnosis } from './failure-diagnosis.js';
import { attachKnowledgeChangeEvidence } from '../workflow/knowledge-evidence.js';
import { DATA_PROVIDER_SCRIPT_CONTRACT } from '../workflow/script-safety.js';

// M4 优化器（design.md 决策 8）：
// ① proposeOptimizations —— 读未通过的评测结果 + 当前规格，产出结构化优化建议；
//    诊断维度含「执行痕迹」：从 finalAnswer 提取实际引用的操作 vs 认领该问题的操作，
//    区分三类根因——知识缺失（add）/ 知识在包里但没被走到（改路由与交叉引用）/ 知识写错（改内容）。
// ② applyOptimizations —— 把专家采纳的建议应用到规格上，产出修订版（版本号由调用方递增）。
// 另有 findingsToSuggestions —— 把包体检的确定性发现直接转成建议（不经 LLM）。

export interface RawSuggestion {
  title: string; detail: string; kind: string; target?: string; relatedQuestion?: string;
  diagnosisIds?: string[];
  evidence?: string[];
  generalizationPlan?: string;
  validationPlan?: string;
}

export type OptimizationDiagnosis = FailureDiagnosis & {
  id?: string;
  resolutionStatus?: string;
  resolutionAction?: string;
  resolutionNote?: string;
  resolutionActor?: string;
};
type ResourceKind = 'object' | 'operation' | 'rule' | 'metric' | 'script';

export interface ScriptOptimizationDecision {
  action: 'fix_invocation' | 'modify_existing' | 'add_new';
  operationKey?: string;
  scriptKey?: string;
  reason: string;
}

const STABLE_DIAGNOSIS_ID = /^dg[_-][A-Za-z0-9][A-Za-z0-9_-]*$/u;
const EVALUATION_ORACLE_LANGUAGE = /(?:标准答案|参考答案|地面真值|(?:智能体|模型)(?:回答|答案|输出原文)|评测(?:答案|结果|得分|通过率)|(?:当前|本次|该条|单个|全部|所有)用例)/u;
const OPTIMIZATION_MAX_ATTEMPTS = 2;
const OPTIMIZATION_MAX_OUTPUT_TOKENS = 65_536;

export class OptimizationExpertReviewRequiredError extends Error {
  readonly validationErrors: string[];

  constructor(validationErrors: string[]) {
    const unique = [...new Set(validationErrors)];
    super(`优化器连续 ${OPTIMIZATION_MAX_ATTEMPTS} 次未能通过校验，需要专家确认是否继续修复或跳过：${unique.join('；')}`);
    this.name = 'OptimizationExpertReviewRequiredError';
    this.validationErrors = unique;
  }
}

const KIND_LITERALS = [
  'add_operation', 'modify_operation', 'add_object', 'modify_object',
  'add_rule', 'modify_rule', 'add_metric', 'modify_metric', 'clarify_description',
  'add_script', 'modify_script',
] as const;

function resourceKindFromSuggestion(kind: string): ResourceKind | undefined {
  if (kind.endsWith('_object')) return 'object';
  if (kind.endsWith('_operation')) return 'operation';
  if (kind.endsWith('_rule')) return 'rule';
  if (kind.endsWith('_metric')) return 'metric';
  if (kind.endsWith('_script')) return 'script';
  return undefined;
}

const catalogItems = (spec: PackageSpec, kind: ResourceKind): { key: string; title?: string }[] => {
  if (kind === 'object') return spec.objects;
  if (kind === 'operation') return spec.operations;
  if (kind === 'rule') return spec.rules ?? [];
  if (kind === 'metric') return spec.metrics ?? [];
  return spec.scripts ?? [];
};

function targetMatches(item: { key: string; title?: string }, target: string): boolean {
  const normalized = target.trim().normalize('NFKC');
  const title = item.title?.trim().normalize('NFKC') ?? '';
  return normalized === item.key.normalize('NFKC')
    || Boolean(title && normalized === title)
    || Boolean(title && normalized === `${item.key}:${title}`.normalize('NFKC'))
    || Boolean(title && normalized === `${item.key}：${title}`.normalize('NFKC'));
}

/**
 * 把模型可能输出的 key:title 或唯一标题转成真实资源 key。
 * 新增建议的 target 是将要创建的 key，只做空白清理，不与已有资源做标题映射。
 */
export function normalizeOptimizationSuggestionTargets<T extends RawSuggestion>(
  suggestions: T[],
  spec: PackageSpec,
): T[] {
  return suggestions.map((suggestion) => {
    const rawTarget = suggestion.target?.trim();
    if (suggestion.kind.startsWith('add_')) {
      return { ...suggestion, ...(rawTarget ? { target: rawTarget } : { target: undefined }) };
    }
    if (!rawTarget) {
      throw new Error(`优化建议「${suggestion.title}」缺少 target，无法定位要修改的知识资源`);
    }
    const kinds = suggestion.kind === 'clarify_description'
      ? (['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[])
      : [resourceKindFromSuggestion(suggestion.kind)].filter(Boolean) as ResourceKind[];
    const matches = kinds.flatMap((kind) => catalogItems(spec, kind)
      .filter((item) => targetMatches(item, rawTarget))
      .map((item) => ({ kind, key: item.key })));
    const unique = [...new Map(matches.map((item) => [`${item.kind}:${item.key}`, item])).values()];
    if (unique.length !== 1) {
      const scope = kinds.length === 1 ? `${kinds[0]} ` : '';
      throw new Error(`优化建议「${suggestion.title}」的 target ${rawTarget} 无法唯一定位到${scope}资源 key`);
    }
    return { ...suggestion, target: unique[0].key };
  });
}

function diagnosisText(diagnosis: OptimizationDiagnosis): string {
  return [
    diagnosis.title, diagnosis.summary, diagnosis.suggestedAction,
    ...(diagnosis.evidence ?? []), ...(diagnosis.relatedQuestions ?? []),
  ].join('\n').normalize('NFKC');
}

/**
 * 脚本修复决策：先判断既有脚本是否被调用，再判断原脚本是否可承担本次职责。
 * 只有无既有脚本，或证据明确表明输入输出契约/职责不兼容时，才建议新增脚本。
 */
export function decideScriptOptimization(
  spec: PackageSpec,
  diagnosis: OptimizationDiagnosis,
): ScriptOptimizationDecision | undefined {
  if (diagnosis.category !== 'needs_script') return undefined;
  const text = diagnosisText(diagnosis);
  let operationMatches = spec.operations.filter((operation) => (
    text.includes(operation.key)
    || Boolean(operation.title && text.includes(operation.title))
    || (diagnosis.relatedQuestions ?? []).some((question) => questionsMatch(operation.competencyQuestion, question))
  ));
  if (!operationMatches.length && spec.operations.length === 1) operationMatches = [spec.operations[0]];
  if (operationMatches.length !== 1) {
    return {
      action: 'add_new',
      reason: '无法唯一定位承载该能力的 operation，需要由专家先确认责任边界和脚本契约。',
    };
  }
  const operation = operationMatches[0];
  const scriptKey = operation.script?.key
    ?? (spec.scripts ?? []).find((script) => script.boundOperation === operation.key)?.key;
  const existingScript = scriptKey
    ? (spec.scripts ?? []).find((script) => script.key === scriptKey)
    : undefined;
  if (!existingScript) {
    return {
      action: 'add_new', operationKey: operation.key,
      reason: `operation:${operation.key} 尚未绑定可修复的脚本，需要新增脚本并完成绑定。`,
    };
  }
  const notInvoked = /(?:execute_script|脚本).{0,32}(?:调用次数\s*[:=]?\s*0|\b0\s*次|未调用|没有调用|未执行)/iu.test(text);
  if (notInvoked) {
    return {
      action: 'fix_invocation', operationKey: operation.key, scriptKey: existingScript.key,
      reason: '已绑定脚本，但执行轨迹显示未调用，应先修复 operation 路由和参数传递。',
    };
  }
  const independentContract = /(?:独立职责|责任边界不同|与原脚本.{0,20}(?:契约不兼容|输入输出不兼容)|需要隔离执行|独立输入输出契约)/u.test(text);
  if (independentContract) {
    return {
      action: 'add_new', operationKey: operation.key, scriptKey: existingScript.key,
      reason: '现有脚本与新能力的职责或输入输出契约不兼容，可以新增独立脚本。',
    };
  }
  return {
    action: 'modify_existing', operationKey: operation.key, scriptKey: existingScript.key,
    reason: '已有脚本承担同一 operation 职责，应先修改原脚本补齐能力。',
  };
}

function validateScriptOptimizationDecisions(
  suggestions: RawSuggestion[],
  diagnoses: OptimizationDiagnosis[],
  spec: PackageSpec,
): void {
  const diagnosisById = new Map(diagnoses.filter((item) => item.id).map((item) => [item.id!, item]));
  for (const suggestion of suggestions) {
    if (suggestion.kind === 'add_script' && suggestion.target
      && spec.operations.some((operation) => operation.key === suggestion.target)) {
      throw new Error(`优化建议「${suggestion.title}」的 target ${suggestion.target} 是 operation key；add_script 必须使用新脚本 key`);
    }
    const linked = (suggestion.diagnosisIds ?? [])
      .map((id) => diagnosisById.get(id))
      .filter((diagnosis): diagnosis is OptimizationDiagnosis => diagnosis?.category === 'needs_script');
    for (const diagnosis of linked) {
      const decision = decideScriptOptimization(spec, diagnosis);
      if (!decision) continue;
      if (decision.action === 'fix_invocation'
        && (suggestion.kind !== 'modify_operation' || suggestion.target !== decision.operationKey)) {
        throw new Error(`优化建议「${suggestion.title}」的脚本决策不正确：${decision.reason}应使用 modify_operation，target=${decision.operationKey}`);
      }
      if (decision.action === 'modify_existing'
        && (suggestion.kind !== 'modify_script' || suggestion.target !== decision.scriptKey)) {
        throw new Error(`优化建议「${suggestion.title}」的脚本决策不正确：${decision.reason}应使用 modify_script，target=${decision.scriptKey}`);
      }
      if (decision.action === 'add_new' && suggestion.kind !== 'add_script') {
        throw new Error(`优化建议「${suggestion.title}」的脚本决策不正确：${decision.reason}应使用 add_script`);
      }
    }
  }
}

// 从回答文本里提取被引用的操作/对象 key（执行痕迹，确定性提取）
export function extractCitedKeys(text: string, spec: PackageSpec): string[] {
  const keys = [
    ...spec.operations.map((o) => o.key), ...spec.objects.map((o) => o.key),
    ...(spec.rules ?? []).map((r) => r.key), ...(spec.metrics ?? []).map((m) => m.key),
    ...(spec.scripts ?? []).map((script) => script.key),
  ];
  const hit = keys.filter((k) => k.length >= 3 && text.includes(k));
  return [...new Set(hit)];
}

// 认领该问题的操作（消费端路由的"应答入口"）
function claimingOps(question: string, spec: PackageSpec): string[] {
  return spec.operations
    .filter((op) => questionsMatch(op.competencyQuestion, question))
    .map((op) => op.key);
}

// 包体检确定性发现 → 优化建议（错误/警告级、带修复建议的才转；提示级噪音大不转）
const FINDING_KIND: Record<string, string> = {
  典型问题重复认领: 'modify_operation', 悬空引用: 'modify_operation',
  领域问题未覆盖: 'add_operation', 操作不完整: 'modify_operation',
  数据映射失效: 'modify_object', 主键失配: 'modify_object',
};
function findingKind(finding: CheckFinding): string {
  if (finding.category === '悬空引用' && finding.message.includes('引用了不存在的规则')) return 'add_rule';
  if (finding.category === '悬空引用' && finding.message.includes('引用了不存在的指标')) return 'add_metric';
  return FINDING_KIND[finding.category] ?? 'clarify_description';
}
function findingTarget(finding: CheckFinding): string {
  const missing = finding.message.match(/不存在的(?:规则|指标)\s+([^\s，。；]+)/)?.[1];
  return missing ?? finding.target;
}
export function findingsToSuggestions(findings: CheckFinding[]): RawSuggestion[] {
  return findings
    .filter((f) => f.severity !== '提示' && f.suggestion)
    .map((f) => ({
      title: `[体检] ${f.category}：${f.target}`,
      detail: `${f.message}。修复：${f.suggestion}`,
      kind: findingKind(f),
      target: findingTarget(f),
    }));
}

function hasForbiddenScriptFailureFallback(value: string): boolean {
  const text = String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN');
  const failures = [...text.matchAll(/(?:execute_script|脚本(?:执行|运行)?|script(?: execution)?).{0,40}(?:失败|异常|不可用|超时|报错|错误|未成功|fail(?:ed|ure)?|error|unavailable|timeout)/giu)];
  if (!failures.length) return false;
  const alternatives = [
    ...text.matchAll(/(?:人工|手工|模型|智能体|llm|business_data_search|ontology_context|业务数据查询).{0,32}(?:计算|推算|复算|复刻|汇总|作答|回答|生成结果|执行|calculate|compute|reason|answer)/giu),
    ...text.matchAll(/(?:计算|推算|复算|复刻|汇总|作答|回答|生成结果|calculate|compute|reason|answer).{0,20}(?:人工|手工|模型|智能体|llm)/giu),
  ];
  const punctuation = /[。！？；;，,\n]/u;
  const negation = /(?:不得|禁止|严禁|不允许|不能|不可|不应|不要|must\s+not|do\s+not|never|cannot|can't)[^。！？；;，,\n]{0,28}$/iu;
  return failures.some((failure) => alternatives.some((alternative) => {
    const failureIndex = failure.index ?? 0;
    const alternativeIndex = alternative.index ?? 0;
    if (Math.abs(failureIndex - alternativeIndex) > 240) return false;
    let clauseStart = alternativeIndex - 1;
    while (clauseStart >= 0 && !punctuation.test(text[clauseStart])) clauseStart -= 1;
    const prefix = text.slice(clauseStart + 1, alternativeIndex);
    return !negation.test(prefix);
  }));
}

function validateAuthoritativeScriptExecutionPolicy(spec: PackageSpec, revised: RevisedSpec): void {
  for (const operation of revised.operations) {
    if (!operation.script) continue;
    const before = spec.operations.find((item) => item.key === operation.key);
    if (before && JSON.stringify(before) === JSON.stringify(operation)) continue;
    if (hasForbiddenScriptFailureFallback(JSON.stringify(operation))) {
      throw new Error(`优化候选违反确定性脚本权威执行路径：操作 ${operation.key} 包含脚本失败后的手工或替代计算回退`);
    }
  }
  for (const script of revised.scripts) {
    const before = (spec.scripts ?? []).find((item) => item.key === script.key);
    if (before && JSON.stringify(before) === JSON.stringify(script)) continue;
    if (hasForbiddenScriptFailureFallback(JSON.stringify(script))) {
      throw new Error(`优化候选违反确定性脚本权威执行路径：脚本 ${script.key} 包含失败后的手工或替代计算回退`);
    }
  }
}

function compactFingerprintText(value: string): string {
  return value
    .normalize('NFKC')
    .toLocaleLowerCase('zh-CN')
    .replace(/[^\p{Letter}\p{Number}]+/gu, '');
}

function containsUnmodeledGroundTruthFragment(
  proposedText: string,
  groundTruth: string,
  packageText: string,
): boolean {
  const truth = compactFingerprintText(groundTruth);
  const proposed = compactFingerprintText(proposedText);
  const existingPackage = compactFingerprintText(packageText);
  if (truth.length >= 8 && proposed.includes(truth) && !existingPackage.includes(truth)) return true;
  if (truth.length < 18) return false;
  const windowSize = Math.min(32, Math.max(18, Math.floor(truth.length * 0.45)));
  for (let index = 0; index <= truth.length - windowSize; index += 1) {
    const fragment = truth.slice(index, index + windowSize);
    if (proposed.includes(fragment) && !existingPackage.includes(fragment)) return true;
  }
  return false;
}

function containsLongEvaluationQuestionFragment(proposedText: string, question: string): boolean {
  const source = compactFingerprintText(question);
  const proposed = compactFingerprintText(proposedText);
  if (source.length < 36) return false;
  const windowSize = Math.min(64, Math.max(36, Math.floor(source.length * 0.35)));
  for (let index = 0; index <= source.length - windowSize; index += 1) {
    if (proposed.includes(source.slice(index, index + windowSize))) return true;
  }
  return false;
}

function safeOptimizationEvidence(item: string): boolean {
  if (EVALUATION_ORACLE_LANGUAGE.test(item)) return false;
  return !item.startsWith('diagnosis:') || STABLE_DIAGNOSIS_ID.test(item.slice('diagnosis:'.length));
}

function sanitizeOptimizationPromptSpec(spec: PackageSpec, results: EvalResult[] = []): PackageSpec {
  const cleanEvidence = <T extends { evidence?: string[] }>(resource: T): T => ({
    ...resource,
    ...(resource.evidence ? { evidence: resource.evidence.filter(safeOptimizationEvidence) } : {}),
  });
  const sanitized = {
    ...spec,
    objects: spec.objects.map((object) => ({
      ...object,
      ...(object.relations ? { relations: object.relations.map((relation) => cleanEvidence(relation)) } : {}),
    })),
    operations: spec.operations.map(cleanEvidence),
    rules: (spec.rules ?? []).map(cleanEvidence),
    metrics: (spec.metrics ?? []).map(cleanEvidence),
    scripts: (spec.scripts ?? []).map(cleanEvidence),
  };
  const redactStrings = (value: unknown): unknown => {
    if (typeof value === 'string') return redactEvaluationExampleNumbers(value, results);
    if (Array.isArray(value)) return value.map(redactStrings);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactStrings(item)]));
    }
    return value;
  };
  const { scripts, ...withoutScripts } = sanitized;
  const redacted = redactStrings(withoutScripts) as Omit<PackageSpec, 'scripts'>;
  return {
    ...redacted,
    scripts: scripts.map((script) => {
      const { sourceCode, ...metadata } = script;
      return {
        ...(redactStrings(metadata) as Omit<typeof script, 'sourceCode'>),
        // 源码里的字符串和注释可以含输入样例；可执行数字则可能是单位换算、
        // 阈值或算法常量，不能替换成文本占位符破坏语法与业务语义。
        sourceCode: redactEvaluationPromptSource(sourceCode, results),
      };
    }),
  };
}

function distinctiveNumericSignatures(value: string): Set<string> {
  const signatures = new Set<string>();
  for (const match of String(value || '').matchAll(/[-－]?[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?[%％]?/gu)) {
    const raw = match[0];
    const numericText = raw.normalize('NFKC').replace(/[,％%]/gu, '');
    const number = Number(numericText);
    if (!Number.isFinite(number)) continue;
    const fractionDigits = numericText.split('.')[1]?.length ?? 0;
    const integerDigits = numericText.replace(/^-/, '').split('.')[0].length;
    const isGenericBoundary = [0, 1, 100].includes(Math.abs(number));
    const isDistinctivePercent = /[%％]$/u.test(raw) && !isGenericBoundary
      && (fractionDigits > 0 || Math.abs(number) >= 10);
    if (!isGenericBoundary && (fractionDigits >= 2 || integerDigits >= 4 || isDistinctivePercent)) {
      signatures.add(String(number));
    }
  }
  return signatures;
}

function validationPlanReusesEvaluationNumbers(plan: string, results: EvalResult[]): boolean {
  const evaluationNumbers = new Set(results.flatMap((result) => [
    result.question,
    result.groundTruth,
    result.finalAnswer ?? '',
    result.reason ?? '',
  ]).flatMap((value) => [...distinctiveNumericSignatures(value)]));
  return [...distinctiveNumericSignatures(plan)].some((signature) => evaluationNumbers.has(signature));
}

function evaluationNumericSignatures(results: EvalResult[]): Set<string> {
  return new Set(results.flatMap((result) => [
    result.question,
    result.groundTruth,
    result.finalAnswer ?? '',
    result.reason ?? '',
  ]).flatMap((text) => [...distinctiveNumericSignatures(text)]));
}

const EVALUATION_EXAMPLE_CONTEXT = /(?:示例|样例|案例|测试输入|运行记录|曾出现数值|example|sample|test\s*case|batchAmount|批次列表|第\s*\d+\s*批)/iu;

function redactEvaluationExampleNumbers(value: string, results: EvalResult[]): string {
  const evaluationNumbers = evaluationNumericSignatures(results);
  if (!evaluationNumbers.size) return value;
  const text = String(value || '');
  return text.replace(/[-－]?[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?[%％]?/gu, (raw, offset: number) => {
    const number = Number(raw.normalize('NFKC').replace(/[,％%]/gu, ''));
    if (!Number.isFinite(number) || !evaluationNumbers.has(String(number))) return raw;
    const context = text.slice(Math.max(0, offset - 120), Math.min(text.length, offset + raw.length + 120));
    return EVALUATION_EXAMPLE_CONTEXT.test(context.normalize('NFKC')) ? '<运行时数值>' : raw;
  });
}

function redactEvaluationPromptSource(value: string, results: EvalResult[]): string {
  const source = String(value || '');
  let output = '';
  let index = 0;
  while (index < source.length) {
    const char = source[index];
    if (char === '#') {
      const newline = source.indexOf('\n', index);
      const end = newline < 0 ? source.length : newline;
      output += redactEvaluationExampleNumbers(source.slice(index, end), results);
      index = end;
      continue;
    }
    if (char !== '"' && char !== "'") {
      output += char;
      index += 1;
      continue;
    }
    const triple = source.slice(index, index + 3) === char.repeat(3);
    const delimiter = triple ? char.repeat(3) : char;
    let end = index + delimiter.length;
    while (end < source.length) {
      if (source.slice(end, end + delimiter.length) === delimiter) {
        let backslashes = 0;
        for (let cursor = end - 1; cursor >= index && source[cursor] === '\\'; cursor -= 1) backslashes += 1;
        if (backslashes % 2 === 0) {
          end += delimiter.length;
          break;
        }
      }
      end += 1;
    }
    output += redactEvaluationExampleNumbers(source.slice(index, Math.min(end, source.length)), results);
    index = Math.min(end, source.length);
  }
  return output;
}

function evaluationExampleNumberSignatures(value: string, results: EvalResult[]): Set<string> {
  const evaluationNumbers = evaluationNumericSignatures(results);
  const signatures = new Set<string>();
  if (!evaluationNumbers.size) return signatures;
  const text = String(value || '').normalize('NFKC');
  for (const match of text.matchAll(/-?\d[\d,]*(?:\.\d+)?%?/gu)) {
    const numericText = match[0].replace(/[,％%]/gu, '');
    const number = Number(numericText);
    if (!Number.isFinite(number) || !evaluationNumbers.has(String(number))) continue;
    const index = match.index ?? 0;
    const context = text.slice(Math.max(0, index - 120), Math.min(text.length, index + match[0].length + 120));
    if (EVALUATION_EXAMPLE_CONTEXT.test(context)) {
      signatures.add(String(number));
    }
  }
  return signatures;
}

function containsNewEvaluationExampleNumber(
  value: string,
  beforeValue: string,
  results: EvalResult[],
): boolean {
  const inherited = evaluationExampleNumberSignatures(beforeValue, results);
  return [...evaluationExampleNumberSignatures(value, results)]
    .some((signature) => !inherited.has(signature));
}

function safeDiagnosisPromptValue(value: string): string {
  return String(value || '')
    .normalize('NFKC')
    .replace(/(?:标准答案|参考答案|地面真值|(?:智能体|模型)(?:回答|答案|输出原文)|评测(?:答案|结果|得分|通过率)|(?:当前|本次|该条|单个)用例)/gu, '已确认业务口径')
    .replace(/(?<![\p{Letter}_])-?\d[\d,]*(?:\.\d+)?%?(?![\p{Letter}_])/gu, '<运行时数值>');
}

function safeOptimizationSuggestionValue(value: string): string {
  const safe = safeDiagnosisPromptValue(value);
  const normalized = safe.replace(/\n/gu, ' ');
  const reversesContract = /(?:从|将).{0,48}(?:命名键|命名列|命名字段|语义字段).{0,48}(?:改为|替换为|切换到).{0,48}(?:位置索引|数字索引|位置键)/iu.test(normalized)
    || /(?:必须|只能|一律|仅).{0,40}(?:位置索引|数字索引|位置键).{0,80}(?:不得|禁止|不能).{0,32}(?:命名键|命名列|命名字段|语义字段)/iu.test(normalized);
  if (!reversesContract) return safe;
  return '按数据契约兼容读取：本体语义字段优先，来源列字段其次；仅当前两者缺失或为空时回退位置索引，并对数值做显式归一。';
}

function safeDiagnosisPrompt(diagnoses: OptimizationDiagnosis[]): string {
  return JSON.stringify(diagnoses.map((diagnosis) => ({
    id: diagnosis.id,
    category: diagnosis.category,
    title: safeDiagnosisPromptValue(diagnosis.title),
    summary: safeDiagnosisPromptValue(diagnosis.summary),
    evidence: diagnosis.evidence.map(safeDiagnosisPromptValue),
    confidence: diagnosis.confidence,
    blocking: diagnosis.blocking,
    repairScope: diagnosis.repairScope,
    packageOptimizable: diagnosis.packageOptimizable,
    suggestedAction: safeDiagnosisPromptValue(diagnosis.suggestedAction),
    expertResolution: diagnosis.resolutionStatus && diagnosis.resolutionStatus !== 'pending' ? {
      status: diagnosis.resolutionStatus,
      action: diagnosis.resolutionAction,
      note: safeDiagnosisPromptValue(diagnosis.resolutionNote ?? ''),
      actor: diagnosis.resolutionActor,
    } : undefined,
  })));
}

function optimizationResourceCatalog(spec: PackageSpec) {
  return {
    objects: spec.objects.map((item) => ({ key: item.key, title: item.title })),
    operations: spec.operations.map((item) => ({
      key: item.key, title: item.title, scriptKey: item.script?.key,
    })),
    rules: (spec.rules ?? []).map((item) => ({ key: item.key, title: item.title })),
    metrics: (spec.metrics ?? []).map((item) => ({ key: item.key, title: item.title })),
    scripts: (spec.scripts ?? []).map((item) => ({
      key: item.key, title: item.title, boundOperation: item.boundOperation, entrypoint: item.entrypoint,
    })),
  };
}

function scriptDecisionPrompt(spec: PackageSpec, diagnoses: OptimizationDiagnosis[]): string {
  const decisions = diagnoses
    .filter((diagnosis) => diagnosis.category === 'needs_script')
    .map((diagnosis) => ({ diagnosisId: diagnosis.id, ...decideScriptOptimization(spec, diagnosis) }));
  return decisions.length ? JSON.stringify(decisions) : '无';
}

/**
 * 优化建议入库前的证据与过拟合门禁。
 * 模型可以选择修改方式，但不能跳过根因关联、通用化设计和样例外验证。
 */
export function validateOptimizationSuggestions(
  suggestions: RawSuggestion[],
  diagnoses: OptimizationDiagnosis[],
  failedResults: EvalResult[],
  spec: PackageSpec,
): void {
  if (!diagnoses.length) return; // 兼容旧版命令行冒烟测试；生产路由层强制先诊断。
  const diagnosisById = new Map(diagnoses.filter((item) => item.id).map((item) => [item.id!, item]));
  const resultById = new Map(failedResults.map((item) => [item.id, item]));
  const packageText = JSON.stringify(spec).normalize('NFKC');

  for (const suggestion of suggestions) {
    const linked = (suggestion.diagnosisIds ?? []).map((id) => diagnosisById.get(id)).filter(Boolean) as OptimizationDiagnosis[];
    if (!linked.length) throw new Error(`优化建议「${suggestion.title}」未关联可验证的失败原因`);
    if (linked.some((item) => !item.packageOptimizable || item.blocking)) {
      throw new Error(`优化建议「${suggestion.title}」关联了不应修改知识包的阻断问题`);
    }
    if (!(suggestion.evidence ?? []).some((item) => item.trim())) {
      throw new Error(`优化建议「${suggestion.title}」缺少证据`);
    }
    if (!suggestion.generalizationPlan?.trim()) {
      throw new Error(`优化建议「${suggestion.title}」缺少通用化方案`);
    }
    if (!suggestion.validationPlan?.trim()) {
      throw new Error(`优化建议「${suggestion.title}」缺少样例外验证方案`);
    }

    const proposedText = [
      suggestion.title,
      suggestion.detail,
      ...(suggestion.evidence ?? []),
      suggestion.generalizationPlan,
      suggestion.validationPlan,
    ]
      .join('\n').normalize('NFKC');
    if (hasForbiddenScriptFailureFallback(proposedText)) {
      throw new Error(`优化建议「${suggestion.title}」违反确定性脚本权威执行路径：脚本失败后不得回退到模型、人工或其他工具计算`);
    }
    if (EVALUATION_ORACLE_LANGUAGE.test(proposedText)) {
      throw new Error(`优化建议「${suggestion.title}」包含评测裁判表述；请只引用资料字段、执行轨迹和稳定诊断，不得携带答案或评测结果原文`);
    }
    if (/(?:当前阶段|本阶段|暂时).{0,100}(?:后续|以后|另行).{0,50}(?:补充|完善|修复|处理)/su.test(proposedText)) {
      throw new Error(`优化建议「${suggestion.title}」是明知遗漏必要组成项的分阶段修复，不是可验证的完整修复`);
    }
    const linkedResultIds = new Set(linked.flatMap((item) => item.resultIds));
    const linkedResults = [...linkedResultIds]
      .map((resultId) => resultById.get(resultId))
      .filter(Boolean) as EvalResult[];
    if (validationPlanReusesEvaluationNumbers(suggestion.validationPlan ?? '', linkedResults)) {
      throw new Error(`优化建议「${suggestion.title}」的验证方案复用了失败轮次的特征数值；请改用符号变量、性质断言或新的边界组合`);
    }
    for (const resultId of linkedResultIds) {
      const truth = resultById.get(resultId)?.groundTruth?.trim().normalize('NFKC') ?? '';
      if (containsUnmodeledGroundTruthFragment(proposedText, truth, packageText)) {
        throw new Error(`优化建议「${suggestion.title}」直接复制了标准答案，存在过拟合风险`);
      }
    }
    if (/第\s*\d+\s*[-~〜—至]\s*\d+\s*批/u.test(proposedText)) {
      throw new Error(`优化建议「${suggestion.title}」硬编码了用例批次范围，存在过拟合风险`);
    }
    if (/\d{1,2}\s*月\s*(?:为|按)\s*\d{2}\s*天/u.test(proposedText)) {
      throw new Error(`优化建议「${suggestion.title}」硬编码了用例日历常量，存在过拟合风险`);
    }
  }
}

export async function proposeOptimizations(
  project: Project,
  spec: PackageSpec,
  failedResults: EvalResult[],
  llmOverride?: LlmDeps,
  checkFindings: CheckFinding[] = [],
  diagnoses: OptimizationDiagnosis[] = [],
): Promise<RawSuggestion[]> {
  const cap: { suggestions?: RawSuggestion[] } = {};
  const submitSuggestions = {
    name: 'submit_suggestions',
    label: '提交优化建议',
    description: '针对未通过的评测用例，提交对知识包规格的结构化优化建议（每条建议对应一处可落地的修改）',
    parameters: Type.Object({
      suggestions: Type.Array(Type.Object({
        title: Type.String(),
        detail: Type.String(),
        kind: Type.Union(KIND_LITERALS.map((k) => Type.Literal(k)) as any),
        target: Type.Optional(Type.String()),
        relatedQuestion: Type.Optional(Type.String()),
        diagnosisIds: Type.Array(Type.String()),
        evidence: Type.Array(Type.String()),
        generalizationPlan: Type.String(),
        validationPlan: Type.String(),
      })),
    }),
    async execute(_id: string, params: { suggestions: RawSuggestion[] }) {
      cap.suggestions = params.suggestions;
      return { content: [{ type: 'text' as const, text: `已提交 ${params.suggestions.length} 条建议。` }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包优化器。评测中部分用例未通过，请先按执行痕迹诊断根因，再给可落地的优化建议：',
    '每条用例给出了「应答操作」（认领该问题的操作）与「实际引用」（回答里真实用到的操作）。三类根因分别处理：',
    'A) 知识缺失——先判断缺的资源类型：数据实体用 add_object，适用条件/判断门槛/例外用 add_rule，',
    '   公式/统计粒度/边界用 add_metric，查询或办理流程用 add_operation；不要把所有缺失都塞进 operation。',
    'B) 知识在包里但没被走到——应答操作含正确口径但实际引用的是别的操作 → modify_operation 修路由：',
    '   收紧被误走操作的认领/描述、加“该问题必须走 XX 操作”的交叉引用；不要重复新增已有规则；',
    'C) 知识写错——实际走了应答操作但结论仍错 → modify_operation 修正该操作的口径/流程措辞（写成不可绕过的强约束、',
    '   把追溯/遍历步骤写成逐行指令，避免执行时漏项）。若同一操作在不同用例中时对时错（执行随机性），',
    '   把口径下沉为可直接执行的查询条件写进 flow（如「查询条件必须为 状态 IN (在库,在制)，禁止只查单一状态」、',
    '   「组合型问题必须分别执行并列出各组成部分结果，任一部分为 0 时先复核再汇总」），杜绝转述丢失口径。',
    '多步骤计算、排序、聚合或需要可重复执行的数据处理，应优先用 add_script/modify_script，而不是把公式和中间结果堆在 operation 文字里。',
    '建议必须完整解决关联诊断中已确认的根因。若资料或现有规格把输入定义为多个组成项的集合或和，公式修复必须覆盖全部必需项，不得只修一项再把其余项留到后续。',
    '对 blocking=true 或 packageOptimizable=false 的数据缺口，不得用 modify_object 伪装成补数据，也不得临时改用另一场景字段，除非源资料明确证明两者口径等价。',
    'kind 取值：add_operation / modify_operation / add_object / modify_object / add_rule / modify_rule / add_metric / modify_metric / add_script / modify_script / clarify_description；',
    '只有上下文明确提供了可复用的问题原文时才填写 relatedQuestion；否则留空，不得从评测问题猜测或复写。target 填要改的操作/对象/规则/指标/脚本 key。',
    '每条建议必须填 diagnosisIds、evidence、generalizationPlan 和 validationPlan。禁止照抄标准答案，禁止硬编码某条用例的批次编号、日期和数值结果。',
    '建议的任何字段（包括 evidence）都不得出现标准答案、参考答案、地面真值、智能体回答、评测结果或当前用例等裁判表述；',
    '证据必须改写为资料字段、规则来源、脚本错误、执行轨迹事实或稳定诊断编号。',
    '确定性脚本是绑定 operation 的唯一权威计算路径。脚本失败时只能修复、重试或报告阻断；禁止建议由模型/人工手算，禁止改用 ontology_context、business_data_search 或其他工具复刻计算结果。',
    '验证方案必须包含至少一组不在当前失败用例中的边界或变体场景，且期望性质要从公式或来源证据推导，不得抄当前用例答案的数字。用 submit_suggestions 提交。',
    '验证方案不得复用失败问题、答案、模型输出或判分原因中的精确百分比、精确小数和长整数；请使用符号变量、性质断言或全新的边界组合。',
    '修改建议的 target 必须严格填下方资源目录中的 key，不得填标题，不得填 key:title。新增建议的 target 填将创建的新 key。',
    '脚本诊断必须遵循服务端决策：已有脚本但未调用时先 modify_operation；同一职责能力不足时先 modify_script；只有无脚本或契约/职责不兼容时才 add_script。',
    `项目背景：${project.background || project.domain || project.name}`,
    `当前资源目录：${JSON.stringify(optimizationResourceCatalog(spec))}`,
    `脚本修复决策：${scriptDecisionPrompt(spec, diagnoses)}`,
  ].join('\n');

  // 压成单行：faux 分支按行正则解析，多行的标准答案/回答会破坏对应关系
  const one = (s: string) => (s || '').replace(/\s*\n\s*/g, ' ').trim();
  const failLines = failedResults.map((r, i) => {
    const answer = one(r.finalAnswer || '（无）');
    const cited = extractCitedKeys(answer, spec);
    const claiming = claimingOps(r.question, spec);
    return `${i + 1}. 问题：${one(r.question)} ｜标准答案：${one(r.groundTruth)} ｜判定：${r.verdict}` +
      ` ｜应答操作：${claiming.join(',') || '（无认领）'} ｜实际引用：${cited.join(',') || '（未识别）'}` +
      ` ｜智能体回答：${answer.slice(0, 600)} ｜理由：${one(r.reason ?? '')}`;
  });
  const checkBlock = checkFindings.length
    ? `\n【包体检发现（确定性检查，可直接采信）】\n${checkFindings.map((f) => `- [${f.severity}/${f.category}] ${f.target}：${f.message}`).join('\n')}`
    : '';
  const diagnosisBlock = diagnoses.length
    ? `\n【已确认的失败原因（只能针对 packageOptimizable=true 且 blocking=false 的诊断提建议）】\n${safeDiagnosisPrompt(diagnoses)}`
    : '';
  const initialPrompt = diagnoses.length
    ? `请仅根据以下已确认诊断和包体检事实生成通用优化建议。不要索取、推测或复述原始评测问题与答案。${diagnosisBlock}${checkBlock}`
    : `未通过的评测用例如下，请给出优化建议：\n${failLines.join('\n')}${checkBlock}`;
  const llm = llmOverride ?? buildTaskLlm({ maxTokensCap: OPTIMIZATION_MAX_OUTPUT_TOKENS });
  let validationError: Error | undefined;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    cap.suggestions = undefined;
    const prompt = attempt === 1
      ? initialPrompt
      : [
          '上一次提交的优化建议未通过服务端门禁，请根据以下准确原因重新生成并提交完整 suggestions 数组：',
          validationError?.message ?? '未产出结构化建议。',
          '不要绕过或弱化门禁。删除具体用例的批次范围、日期、答案数值和样例专用分支，把它们改为运行时参数、来源证据和可验证的通用约束。',
          '删除标准答案、参考答案、地面真值、智能体回答、评测结果等裁判措辞；evidence 只保留资料、脚本错误、执行轨迹或稳定诊断依据。',
          '验证方案不得复用失败轮次中的精确百分比、精确小数或长整数；改用符号变量、性质断言或新的边界组合。',
          '本次重试的 validationPlan 不要写任何阿拉伯数字或带单位数值；使用“空输入、单位输入、满量程、缺失字段、字段顺序变化”等文字性质描述。',
          '仍然只能处理 packageOptimizable=true 且 blocking=false 的诊断，并保留每条建议的 diagnosisIds、evidence、generalizationPlan 与 validationPlan。',
          diagnosisBlock,
          checkBlock,
          '请立即调用 submit_suggestions 重新提交；未通过工具提交的正文不会保存。',
        ].join('\n');
    // 每次重试使用全新上下文，避免上一轮包含具体数值的建议继续锚定后续输出。
    const agent = makeAgent(llm, sys, [submitSuggestions] as any);
    await runAgent(agent, prompt);
    const suggestions = cap.suggestions as RawSuggestion[] | undefined;
    if (!suggestions?.length) {
      validationError = new Error('优化器未产出建议，请重试');
      if (attempt < 3) continue;
      throw validationError;
    }
    try {
      const normalized = normalizeOptimizationSuggestionTargets(suggestions, spec);
      validateOptimizationSuggestions(normalized, diagnoses, failedResults, spec);
      validateScriptOptimizationDecisions(normalized, diagnoses, spec);
      return normalized;
    } catch (error) {
      validationError = error instanceof Error ? error : new Error(String(error));
      if (attempt >= 3) throw validationError;
    }
  }
  throw validationError ?? new Error('优化器未产出建议，请重试');
}

type RevisedSpec = {
  objects: PackageSpec['objects']; operations: PackageSpec['operations'];
  rules: NonNullable<PackageSpec['rules']>; metrics: NonNullable<PackageSpec['metrics']>;
  scripts: NonNullable<PackageSpec['scripts']>;
};

export interface OptimizationApplyProgressEvent {
  phase: 'revision_started' | 'validation_failed' | 'validation_passed';
  attempt: number;
  maxAttempts: number;
  error?: string;
}

const resourceItems = (spec: PackageSpec | RevisedSpec, kind: ResourceKind): any[] => {
  if (kind === 'object') return spec.objects;
  if (kind === 'operation') return spec.operations;
  if (kind === 'rule') return spec.rules ?? [];
  if (kind === 'metric') return spec.metrics ?? [];
  return spec.scripts ?? [];
};

function changedRevisionPatch(spec: PackageSpec, revised: RevisedSpec): RevisedSpec {
  const changed = (kind: ResourceKind) => {
    const beforeByKey = new Map(resourceItems(spec, kind).map((item) => [item.key, item]));
    return resourceItems(revised, kind).filter((item) => {
      const before = beforeByKey.get(item.key);
      return !before || JSON.stringify(before) !== JSON.stringify(item);
    });
  };
  return {
    objects: changed('object'),
    operations: changed('operation'),
    rules: changed('rule'),
    metrics: changed('metric'),
    scripts: changed('script'),
  } as RevisedSpec;
}

function rejectedPatchCanReturnToModel(validationError: string): boolean {
  return !/(?:评测裁判|真实评测数值|真实评测问题|评测答案|答案片段|过拟合风险)/u.test(validationError);
}

function rejectedPatchForPrompt(
  spec: PackageSpec,
  revised: RevisedSpec | undefined,
  failedResults: EvalResult[],
): RevisedSpec | undefined {
  if (!revised) return undefined;
  const patch = changedRevisionPatch(spec, revised);
  if (![patch.objects, patch.operations, patch.rules, patch.metrics, patch.scripts].some((items) => items.length)) {
    return undefined;
  }
  const sanitized = sanitizeOptimizationPromptSpec({ ...spec, ...patch }, failedResults);
  return {
    objects: sanitized.objects,
    operations: sanitized.operations,
    rules: sanitized.rules ?? [],
    metrics: sanitized.metrics ?? [],
    scripts: sanitized.scripts ?? [],
  };
}

function kindFromSuggestion(kind: string): ResourceKind | undefined {
  return resourceKindFromSuggestion(kind);
}

function stripUnplannedAdditionsForRetryBase(
  spec: PackageSpec,
  revised: RevisedSpec,
  adopted: OptSuggestion[],
): RevisedSpec {
  const plannedTargets: Record<ResourceKind, Set<string>> = {
    object: new Set(), operation: new Set(), rule: new Set(), metric: new Set(), script: new Set(),
  };
  const untargetedBudget: Record<ResourceKind, number> = {
    object: 0, operation: 0, rule: 0, metric: 0, script: 0,
  };
  for (const suggestion of adopted) {
    const kind = kindFromSuggestion(suggestion.kind);
    if (!kind || !suggestion.kind.startsWith('add_')) continue;
    if (suggestion.target) plannedTargets[kind].add(suggestion.target);
    else untargetedBudget[kind] += 1;
  }
  const keep = (kind: ResourceKind) => {
    const existingKeys = new Set(resourceItems(spec, kind).map((item) => item.key));
    let remainingUntargeted = untargetedBudget[kind];
    return resourceItems(revised, kind).filter((item) => {
      if (existingKeys.has(item.key) || plannedTargets[kind].has(item.key)) return true;
      if (remainingUntargeted <= 0) return false;
      remainingUntargeted -= 1;
      return true;
    });
  };
  return {
    objects: keep('object'),
    operations: keep('operation'),
    rules: keep('rule'),
    metrics: keep('metric'),
    scripts: keep('script'),
  } as RevisedSpec;
}

function preserveUntouchedResources(
  spec: PackageSpec,
  revised: RevisedSpec,
  adopted: OptSuggestion[],
): RevisedSpec {
  const touched: Record<ResourceKind, Set<string>> = {
    object: new Set(), operation: new Set(), rule: new Set(), metric: new Set(), script: new Set(),
  };
  for (const suggestion of adopted) {
    let kind = kindFromSuggestion(suggestion.kind);
    if (suggestion.kind === 'clarify_description' && suggestion.target) {
      kind = (['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[])
        .find((candidate) => resourceItems(spec, candidate).some((item) => item.key === suggestion.target));
    }
    if (kind && suggestion.target && !suggestion.kind.startsWith('add_')) touched[kind].add(suggestion.target);
  }
  for (const suggestion of adopted.filter((item) => kindFromSuggestion(item.kind) === 'script')) {
    const submitted = suggestion.target
      ? revised.scripts.find((script) => script.key === suggestion.target)
      : revised.scripts.find((script) => !(spec.scripts ?? []).some((current) => current.key === script.key));
    const boundOperation = submitted?.boundOperation
      ? spec.operations.find((operation) => operation.key === submitted.boundOperation)
      : undefined;
    const bindingNeedsUpdate = Boolean(submitted && boundOperation
      && (suggestion.kind.startsWith('add_') || boundOperation.script?.key !== submitted.key));
    if (bindingNeedsUpdate && submitted?.boundOperation) {
      touched.operation.add(submitted.boundOperation);
    }
  }

  const preserve = (kind: ResourceKind) => {
    const before = resourceItems(spec, kind);
    const submitted = resourceItems(revised, kind);
    const beforeKeys = new Set(before.map((item) => item.key));
    const existing = before.map((item) => (
      touched[kind].has(item.key)
        ? submitted.find((candidate) => candidate.key === item.key) ?? item
        : item
    ));
    return [...existing, ...submitted.filter((item) => !beforeKeys.has(item.key))];
  };

  return {
    objects: preserve('object'),
    operations: preserve('operation'),
    rules: preserve('rule'),
    metrics: preserve('metric'),
    scripts: preserve('script'),
  } as RevisedSpec;
}

function completeDeclaredScriptBindings(
  spec: PackageSpec,
  revised: RevisedSpec,
  adopted: OptSuggestion[],
): RevisedSpec {
  const operations = revised.operations.map((operation) => ({ ...operation }));
  const claimedNewScripts = new Set<string>();
  for (const suggestion of adopted.filter((item) => kindFromSuggestion(item.kind) === 'script')) {
    const script = suggestion.target
      ? revised.scripts.find((item) => item.key === suggestion.target)
      : revised.scripts.find((item) => !(spec.scripts ?? []).some((current) => current.key === item.key)
        && !claimedNewScripts.has(item.key));
    if (!script?.boundOperation) continue;
    claimedNewScripts.add(script.key);
    const matches = operations
      .map((operation, index) => ({ operation, index }))
      .filter(({ operation }) => operation.key === script.boundOperation);
    if (matches.length !== 1 || matches[0].operation.script) continue;
    operations[matches[0].index] = {
      ...matches[0].operation,
      script: { key: script.key, required: true },
    };
  }
  return { ...revised, operations };
}

function validateOptimizationChanges(spec: PackageSpec, revised: RevisedSpec, adopted: OptSuggestion[]): void {
  const touched: Record<ResourceKind, Set<string>> = {
    object: new Set(), operation: new Set(), rule: new Set(), metric: new Set(), script: new Set(),
  };
  const addCounts: Record<ResourceKind, number> = { object: 0, operation: 0, rule: 0, metric: 0, script: 0 };

  for (const suggestion of adopted) {
    let kind = kindFromSuggestion(suggestion.kind);
    if (suggestion.kind === 'clarify_description') {
      if (!suggestion.target) throw new Error(`建议「${suggestion.title}」缺少 target，无法校验落实结果`);
      const matches = (['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[])
        .filter((candidate) => resourceItems(spec, candidate).some((item) => item.key === suggestion.target));
      if (matches.length !== 1) throw new Error(`建议「${suggestion.title}」的 target ${suggestion.target} 无法唯一定位`);
      [kind] = matches;
    }
    if (!kind) continue;

    if (suggestion.kind.startsWith('add_')) {
      addCounts[kind] += 1;
      if (suggestion.target) {
        const before = resourceItems(spec, kind).some((item) => item.key === suggestion.target);
        const after = resourceItems(revised, kind).some((item) => item.key === suggestion.target);
        if (before || !after) throw new Error(`建议「${suggestion.title}」未新增 ${kind}:${suggestion.target}`);
      }
      if (kind === 'script') {
        const added = resourceItems(revised, 'script').find((item) => item.key === suggestion.target)
          ?? resourceItems(revised, 'script').find((item) => !resourceItems(spec, 'script').some((old) => old.key === item.key));
        if (!added) throw new Error(`建议「${suggestion.title}」未新增脚本资源`);
        const boundOperation = revised.operations.find((operation) => operation.key === added.boundOperation);
        if (!boundOperation || boundOperation.script?.key !== added.key) {
          throw new Error(`建议「${suggestion.title}」新增的脚本 ${added.key} 未绑定到 operation:${added.boundOperation}`);
        }
        touched.operation.add(added.boundOperation);
      }
      continue;
    }

    if (!suggestion.target) throw new Error(`建议「${suggestion.title}」缺少 target，无法校验落实结果`);
    const before = resourceItems(spec, kind).find((item) => item.key === suggestion.target);
    const after = resourceItems(revised, kind).find((item) => item.key === suggestion.target);
    if (!before || !after || JSON.stringify(before) === JSON.stringify(after)) {
      throw new Error(`建议「${suggestion.title}」未落实到 ${kind}:${suggestion.target}`);
    }
    touched[kind].add(suggestion.target);
    if (kind === 'script') {
      const afterScript = resourceItems(revised, 'script').find((item) => item.key === suggestion.target);
      const boundOperation = afterScript?.boundOperation
        ? spec.operations.find((operation) => operation.key === afterScript.boundOperation)
        : undefined;
      if (afterScript && boundOperation?.script?.key !== afterScript.key) {
        touched.operation.add(afterScript.boundOperation);
      }
    }
  }

  for (const kind of ['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[]) {
    const beforeItems = resourceItems(spec, kind);
    const afterItems = resourceItems(revised, kind);
    const beforeKeys = new Set(beforeItems.map((item) => item.key));
    const newKeys = afterItems.filter((item) => !beforeKeys.has(item.key));
    if (newKeys.length !== addCounts[kind]) {
      throw new Error(`${kind} 新增数量与建议不一致：建议 ${addCounts[kind]} 条，实际 ${newKeys.length} 条`);
    }
    for (const item of beforeItems) {
      if (touched[kind].has(item.key)) continue;
      const after = afterItems.find((candidate) => candidate.key === item.key);
      if (!after || JSON.stringify(after) !== JSON.stringify(item)) {
        const label = { object: '对象', operation: '操作', rule: '规则', metric: '指标', script: '脚本' }[kind];
        throw new Error(`未涉及的${label} ${item.key} 被删除或替换`);
      }
    }
  }
}

function validateOptimizationRevisionAgainstEvaluationData(
  spec: PackageSpec,
  revised: RevisedSpec,
  failedResults: EvalResult[],
): void {
  if (!failedResults.length) return;
  for (const kind of ['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[]) {
    const beforeByKey = new Map(resourceItems(spec, kind).map((item) => [item.key, item]));
    for (const resource of resourceItems(revised, kind)) {
      const before = beforeByKey.get(resource.key);
      if (before && JSON.stringify(before) === JSON.stringify(resource)) continue;
      const resourceText = JSON.stringify(resource).normalize('NFKC');
      if (EVALUATION_ORACLE_LANGUAGE.test(resourceText)) {
        throw new Error(`优化候选 ${kind}:${resource.key} 包含评测裁判表述；请仅保留资料、流程确认、脚本契约和稳定诊断引用`);
      }
      if (containsNewEvaluationExampleNumber(
        resourceText,
        before ? JSON.stringify(before).normalize('NFKC') : '',
        failedResults,
      )) {
        throw new Error(`优化候选 ${kind}:${resource.key} 在示例或输入格式中复用了真实评测数值；请改用符号占位或独立合成样例`);
      }
      for (const result of failedResults) {
        if (containsLongEvaluationQuestionFragment(resourceText, result.question)) {
          throw new Error(`优化候选 ${kind}:${resource.key} 复制了真实评测问题片段；请改为参数化输入和通用业务边界`);
        }
        if (containsUnmodeledGroundTruthFragment(
          resourceText,
          result.groundTruth,
          before ? JSON.stringify(before) : '',
        )) {
          throw new Error(`优化候选 ${kind}:${resource.key} 复制了评测答案片段，存在过拟合风险`);
        }
      }
    }
  }
}

function suggestionRequirementText(suggestion: OptSuggestion): string {
  return [
    suggestion.title,
    suggestion.detail,
    ...(suggestion.evidence ?? []),
    suggestion.generalizationPlan ?? '',
    suggestion.validationPlan ?? '',
    suggestion.candidateFeedback ?? '',
  ].join('\n').normalize('NFKC').toLocaleLowerCase('zh-CN');
}

function dataProviderFieldOrderContract(spec: PackageSpec): string {
  const mappings = spec.objects.flatMap((object) => object.fields.map((field) => ({
    object: object.key,
    semanticKey: field.key,
    sourceColumn: field.sourceColumn || field.key,
  })));
  if (!mappings.length) return '';
  return [
    '【DataProvider 字段候选顺序契约】',
    '读取数据行时必须按“本体语义字段 → 来源列字段 → 位置索引”的顺序逐项跳过 null/空字符串。',
    '纯数字字符串键始终只能放在所有命名键之后；例如 first_non_empty(row, "semantic_key", "source_column", "0")，禁止把 "0" 放在前面。',
    `当前规格字段映射：${JSON.stringify(mappings)}`,
  ].join('\n');
}

function hasNonEmptyRowFallback(source: string): boolean {
  const tupleCheck = /(?:not\s+in\s*\(\s*None\s*,\s*["']{2}(?:\s*,[^)]*)?\)|in\s*\(\s*None\s*,\s*["']{2}(?:\s*,[^)]*)?\).{0,120}(?:continue|return)|is\s+not\s+None.{0,120}!=\s*["']{2})/su;
  const skipsNullOrBlank = /\bif\s+(?=[^:\n]{0,220}(?:is\s+None|==\s*None))(?=[^:\n]{0,220}(?:==\s*["']{2}|not\s+(?:(?:str\s*\([^)]*\)|[A-Za-z_]\w*)\s*\.\s*)?strip\s*\(\s*\)))[^:\n]+:\s*(?:\n[ \t]+)?(?:continue|return\b)/u;
  const returnsOnlyNonBlank = /\bif\s+(?=[^:\n]{0,220}(?:is\s+not\s+None|!=\s*None))(?=[^:\n]{0,220}(?:!=\s*["']{2}|(?:(?:str\s*\([^)]*\)|[A-Za-z_]\w*)\s*\.\s*)strip\s*\(\s*\)))[^:\n]+:\s*(?:\n[ \t]+)?return\b/u;
  return tupleCheck.test(source) || skipsNullOrBlank.test(source) || returnsOnlyNonBlank.test(source);
}

function hasReversedProviderFieldPrecedence(source: string, spec: PackageSpec): boolean {
  const fieldPairs = spec.objects.flatMap((object) => object.fields
    .filter((field) => field.sourceColumn && field.sourceColumn !== field.key)
    .map((field) => [field.key, field.sourceColumn!] as const));
  if (!fieldPairs.length) return false;
  const calls = source.matchAll(
    /\b(?:first_non_empty|get_field|read_field|resolve_field|lookup_field|pick_value|resolve_value|field_value)\s*\(([^)]*)\)/giu,
  );
  for (const call of calls) {
    const args = call[1] ?? '';
    for (const [semanticKey, sourceColumn] of fieldPairs) {
      const semantic = args.search(new RegExp(`["']${semanticKey.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}["']`, 'u'));
      const physical = args.search(new RegExp(`["']${sourceColumn.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}["']`, 'u'));
      if (semantic >= 0 && physical >= 0 && physical < semantic) return true;
    }
  }
  return false;
}

function hasExplicitNumericNormalization(source: string): boolean {
  return /\b(?:float|int|Decimal)\s*\(\s*[A-Za-z_][A-Za-z0-9_]*/u.test(source);
}

function hasPositionalBeforeSemanticFallback(source: string): boolean {
  const literalPositionFirst = /\b(?:safe_get|first_non_empty|get_field|read_field|resolve_field|lookup_field)\s*\(\s*[^,\n]+,\s*["']\d+["']\s*,\s*["'][A-Za-z_][A-Za-z0-9_]*["']/u;
  const wrappedPositionFirst = /\b(?:safe_get|first_non_empty|get_field|read_field|resolve_field|lookup_field)\s*\(\s*[^,\n]+,\s*(?:source|physical|position|positional|index|column)[A-Za-z0-9_]*\s*,\s*(?:semantic|ontology|named|primary)[A-Za-z0-9_]*/iu;
  return literalPositionFirst.test(source) || wrappedPositionFirst.test(source);
}

function structuredInputVariables(source: string): Set<string> {
  const variables = new Set<string>();
  for (const match of source.matchAll(
    /^\s*([A-Za-z_]\w*)\s*=\s*(?:json\.loads\s*\(|[A-Za-z_]\w*\.data\b)/gmu,
  )) variables.add(match[1]);

  for (let pass = 0; pass < 6; pass += 1) {
    let changed = false;
    for (const match of source.matchAll(/^\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*(?:or\s*\[\s*\])?\s*$/gmu)) {
      if (variables.has(match[2]) && !variables.has(match[1])) {
        variables.add(match[1]);
        changed = true;
      }
    }
    for (const match of source.matchAll(/\bfor\s+([A-Za-z_]\w*)\s+in\s+([A-Za-z_]\w*)\b/gu)) {
      if (variables.has(match[2]) && !variables.has(match[1])) {
        variables.add(match[1]);
        changed = true;
      }
    }
    for (const match of source.matchAll(
      /\bfor\s+(?:\(\s*)?([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)(?:\s*\))?\s+in\s+enumerate\s*\(\s*([A-Za-z_]\w*)\b/gu,
    )) {
      if (variables.has(match[3]) && !variables.has(match[2])) {
        variables.add(match[2]);
        changed = true;
      }
    }
    for (const match of source.matchAll(
      /\bfor\s+([A-Za-z_]\w*)\s+in\s+(?:list|tuple|iter)\s*\(\s*([A-Za-z_]\w*)\b/gu,
    )) {
      if (variables.has(match[2]) && !variables.has(match[1])) {
        variables.add(match[1]);
        changed = true;
      }
    }
    for (const match of source.matchAll(
      /\bfor\s+([A-Za-z_]\w*)\s+in\s+filter\s*\([^,\n]+,\s*([A-Za-z_]\w*)\b/gu,
    )) {
      if (variables.has(match[2]) && !variables.has(match[1])) {
        variables.add(match[1]);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return variables;
}

function optionalNumericFieldKeys(spec: PackageSpec): string[] {
  const numericType = /^(?:number|numeric|integer|int|float|double|decimal)(?:<[^>]+>)?$/iu;
  return [...new Set(spec.objects.flatMap((object) => object.fields
    .filter((field) => field.required === false && numericType.test((field.type ?? '').trim()))
    .map((field) => field.key)))]
    .filter(Boolean)
    .sort();
}

function numericHelperAllowsMissing(source: string, helperName: string): boolean {
  const escaped = helperName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(
    `^\\s*def\\s+${escaped}\\s*\\([^\\n]*\\):\\s*\\n([\\s\\S]*?)(?=^\\s*def\\s+[A-Za-z_]\\w*\\s*\\(|(?![\\s\\S]))`,
    'mu',
  ).exec(source);
  if (!match) return false;
  const body = match[1] ?? '';
  return /\bif\b[^:\n]{0,240}(?:\bis\s+None\b|\bin\s*\(\s*None\s*,\s*["']{2}\s*\)|==\s*["']{2})[^:\n]*:\s*(?:\n[ \t]+)?return\s+None\b/u.test(body);
}

function unsafeOptionalNumericFields(source: string, spec: PackageSpec): string[] {
  const unsafe = new Set<string>();
  for (const fieldKey of optionalNumericFieldKeys(spec)) {
    const escapedField = fieldKey.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const helperCalls = source.matchAll(
      new RegExp(
        `\\b((?:get|read|resolve|parse|extract|lookup)_(?:numeric|number)|numeric_(?:field|value))\\s*\\(\\s*[^,\\n]+,\\s*["']${escapedField}["']([^)]*)\\)`,
        'giu',
      ),
    );
    for (const call of helperCalls) {
      const helper = call[1];
      const trailingArgs = call[2] ?? '';
      const explicitlyOptional = /\b(?:allow_none|optional)\s*=\s*True\b|\brequired\s*=\s*False\b/u.test(trailingArgs);
      if (!explicitlyOptional && !numericHelperAllowsMissing(source, helper)) unsafe.add(fieldKey);
    }
    const directConversion = new RegExp(
      `\\b(?:float|int|Decimal|normalize_number|to_number|parse_number)\\s*\\(\\s*(?:[A-Za-z_]\\w*\\.get\\s*\\(\\s*["']${escapedField}["']|(?:first_non_empty|get_field|read_field|resolve_field|lookup_field)\\s*\\([^\\n)]*["']${escapedField}["'])`,
      'iu',
    );
    if (directConversion.test(source)) unsafe.add(fieldKey);
  }
  return [...unsafe].sort();
}

function mentionedOptionalFieldKeys(spec: PackageSpec, requirements: string): string[] {
  const normalizedRequirements = normalizedMention(requirements);
  return [...new Set(spec.objects.flatMap((object) => object.fields)
    .filter((field) => field.required === false)
    .filter((field) => [field.key, field.cnName, field.alias ?? '', field.sourceColumn ?? '']
      .filter((alias): alias is string => typeof alias === 'string' && alias.length > 0 && !/^\d+$/u.test(alias))
      .some((alias) => {
        const normalized = normalizedMention(alias);
        return normalized.length >= 3 && normalizedRequirements.includes(normalized);
      }))
    .map((field) => field.key))].sort();
}

function requiresComplementaryAssociationAggregation(requirements: string, fields: string[]): boolean {
  if (fields.length < 2) return false;
  const multipleRecords = /(?:互补|两条|多条|multiple\s+records?|complementary)/iu.test(requirements);
  const missingAssociations = /(?:只有|缺失|为空|无[A-Za-z_]|null|missing|only\s+has?)/iu.test(requirements);
  const retainAndAggregate = /(?:保留|求和|累计|汇总|聚合|retain|sum|aggregate)/iu.test(requirements);
  return multipleRecords && missingAssociations && retainAndAggregate;
}

function queryEntityByRecordVariable(source: string): Map<string, string> {
  const entities = new Map<string, string>();
  for (const match of source.matchAll(
    /^\s*([A-Za-z_]\w*)\s*=\s*[A-Za-z_]\w*\.query\s*\(\s*["']([^"']+)["']/gmu,
  )) entities.set(match[1], match[2]);
  for (let pass = 0; pass < 6; pass += 1) {
    let changed = false;
    for (const match of source.matchAll(
      /^\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)(?:\.data\b|\s*(?:or\s*\[\s*\])?\s*$)/gmu,
    )) {
      const entity = entities.get(match[2]);
      if (entity && entities.get(match[1]) !== entity) {
        entities.set(match[1], entity);
        changed = true;
      }
    }
    for (const match of source.matchAll(/\bfor\s+([A-Za-z_]\w*)\s+in\s+([A-Za-z_]\w*)\b/gu)) {
      const entity = entities.get(match[2]);
      if (entity && entities.get(match[1]) !== entity) {
        entities.set(match[1], entity);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return entities;
}

function fieldIsOptionalForEntity(spec: PackageSpec, entity: string, fieldKey: string): boolean | undefined {
  const normalizedEntity = normalizedMention(entity);
  const matchingFields = spec.objects
    .filter((object) => [
      object.key,
      object.title,
      object.sourceTable ?? '',
      ...(object.aliases ?? []),
      ...((object.sourceRefs ?? []).filter((ref) => ref.kind === 'table').map((ref) => ref.table)),
    ].some((alias) => alias && normalizedMention(alias) === normalizedEntity))
    .flatMap((object) => object.fields.filter((field) => field.key === fieldKey));
  if (!matchingFields.length) return undefined;
  return matchingFields.some((field) => field.required === false);
}

function singleOptionalAssociationPrefilters(
  source: string,
  fields: string[],
  spec: PackageSpec,
): string[] {
  const unsafe = new Set<string>();
  const recordEntities = queryEntityByRecordVariable(source);
  for (const field of fields) {
    const escapedField = field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const assignments = source.matchAll(new RegExp(
      `^\\s*([A-Za-z_]\\w*)\\s*=\\s*(?:([A-Za-z_]\\w*)\\s*\\.\\s*get\\s*\\(\\s*["']${escapedField}["']|(?:first_non_empty|get_field|read_field|resolve_field|lookup_field|get_row_value|pick_value)\\s*\\(\\s*([A-Za-z_]\\w*)[^\\n)]*["']${escapedField}["'])[^\\n]*$`,
      'gimu',
    ));
    for (const assignment of assignments) {
      const rawVariable = assignment[1];
      const recordVariable = assignment[2] ?? assignment[3];
      const recordEntity = recordEntities.get(recordVariable);
      if (recordEntity && fieldIsOptionalForEntity(spec, recordEntity, field) === false) continue;
      const start = (assignment.index ?? 0) + assignment[0].length;
      const segment = source.slice(start, start + 900);
      const escapedRaw = rawVariable.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const escapedRecord = recordVariable.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const guardedAppend = new RegExp(
        `\\bif\\s+${escapedRaw}\\s+is\\s+not\\s+None\\s*:\\s*[\\s\\S]{0,600}?\\.append\\s*\\(\\s*${escapedRecord}\\s*\\)`,
        'u',
      );
      if (guardedAppend.test(segment)) unsafe.add(field);
    }
  }
  return [...unsafe].sort();
}

function rawStructuredNumericVariables(source: string): string[] {
  const rawVariables = new Set<string>();
  const structuredVariables = structuredInputVariables(source);
  const assignments = source.matchAll(
    /^\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)(?:\[[^\]\n]+\])?\s*\.\s*get\s*\([^\n)]*\)\s*(?:#.*)?$|^\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)(?:\[[^\]\n]+\])+\s*(?:#.*)?$/gmu,
  );
  for (const match of assignments) {
    const name = match[1] ?? match[3];
    const sourceVariable = match[2] ?? match[4];
    if (!structuredVariables.has(sourceVariable)) continue;
    const start = (match.index ?? 0) + match[0].length;
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const nextAssignment = new RegExp(`\\b${escaped}\\s*=`, 'gu');
    nextAssignment.lastIndex = start;
    const next = nextAssignment.exec(source);
    const segment = source.slice(start, Math.min(next?.index ?? source.length, start + 1200));
    const numericUse = new RegExp(
      `(?:\\b${escaped}\\b\\s*(?:<=|>=|<|>|\\+|-|\\*|/|%)|(?:<=|>=|<|>|\\+|-|\\*|/|%)\\s*\\b${escaped}\\b)`,
      'u',
    );
    if (numericUse.test(segment)) rawVariables.add(name);
  }
  return [...rawVariables].sort();
}

function hasStableBusinessDeduplication(source: string): boolean {
  const executableSource = source
    .replace(/"""[\s\S]*?"""|'''[\s\S]*?'''/gu, '')
    .split('\n')
    .map((line) => {
      let quote = '';
      let escaped = false;
      for (let index = 0; index < line.length; index += 1) {
        const char = line[index];
        if (escaped) {
          escaped = false;
          continue;
        }
        if (char === '\\' && quote) {
          escaped = true;
          continue;
        }
        if ((char === '"' || char === "'") && (!quote || quote === char)) {
          quote = quote ? '' : char;
          continue;
        }
        if (char === '#' && !quote) return line.slice(0, index);
      }
      return line;
    })
    .join('\n');
  const namedCollection = /\b((?:seen|processed|dedup(?:licated)?|unique|merged|stable|business_key)[A-Za-z0-9_]*)\s*=\s*(set\s*\(\s*\)|\{[^\n]*\})/giu;
  for (const match of executableSource.matchAll(namedCollection)) {
    const name = match[1];
    const initializer = match[2] ?? '';
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (/\bfor\b.+\bin\b/u.test(initializer)) return true;
    const mutates = new RegExp(`(?:\\b${escaped}\\s*\\[[^\\]]+\\]\\s*=|\\b${escaped}\\.(?:add|setdefault)\\s*\\()`, 'u').test(executableSource);
    const consults = new RegExp(`(?:\\b(?:in|not\\s+in)\\s+${escaped}\\b|\\b${escaped}\\.(?:items|keys|values)\\s*\\()`, 'u').test(executableSource);
    if (mutates && consults) return true;
  }
  const recordIdentityAssignment = executableSource.matchAll(
    /\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*((?:record|row|source|primary|business)[A-Za-z0-9_]*(?:_id|_key))\s*\]\s*=/giu,
  );
  for (const match of recordIdentityAssignment) {
    const collection = match[1];
    const identity = match[2];
    const escapedCollection = collection.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const escapedIdentity = identity.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const initializesMapping = new RegExp(`\\b${escapedCollection}\\s*=\\s*\\{\\s*\\}`, 'u').test(executableSource);
    const checksIdentity = new RegExp(`\\b${escapedIdentity}\\s+(?:in|not\\s+in)\\s+${escapedCollection}\\b`, 'u').test(executableSource);
    const aggregatesValues = new RegExp(`\\b${escapedCollection}\\.(?:items|values)\\s*\\(`, 'u').test(executableSource);
    if (initializesMapping && checksIdentity && aggregatesValues) return true;
  }
  return false;
}

function hasPresenceOnlyClassification(source: string): boolean {
  return /\bif\s+(?=[^:\n]{0,240}\bis\s+not\s+None\b)(?=[^:\n]{0,240}\bor\b[^:\n]{0,160}\bis\s+not\s+None\b)[^:\n]+:\s*(?:\n[ \t]+)?[A-Za-z_][A-Za-z0-9_]*\.(?:add|append)\s*\(/u.test(source);
}

function hasHardCodedIdentifierClassification(source: string): boolean {
  const declarations = source.matchAll(
    /\b([A-Za-z_][A-Za-z0-9_]*(?:ids?|keys?|codes?|types?))\s*=\s*([\[{(][^\n\]} )]*(?:\s*,\s*[^\n\]} )]+)+\s*[\]})])/giu,
  );
  for (const match of declarations) {
    const name = match[1];
    const values = match[2] ?? '';
    const literalCount = [...values.matchAll(/(?:["'][^"']+["']|-?\d+(?:\.\d+)?)/gu)].length;
    if (literalCount < 2) continue;
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (new RegExp(`\\b(?:in|not\\s+in)\\s+${escaped}\\b`, 'u').test(source)) return true;
  }
  const inlineMemberships = source.matchAll(
    /\b([A-Za-z_][A-Za-z0-9_]*(?:name|group|category|classification|class|type|target|destination)[A-Za-z0-9_]*)\s+in\s*\(([^)]*)\)/giu,
  );
  for (const match of inlineMemberships) {
    const literalCount = [...(match[2] ?? '').matchAll(/["'][^"']+["']/gu)].length;
    if (literalCount >= 2) return true;
  }
  return false;
}

function normalizedMention(value: string): string {
  return value.normalize('NFKC').toLocaleLowerCase('zh-CN').replace(/[\p{White_Space}\p{Punctuation}\p{Symbol}]+/gu, '');
}

function allowedClassificationFieldAliases(spec: PackageSpec, requirements: string): Set<string> {
  const normalizedRequirements = normalizedMention(requirements);
  const allowed = new Set<string>();
  for (const field of spec.objects.flatMap((object) => object.fields)) {
    const aliases = [field.key, field.cnName, field.alias ?? '', field.sourceColumn ?? '']
      .filter((alias): alias is string => typeof alias === 'string' && alias.length > 0);
    const namedAliases = aliases.filter((alias) => !/^\d+$/u.test(alias));
    if (!namedAliases.some((alias) => {
      const normalized = normalizedMention(alias);
      return normalized.length >= 3 && normalizedRequirements.includes(normalized);
    })) continue;
    aliases.forEach((alias) => allowed.add(alias));
  }
  return allowed;
}

function classificationVariableOrigins(source: string): Map<string, Set<string>> {
  const origins = new Map<string, Set<string>>();
  const assignments = [...source.matchAll(/^\s*([A-Za-z_]\w*)\s*=\s*([^\n]+)$/gmu)]
    .map((match) => ({ variable: match[1], expression: match[2] ?? '' }));
  for (let pass = 0; pass < 6; pass += 1) {
    let changed = false;
    for (const { variable, expression } of assignments) {
      const next = new Set(origins.get(variable) ?? []);
      for (const match of expression.matchAll(/\.\s*get\s*\(\s*["']([^"']+)["']/gu)) {
        if (!/^\d+$/u.test(match[1])) next.add(match[1]);
      }
      for (const match of expression.matchAll(
        /\b(?:first_non_empty|get_field|read_field|resolve_field|lookup_field|get_row_value|pick_value)\s*\([^\n)]*\)/gu,
      )) {
        for (const literal of (match[0] ?? '').matchAll(/["']([^"']+)["']/gu)) {
          if (!/^\d+$/u.test(literal[1])) next.add(literal[1]);
        }
      }
      for (const reference of expression.matchAll(/\b([A-Za-z_]\w*)\b/gu)) {
        for (const field of origins.get(reference[1]) ?? []) next.add(field);
      }
      const previous = origins.get(variable) ?? new Set<string>();
      if (next.size !== previous.size || [...next].some((field) => !previous.has(field))) {
        origins.set(variable, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return origins;
}

function hasUnapprovedSubstringClassification(
  source: string,
  spec: PackageSpec,
  requirements: string,
): boolean {
  const allowedFields = allowedClassificationFieldAliases(spec, requirements);
  const origins = classificationVariableOrigins(source);
  const variables = new Set<string>();
  for (const match of source.matchAll(/["'][^"'\n]{2,}["']\s+in\s+(?:str\s*\(\s*)?([A-Za-z_]\w*)/giu)) {
    variables.add(match[1]);
  }
  for (const match of source.matchAll(
    /\b([A-Za-z_]\w*)\s*(?:\.\s*(?:lower|casefold|strip)\s*\(\s*\))?\s*\.\s*(?:find|startswith|endswith)\s*\(\s*["'][^"'\n]{2,}["']/giu,
  )) variables.add(match[1]);
  for (const variable of variables) {
    const fields = origins.get(variable) ?? new Set<string>();
    if (!fields.size || ![...fields].some((field) => allowedFields.has(field))) return true;
  }
  return false;
}

function hasConcreteStructuredCliHelp(source: string): boolean {
  const helpBlocks = source.matchAll(/\bhelp\s*=\s*([\s\S]{0,600}?)(?=\)\s*(?:\n|$)|\n\s*parser\.)/gu);
  for (const match of helpBlocks) {
    const block = match[1] ?? '';
    const structuredFragments = block.match(/(?:\[\s*)?\{[\s\S]*?\}(?:\s*\])?/gu) ?? [];
    for (const fragment of structuredFragments) {
      if (!/["'][A-Za-z_][A-Za-z0-9_]*["']\s*:/u.test(fragment)) continue;
      const withoutPlaceholders = fragment
        .replace(/<[^>\n]+>/gu, '')
        .replace(/\$\{[^}\n]+\}|\{\{[^}\n]+\}\}/gu, '');
      const concreteStringValue = /:\s*["'](?!\s*(?:example|sample|synthetic|示例|样例)\b)[^"'<>\n]{2,}["']/iu.test(withoutPlaceholders);
      const concreteNumericValue = /:\s*-?\d+(?:\.\d+)?\b/u.test(withoutPlaceholders);
      if (concreteStringValue || concreteNumericValue) return true;
    }
  }
  return false;
}

function requiresPercentageScaleNormalization(text: string): boolean {
  return /(?:(?:小数比例|比例值|ratio).{0,100}(?:百分数|百分比|percent)|(?:百分数|百分比|percent).{0,100}(?:小数比例|比例值|ratio)|(?:0?\.\d+).{0,120}(?:百分数|百分比|%|percent)|(?:百分数|百分比|%|percent).{0,120}(?:0?\.\d+))/isu.test(text);
}

function hasPercentageScaleNormalization(source: string): boolean {
  const detectsFractionScale = /(?:abs\s*\([^)]*\)|\b[A-Za-z_]\w*)\s*(?:<=|<)\s*1(?:\.0+)?\b/u.test(source);
  const scalesToPercent = /(?:\*\s*100(?:\.0+)?\b|\b100(?:\.0+)?\s*\*)/u.test(source);
  const detectsPercentScale = /(?:abs\s*\([^)]*\)|\b[A-Za-z_]\w*)\s*(?:>|>=)\s*1(?:\.0+)?\b/u.test(source);
  const scalesToRatio = /(?:\/\s*100(?:\.0+)?\b|\/=\s*100(?:\.0+)?\b)/u.test(source);
  return (detectsFractionScale && scalesToPercent) || (detectsPercentScale && scalesToRatio);
}

function directStructuredNumericExpressions(source: string): string[] {
  const expressions = new Set<string>();
  for (const variable of structuredInputVariables(source)) {
    const escaped = variable.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const expression = String.raw`\b(${escaped}(?:\s*\[\s*["'][^"']+["']\s*\])+)`;
    for (const match of source.matchAll(new RegExp(`${expression}\\s*(?:[+*/-]=?|[<>]=?)`, 'gu'))) {
      expressions.add((match[1] ?? '').replace(/\s+/gu, ''));
    }
    for (const match of source.matchAll(new RegExp(`(?:[+*/-]=?|[<>]=?)\\s*${expression}`, 'gu'))) {
      expressions.add((match[1] ?? '').replace(/\s+/gu, ''));
    }
  }
  return [...expressions].filter(Boolean).sort();
}

function hasEnumeratedRouteValidation(source: string): boolean {
  const checksMembership = /\b(?:route|routing|direction|target|mode)[A-Za-z0-9_]*\s+not\s+in\s+[A-Za-z0-9_\[\]{}(),.'"\s-]+:/iu.test(source);
  const rejectsInvalid = /(?:raise\s+(?:ValueError|RuntimeError)|unsupported\s+(?:route|routing|direction)|invalid\s+(?:route|routing|direction)|不支持的?(?:路由|方向)|非法(?:路由|方向))/iu.test(source);
  return checksMembership && rejectsInvalid;
}

function hasAllocationConservationCheck(source: string): boolean {
  const comparesTotals = /(?:math\.isclose\s*\(|abs\s*\([^\n)]*-[^\n)]*\)\s*[><=!]=?)/iu.test(source);
  const blocksMismatch = /(?:raise\s+(?:ValueError|RuntimeError)|conservation|守恒|总量不一致|allocation\s+failed)/iu.test(source);
  return comparesTotals && blocksMismatch;
}

function validateOptimizationScriptRequirements(
  spec: PackageSpec,
  revised: RevisedSpec,
  adopted: OptSuggestion[],
): void {
  const errors: string[] = [];
  const scriptSuggestions = adopted.filter((suggestion) => kindFromSuggestion(suggestion.kind) === 'script');
  const requirementsByTarget = new Map<string, string>();
  for (const target of new Set(scriptSuggestions.map((suggestion) => suggestion.target).filter(Boolean) as string[])) {
    const script = revised.scripts.find((candidate) => candidate.key === target)
      ?? (spec.scripts ?? []).find((candidate) => candidate.key === target);
    const aliases = [target, script?.title ?? '', script?.entrypoint ?? '']
      .map(normalizedMention)
      .filter((alias) => alias.length >= 4);
    const related = adopted.filter((suggestion) => {
      if (kindFromSuggestion(suggestion.kind) === 'script' && suggestion.target === target) return true;
      const text = normalizedMention(suggestionRequirementText(suggestion));
      return aliases.some((alias) => text.includes(alias));
    });
    requirementsByTarget.set(target, related.map(suggestionRequirementText).join('\n'));
  }
  for (const suggestion of scriptSuggestions) {
    if (!suggestion.target) continue;
    const before = (spec.scripts ?? []).find((script) => script.key === suggestion.target);
    const script = revised.scripts.find((candidate) => candidate.key === suggestion.target);
    if (!script || (before && JSON.stringify(before) === JSON.stringify(script))) continue;
    if (before) {
      const identityChanges = [
        script.runtime !== before.runtime ? `运行时必须保持 ${before.runtime}` : '',
        script.boundOperation !== before.boundOperation ? `绑定操作必须保持 ${before.boundOperation}` : '',
        script.entrypoint !== before.entrypoint ? `入口文件名必须保持 ${before.entrypoint}` : '',
      ].filter(Boolean);
      if (identityChanges.length) {
        errors.push(`优化候选 script:${script.key} 不得改变既有脚本身份：${identityChanges.join('、')}`);
      }
    }
    if (!script.sourceCode.trim()) {
      errors.push(`优化候选 script:${script.key} 的源码不能为空；修改既有脚本时必须保留可执行实现`);
      continue;
    }
    const requirements = requirementsByTarget.get(suggestion.target) ?? suggestionRequirementText(suggestion);
    const scriptContract = [
      script.description,
      ...script.parameters.flatMap((parameter) => [parameter.name, parameter.description ?? '']),
      ...script.boundaries,
    ].join('\n').normalize('NFKC').toLocaleLowerCase('zh-CN');
    const source = script.sourceCode.normalize('NFKC');
    if (/\bDataProviderClient\b/u.test(source) && hasPositionalBeforeSemanticFallback(source)) {
      errors.push(`优化候选 script:${script.key} 的数据行候选键顺序不正确：必须优先读取语义字段和来源字段，空值时才回退到位置索引`);
    }
    const providerRowFallbackRequired = /(?:(?:命名键|命名字段|语义字段|ontology[-\s_]*named?\s*fields?).{0,180}(?:空字符串|为空|empty).{0,180}(?:索引|来源列|source\s*column|index)|(?:索引|来源列|source\s*column|index).{0,180}(?:命名键|命名字段|语义字段|ontology[-\s_]*named?\s*fields?).{0,180}(?:空字符串|为空|empty))/isu.test(requirements);
    if (providerRowFallbackRequired && !hasNonEmptyRowFallback(source)) {
      errors.push(`优化候选 script:${script.key} 未落实数据行空值回退：必须按候选键顺序跳过 null/空字符串后再取来源列值`);
    }
    if (providerRowFallbackRequired && hasReversedProviderFieldPrecedence(source, spec)) {
      errors.push(`优化候选 script:${script.key} 的数据行候选键顺序错误：必须优先读取本体语义字段，再回退到来源列字段`);
    }
    const numericNormalizationRequired = /(?:字符串.{0,80}(?:数字|数值)|(?:数字|数值).{0,80}字符串|mixed.{0,40}(?:number|string)|(?:number|string).{0,40}mixed|结构化输入.{0,120}(?:数值|数字).{0,120}(?:转换|类型)|(?:数值|数字).{0,120}(?:转换|类型).{0,120}结构化输入)/isu.test(`${requirements}\n${scriptContract}`);
    if (numericNormalizationRequired && !hasExplicitNumericNormalization(source)) {
      errors.push(`优化候选 script:${script.key} 未落实数值类型归一：Provider 行值参与比较或计算前必须显式转换并处理缺失/非法值`);
    }
    const rawNumericVariables = numericNormalizationRequired ? rawStructuredNumericVariables(source) : [];
    if (rawNumericVariables.length) {
      errors.push(`优化候选 script:${script.key} 仍将结构化输入变量 ${rawNumericVariables.join('、')} 的原始值直接参与数值比较或计算；请分别先显式转换，再使用转换后的变量`);
    } else if (numericNormalizationRequired) {
      const directNumericExpressions = directStructuredNumericExpressions(source);
      if (directNumericExpressions.length) {
        errors.push(`优化候选 script:${script.key} 仍将结构化输入的下标表达式 ${directNumericExpressions.join('、')} 直接参与数值比较或计算；请先赋值并显式转换，再使用转换后的变量`);
      }
    }
    if (/\b(?:get_field|read_field|resolve_field|lookup_field|first_non_empty)\s*\([^\n]*\)\s*==\s*-?\d/iu.test(source)) {
      errors.push(`优化候选 script:${script.key} 仍将未归一的数据行值直接与数字比较`);
    }
    const optionalNumericSafetyRequired = /(?:(?:可选|optional).{0,100}(?:数值|数字|number|numeric).{0,120}(?:判空|非空|缺失|为空|转换)|(?:判空|非空|缺失|为空).{0,120}(?:可选|optional).{0,100}(?:数值|数字|number|numeric))/isu.test(`${requirements}\n${scriptContract}`);
    if (optionalNumericSafetyRequired) {
      const unsafeFields = unsafeOptionalNumericFields(source, spec);
      if (unsafeFields.length) {
        errors.push(`优化候选 script:${script.key} 将可选数值字段 ${unsafeFields.join('、')} 直接交给必填数值转换；必须先读取并判断非空，再按需要转换`);
      }
    }
    const complementaryAssociationFields = mentionedOptionalFieldKeys(spec, requirements);
    if (requiresComplementaryAssociationAggregation(requirements, complementaryAssociationFields)) {
      const prefilteredFields = singleOptionalAssociationPrefilters(source, complementaryAssociationFields, spec);
      if (prefilteredFields.length) {
        errors.push(`优化候选 script:${script.key} 在汇总互补关联记录前按单一可选字段 ${prefilteredFields.join('、')} 预过滤，会丢失只含其他关联的合法记录；必须先按稳定记录身份保留并汇总，再进行分类或路由`);
      }
    }
    const deduplicationRequired = /(?:(?:重复|两(?:个|条).{0,50}记录|two.{0,50}records?|duplicate).{0,220}(?:去重|互斥|单一|一次|deduplicat|exclusive|single|double)|(?:去重|deduplicat).{0,220}(?:互斥|单一|一次|exclusive|single)|(?:稳定业务键|业务主键|记录标识|record\s+id|primary\s+key).{0,220}(?:去重|合并|覆盖|聚合|分类|目标|去向)|(?:去重|合并|覆盖|聚合).{0,220}(?:稳定业务键|业务主键|记录标识|record\s+id|primary\s+key)|(?:两(?:个|条).{0,40}记录|双记录|多条记录|multiple\s+records?).{0,220}(?:之和|合计|累计|汇总|聚合|求和|sum|aggregate)|(?:之和|合计|累计|汇总|聚合|求和|sum|aggregate).{0,220}(?:两(?:个|条).{0,40}记录|双记录|多条记录|multiple\s+records?))/isu.test(requirements);
    if (deduplicationRequired && !hasStableBusinessDeduplication(source)) {
      errors.push(`优化候选 script:${script.key} 未按稳定来源记录身份保留并汇总多条记录：不得用目标对象或分类键覆盖不同记录，重复物理记录只计一次后再聚合或互斥分配`);
    }
    const routeValidationRequired = /(?:(?:路由|方向|route|routing|direction).{0,120}(?:非法|不支持|允许值|白名单|明确拒绝|只能|枚举|one\s+of|invalid|unsupported|allowlist|\bor\b)|(?:非法|不支持|允许值|白名单|明确拒绝|只能|枚举|one\s+of|invalid|unsupported|allowlist).{0,120}(?:路由|方向|route|routing|direction))/isu.test(`${requirements}\n${scriptContract}`);
    if (routeValidationRequired && !hasEnumeratedRouteValidation(source)) {
      errors.push(`优化候选 script:${script.key} 未校验路由输入：必须枚举允许值并明确拒绝未知路由`);
    }
    const conservationRequired = /(?:总量守恒|总量只计一次|conservation|mass\s+balance)/iu.test(requirements);
    if (conservationRequired && !hasAllocationConservationCheck(source)) {
      errors.push(`优化候选 script:${script.key} 未落实总量守恒校验：互斥分配后必须核对输入逻辑总量与输出分配总量`);
    }
    const explicitClassificationRequired = /(?:(?:特殊类别|特殊流向|成员资格|classification\s+membership).{0,180}(?:明确|显式|数据字段|可验证|关联字段非空)|(?:明确|显式|数据字段|可验证|关联字段非空).{0,180}(?:特殊类别|特殊流向|成员资格|classification\s+membership))/isu.test(requirements)
      || /(?:(?:分类|路由|classification|routing).{0,180}(?:运行时来源|来源记录|来源字段|source\s+(?:record|field)).{0,180}(?:不能|不得|禁止|must\s+not).{0,120}(?:名称|目标对象|name|target\s+object)|(?:不能|不得|禁止|must\s+not).{0,120}(?:名称|目标对象|name|target\s+object).{0,180}(?:分类|路由|classification|routing))/isu.test(requirements);
    if (explicitClassificationRequired) {
      const unsafeClassificationReasons = [
        hasPresenceOnlyClassification(source) ? '仅根据关联字段非空' : '',
        hasHardCodedIdentifierClassification(source) ? '使用硬编码标识集合' : '',
        hasUnapprovedSubstringClassification(source, spec, requirements) ? '使用未在建议中确认的名称字段子串' : '',
      ].filter(Boolean);
      if (unsafeClassificationReasons.length) {
        errors.push(`优化候选 script:${script.key} ${unsafeClassificationReasons.join('、')}推断特殊类别或流向；成员资格必须来自运行时显式分类字段、建议明确允许的来源记录字段或可验证配置`);
      }
    }
    const placeholderExamplesRequired = /(?:(?:命令行|cli|帮助|示例|样例).{0,180}(?:占位|合成|不得固化|不能固化)|(?:占位|合成|不得固化|不能固化).{0,180}(?:命令行|cli|帮助|示例|样例))/isu.test(requirements);
    if (placeholderExamplesRequired && hasConcreteStructuredCliHelp(source)) {
      errors.push(`优化候选 script:${script.key} 的命令行帮助仍包含具体结构化业务值；只允许字段形状、符号占位或明确独立的合成样例`);
    }
    if (requiresPercentageScaleNormalization(`${requirements}\n${scriptContract}`)
      && !hasPercentageScaleNormalization(source)) {
      errors.push(`优化候选 script:${script.key} 未统一比例与百分数量纲：输入可能是小数比例或百分数时，必须先归一到同一量纲再比较或计算`);
    }
  }
  if (errors.length) throw new Error([...new Set(errors)].join('；'));
}

function ruleConsistencyText(rule: NonNullable<PackageSpec['rules']>[number]): string {
  return [
    rule.description ?? '',
    rule.content,
    ...(rule.conditions ?? []).flatMap((condition) => [condition.condition, condition.conclusion]),
    ...(rule.exceptions ?? []),
  ].join('\n').normalize('NFKC');
}

function contradictoryRuleThresholds(text: string): string[] {
  const negatedPattern = /(?:而非|不是|并非|不应|不得|not\s+)[^。；;\n]{0,28}?([<>]=?|≤|≥)\s*(\d+(?:\.\d+)?)(%?)/giu;
  const negated = [...text.matchAll(negatedPattern)].map((match) => ({
    operator: match[1], value: match[2], unit: match[3] ?? '',
  }));
  if (!negated.length) return [];
  const positiveText = text.replace(negatedPattern, '');
  return [...new Set(negated.flatMap(({ operator, value, unit }) => {
    const comparison = `${operator.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*${value.replace('.', '\\.')}%?`;
    const conflicts = new RegExp(`${comparison}[^。；;\\n]{0,32}(?:过载|满足|通过|判定|触发)`, 'iu').test(positiveText)
      || new RegExp(`(?:过载|满足|通过|判定|触发)[^。；;\\n]{0,32}${comparison}`, 'iu').test(positiveText);
    return conflicts ? [`${operator} ${value}${unit}`] : [];
  }))];
}

function hasPositionOnlyDataAccessRule(text: string): boolean {
  return /(?:(?:必须|只能|一律|仅).{0,36}(?:位置索引|数字索引|位置键).{0,80}(?:不得|禁止|不能).{0,28}(?:命名键|语义字段)|(?:不得|禁止|不能).{0,28}(?:命名键|语义字段).{0,80}(?:必须|只能|一律|仅).{0,36}(?:位置索引|数字索引|位置键))/isu.test(text);
}

function hasStaticMappingAutoAdaptationConflict(text: string): boolean {
  const arrowEntries = [...text.matchAll(/(?:→|=>|->)/gu)].length;
  const equalsEntries = [...text.matchAll(
    /(?:^|[：:,，；;、\n])\s*[\p{L}\p{N}_-]+\s*=\s*[\p{L}\p{N}_-]+(?=\s*(?:[，,；;、\n]|$))/gu,
  )].length;
  const mappingEntries = arrowEntries + equalsEntries;
  const claimsAutomaticAdaptation = /(?:自动适配|无需修改(?:规则|代码)|只需更新.{0,24}(?:表|数据)|随.{0,24}(?:表|数据).{0,12}自动)/isu.test(text);
  const explicitlyExamples = /(?:示例|样例|仅供说明|illustrative\s+example)/iu.test(text);
  return mappingEntries >= 2 && claimsAutomaticAdaptation && !explicitlyExamples;
}

function hasClosedWorldDynamicMappingConflict(text: string): boolean {
  const arrowEntries = [...text.matchAll(/(?:→|=>|->)/gu)].length;
  const equalsEntries = [...text.matchAll(
    /(?:^|[：:,，；;、\n])\s*[\p{L}\p{N}_-]+\s*=\s*[\p{L}\p{N}_-]+(?=\s*(?:[，,；;、\n]|$))/gu,
  )].length;
  const dynamicLookup = /(?:(?:运行时|动态|查询|读取).{0,60}(?:表|数据源|字典)|(?:表|数据源|字典).{0,60}(?:查询|读取|获得))/isu.test(text);
  const authoritativeList = /(?:已知|当前|上述|以下).{0,16}映射/iu.test(text);
  const rejectsOutsideList = /(?:不在|未出现在|未包含在).{0,32}(?:上述|以下|当前|已知)?.{0,16}映射.{0,60}(?:错误|拒绝|返回|不得|禁止)/isu.test(text);
  const explicitlyExamples = /(?:示例|样例|仅供说明|illustrative\s+example)/iu.test(text);
  return arrowEntries + equalsEntries >= 2
    && dynamicLookup && authoritativeList && rejectsOutsideList && !explicitlyExamples;
}

function hasAmbiguousMatchingStrategy(text: string): boolean {
  return /(?:模糊匹配|fuzzy\s+match(?:ing)?)\s*(?:或|或者|\/|、|or)\s*(?:精确匹配|exact\s+match(?:ing)?)|(?:精确匹配|exact\s+match(?:ing)?)\s*(?:或|或者|\/|、|or)\s*(?:模糊匹配|fuzzy\s+match(?:ing)?)/iu.test(text);
}

function validateOptimizationRuleConsistency(spec: PackageSpec, revised: RevisedSpec): void {
  const errors: string[] = [];
  const beforeByKey = new Map((spec.rules ?? []).map((rule) => [rule.key, rule]));
  for (const rule of revised.rules) {
    const before = beforeByKey.get(rule.key);
    if (before && JSON.stringify(before) === JSON.stringify(rule)) continue;
    const text = ruleConsistencyText(rule);
    const thresholdConflicts = contradictoryRuleThresholds(text);
    if (thresholdConflicts.length) {
      errors.push(`优化候选 rule:${rule.key} 的阈值判定互相矛盾：冲突比较 ${thresholdConflicts.join('、')} 同时以肯定和否定口径出现；请删除被废弃阈值的否定对照，只保留当前权威公式、阈值和异常处理`);
    }
    if (hasPositionOnlyDataAccessRule(text)) {
      errors.push(`优化候选 rule:${rule.key} 违反数据契约：必须优先读取语义字段，仅在缺失时回退来源列字段`);
    }
    if (hasStaticMappingAutoAdaptationConflict(text)) {
      errors.push(`优化候选 rule:${rule.key} 同时硬编码映射记录并宣称自动适配；请改为运行时查询数据源或明确其为非权威示例`);
    }
    if (hasClosedWorldDynamicMappingConflict(text)) {
      errors.push(`优化候选 rule:${rule.key} 用当前静态映射名单封闭了运行时查表结果；数据源必须是唯一事实来源，静态条目只能作为非权威示例且不得排除新增记录`);
    }
    if (hasAmbiguousMatchingStrategy(text)) {
      errors.push(`优化候选 rule:${rule.key} 保留了“模糊匹配或精确匹配”的未决策略；必须选择单一、可验证的权威匹配口径，并与执行脚本保持一致`);
    }
  }
  if (errors.length) throw new Error([...new Set(errors)].join('；'));
}

export async function applyOptimizations(
  project: Project,
  spec: PackageSpec,
  adopted: OptSuggestion[],
  llmOverride?: LlmDeps,
  onProgress?: (event: OptimizationApplyProgressEvent) => void | Promise<void>,
  failedResults: EvalResult[] = [],
  diagnoses: OptimizationDiagnosis[] = [],
): Promise<{
  objects: PackageSpec['objects']; operations: PackageSpec['operations'];
  rules: NonNullable<PackageSpec['rules']>; metrics: NonNullable<PackageSpec['metrics']>;
  scripts: NonNullable<PackageSpec['scripts']>;
}> {
  adopted = normalizeOptimizationSuggestionTargets(adopted, spec) as OptSuggestion[];
  validateScriptOptimizationDecisions(adopted, diagnoses, spec);
  const cap: { revised?: RevisedSpec } = {};
  const originalPatchBase: RevisedSpec = {
    objects: spec.objects,
    operations: spec.operations,
    rules: spec.rules ?? [],
    metrics: spec.metrics ?? [],
    scripts: spec.scripts ?? [],
  };
  let patchBase = originalPatchBase;
  const mergeResourcePatch = <T extends { key: string }>(base: T[], changes: T[]): T[] => {
    const changedByKey = new Map(changes.map((item) => [item.key, item]));
    const baseKeys = new Set(base.map((item) => item.key));
    return [
      ...base.map((item) => changedByKey.get(item.key) ?? item),
      ...changes.filter((item) => !baseKeys.has(item.key)),
    ];
  };
  const submitPatch = {
    name: 'submit_optimization_patch',
    label: '提交知识资源补丁',
    description: '只提交本轮实际修改或新增的资源；未涉及资源无需重复，服务端会与当前完整规格合并后执行全部校验',
    parameters: Type.Object({
      objects: Type.Optional(Type.Array(specObjectSchema())),
      operations: Type.Optional(Type.Array(specOperationSchema())),
      rules: Type.Optional(Type.Array(specRuleSchema())),
      metrics: Type.Optional(Type.Array(specMetricSchema())),
      scripts: Type.Optional(Type.Array(specScriptSchema())),
    }),
    async execute(_id: string, params: any) {
      cap.revised = {
        objects: mergeResourcePatch(patchBase.objects, params.objects ?? []),
        operations: mergeResourcePatch(patchBase.operations, params.operations ?? []),
        rules: mergeResourcePatch(patchBase.rules, params.rules ?? []),
        metrics: mergeResourcePatch(patchBase.metrics, params.metrics ?? []),
        scripts: mergeResourcePatch(patchBase.scripts, params.scripts ?? []),
      };
      return { content: [{ type: 'text' as const, text: '知识资源补丁已提交并合并。' }], details: {}, terminate: true };
    },
  };
  const submitRevised = {
    name: 'submit_revised_spec',
    label: '提交修订后的规格',
    description: '兼容工具：提交完整知识包规格。通常应使用 submit_optimization_patch 只提交变更资源，避免重复未修改内容',
    // 与生成阶段共用 schema，修订时不丢新字段。
    parameters: Type.Object({
      objects: Type.Array(specObjectSchema()),
      operations: Type.Array(specOperationSchema()),
      rules: Type.Array(specRuleSchema()),
      metrics: Type.Array(specMetricSchema()),
      scripts: Type.Array(specScriptSchema()),
    }),
    async execute(_id: string, params: any) {
      cap.revised = {
        objects: params.objects ?? [], operations: params.operations ?? [],
        rules: params.rules ?? [], metrics: params.metrics ?? [],
        scripts: params.scripts ?? [],
      };
      return { content: [{ type: 'text' as const, text: '修订规格已提交。' }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包优化器。请把专家采纳的优化建议应用到当前知识包规格上，产出修订后的完整规格。',
    '要求：① 保留原有对象、操作、规则、指标和脚本（除非建议要求修改）；② 每条建议都要落实；',
    '③ add_rule/modify_rule 落到 rules，add_metric/modify_metric 落到 metrics，不得改写成一段 operation 描述；',
    '④ add_script/modify_script 必须落到 scripts，且目标 operation.script.key 必须同步绑定；脚本只接受结构化输入，不得内置用例答案常量。',
    '绑定脚本的 operation 只有一条权威计算路径：脚本失败时停止并报告阻断；禁止新增模型/人工手算、business_data_search、ontology_context 或其他替代计算兜底。',
    '修改既有脚本时必须保留原 key、runtime、boundOperation、entrypoint 和非空 sourceCode；除非建议明确要求迁移入口，否则只能在原入口文件内修复，禁止通过改名或空实现删除既有脚本文件。',
    '脚本必须提供 argparse CLI、按 parameters 声明全部 flag，并向 stdout 输出 JSON。需要查询本体数据源时，',
    '只能 from shared_capabilities.ontology_script_sdk import DataProviderClient 后调用 query/query_sql；禁止定义 query_table 等待运行时注入的占位函数，禁止 NotImplementedError。',
    DATA_PROVIDER_SCRIPT_CONTRACT,
    'DataProvider 行适配必须由当前对象的 sourceRefs/fields 契约驱动：按语义键、来源列键等候选顺序读取，跳过 null 和空字符串；数值字段必须显式转换并对缺失或非法值给出清晰错误，禁止直接把原始字符串参与数值比较或运算。',
    '从 CLI JSON、字典 .get(...) 或下标读取的每个数值字段，也必须先显式转换成数值变量，再进行大小比较、加减乘除或聚合；不能因为脚本中其他字段已经调用 float/int 就跳过当前字段。',
    '推荐逐字段采用 raw_value = record.get("field")；value = normalize_number(raw_value, "field") 的两段式写法。校验错误列出的每一个原始变量都必须分别修复，不能只增加一个未覆盖该变量的通用转换函数。',
    '推荐统一使用以下等价 helper（可以重命名，但不得省略空值判断）：def first_non_empty(row, *candidate_keys): 对候选键依次 value = row.get(key)；若 value in (None, "") 则 continue；否则 return value；全部为空时 return None。',
    '候选键的调用顺序必须明确写成“语义字段、来源列字段、最后才是纯数字位置索引”；任何纯数字字符串键都不得出现在命名键之前。',
    '若多个物理记录代表同一逻辑贡献，必须先按稳定业务键去重，再做聚合或互斥分配；互斥路由必须保证每个逻辑贡献恰好计入一次，并校验总量守恒。',
    '稳定业务键应优先使用被查询资源声明的主键或记录标识；不得仅因分类、目标或去向相同就合并不同业务记录。',
    '若多条独立来源记录需要求和，必须先按来源记录主键保留每条唯一记录，再聚合其数值；禁止用目标对象、分类或去向作为字典键覆盖前一条来源记录。',
    '特殊类别或特殊流向的成员资格必须来自运行时显式分类字段、来源记录或可验证配置；不得仅凭任意关联字段非空推断，也不得在源码中硬编码标识集合。',
    '动态映射必须以运行时数据源为唯一事实来源；当前数据记录只能作为非权威示例，禁止把静态枚举写成封闭名单并据此排除未来新增记录。',
    '比例与百分比字段在比较或计算前必须统一量纲；当输入可能使用小数比例或百分数两种表示时，必须显式归一后再使用。',
    '可以统一到 0-1 小数比例，也可以统一到 0-100 百分数，但必须先解析数值并校验范围，再按输入量级分支转换：例如大于 1 的百分数除以 100，或不大于 1 的小数比例乘以 100；禁止无条件除以或乘以 100。',
    '修正规则公式或阈值时，description、content、conditions、exceptions 只能保留当前权威口径；禁止把被废弃公式或阈值继续作为“而非/不是”历史对照写入规则。',
    '声明为枚举值的路由或方向参数必须先校验允许值并明确拒绝未知值，禁止让未知值静默进入 else 默认分支。',
    '结构化输入中的每个数值都必须在使用点显式转换或严格校验类型；其他字段的数值转换不能替代当前字段的归一。',
    '源码、参数说明和知识文件中的示例只能使用符号占位或独立合成值，不得沿用真实评测问题中的批次、日期、金额、比率或结果数值。',
    '⑤ 新增操作时在 competencyQuestion 里填 relatedQuestion 原文；⑥ 候选曾被人工退回时必须落实 candidateFeedback；',
    '不得把标准答案、评测问题原文、模型回答、判分理由或用例专用数值写入任何资源；证据只允许资料、流程确认、脚本契约和 diagnosis:dg_* 稳定引用。',
    '⑦ 必须优先用 submit_optimization_patch，只提交本轮实际修改或新增的资源；服务端会保留未涉及资源并执行完整校验。不要在补丁里重复未修改资源。',
    '只有无法用资源补丁表达时才使用 submit_revised_spec 提交五类资源的全量规格。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  // 规格 JSON 不截断：截断会让模型（或 faux 解析）看不到尾部对象/操作，
  // 提交的「全量规格」静默缺内容并被覆盖式入库
  const promptSpec = sanitizeOptimizationPromptSpec(spec, failedResults);
  const promptFieldContract = dataProviderFieldOrderContract(promptSpec);
  const promptSuggestions = adopted.map((s) => ({
    title: safeOptimizationSuggestionValue(s.title), detail: safeOptimizationSuggestionValue(s.detail),
    kind: s.kind, target: s.target, relatedQuestion: safeOptimizationSuggestionValue(s.relatedQuestion ?? ''),
    diagnosisIds: s.diagnosisIds ?? [],
    generalizationPlan: safeOptimizationSuggestionValue(s.generalizationPlan ?? ''),
    validationPlan: safeOptimizationSuggestionValue(s.validationPlan ?? ''),
    candidateFeedback: safeOptimizationSuggestionValue(s.candidateFeedback ?? ''),
  }));
  const body = [
    '【当前规格】',
    JSON.stringify({
      objects: promptSpec.objects, operations: promptSpec.operations,
      rules: promptSpec.rules ?? [], metrics: promptSpec.metrics ?? [], scripts: promptSpec.scripts ?? [],
    }),
    promptFieldContract,
    '【采纳的建议】',
    JSON.stringify(promptSuggestions),
    '请应用以上建议，并优先通过 submit_optimization_patch 只提交实际变更或新增的资源。',
  ].join('\n');

  let validationRevisionForRetry: RevisedSpec | undefined;
  const validateRevision = (revised: RevisedSpec | undefined): RevisedSpec => {
    if (!revised || (!revised.objects.length && !revised.operations.length
      && !revised.rules.length && !revised.metrics.length && !revised.scripts.length)) {
      throw new Error('优化器未产出修订规格');
    }
    const protectedRevision = completeDeclaredScriptBindings(
      spec,
      preserveUntouchedResources(spec, revised, adopted),
      adopted,
    );
    validationRevisionForRetry = protectedRevision;
    // 不在这里用“整包 JSON 相同”笼统报错。后面的
    // validateOptimizationChanges 会按建议 kind + target 给出精确的未落实资源，
    // 使第二次修订和专家决策都能拿到可操作的失败原因。
    // 优化建议只增改不删：任一资源数量变少都视为模型丢内容。
    if (protectedRevision.objects.length < spec.objects.length
      || protectedRevision.operations.length < spec.operations.length
      || protectedRevision.rules.length < (spec.rules?.length ?? 0)
      || protectedRevision.metrics.length < (spec.metrics?.length ?? 0)
      || protectedRevision.scripts.length < (spec.scripts?.length ?? 0)) {
      throw new Error(`修订规格丢失既有内容（对象 ${spec.objects.length}→${protectedRevision.objects.length}，操作 ${spec.operations.length}→${protectedRevision.operations.length}，规则 ${spec.rules?.length ?? 0}→${protectedRevision.rules.length}，指标 ${spec.metrics?.length ?? 0}→${protectedRevision.metrics.length}，脚本 ${spec.scripts?.length ?? 0}→${protectedRevision.scripts.length}）`);
    }
    const occupiedTargets = new Set<string>();
    const evidenceChanges = adopted.flatMap((suggestion) => {
      let kind = kindFromSuggestion(suggestion.kind);
      if (suggestion.kind === 'clarify_description' && suggestion.target) {
        kind = (['object', 'operation', 'rule', 'metric', 'script'] as ResourceKind[])
          .find((candidate) => resourceItems(protectedRevision, candidate).some((item) => item.key === suggestion.target));
      }
      if (!kind) return [];
      let targetKey = suggestion.target;
      if (!targetKey && suggestion.kind.startsWith('add_')) {
        targetKey = resourceItems(protectedRevision, kind)
          .map((item) => item.key)
          .find((key) => !resourceItems(spec, kind).some((item) => item.key === key)
            && !occupiedTargets.has(`${kind}:${key}`));
      }
      if (!targetKey) return [];
      occupiedTargets.add(`${kind}:${targetKey}`);
      return [{
        targetKind: kind,
        targetKey,
        evidence: [
          ...(suggestion.diagnosisIds ?? [])
            .map((id) => id.trim())
            .filter((id) => STABLE_DIAGNOSIS_ID.test(id))
            .map((id) => `diagnosis:${id}`),
          `optimization-decision:${safeOptimizationSuggestionValue(suggestion.title)}`,
        ],
      }];
    });
    const withEvidence = attachKnowledgeChangeEvidence(
      { ...spec, ...protectedRevision },
      evidenceChanges,
    );
    // 增量补丁会保留未触及资源；若旧版本已经把评测样例写进知识包，不能让它
    // 因“未修改”而绕过候选门禁。统一在服务端清理继承的裁判证据与样例数值，
    // 再对完整候选做校验，避免要求模型看到或复述这些敏感内容。
    const rawCandidate: RevisedSpec = {
      objects: withEvidence.objects,
      operations: withEvidence.operations,
      rules: withEvidence.rules ?? [],
      metrics: withEvidence.metrics ?? [],
      scripts: withEvidence.scripts ?? [],
    };
    // 服务端会补齐脚本绑定、保护未触及资源并附加证据。后续校验若失败，
    // 重试上下文必须携带这份实际被校验的候选，而不是模型最初提交的原始补丁；
    // 否则模型看不到由服务端补齐后触发错误的 operation，无法做定点修复。
    validationRevisionForRetry = rawCandidate;
    // 先对模型原始提交做门禁和建议落实校验，确保新引入的评测样例仍会被拒绝；
    // 随后才清理旧版本中继承的敏感证据，避免自动清理被误判成模型越权修改。
    const validationErrors = [
      () => validateOptimizationRevisionAgainstEvaluationData(spec, rawCandidate, failedResults),
      () => validateOptimizationScriptRequirements(spec, rawCandidate, adopted),
      () => validateOptimizationRuleConsistency(spec, rawCandidate),
      // 建议是否真正落实，必须比较模型提交且经资源保护后的候选。
      // 服务端后加的 diagnosis/optimization-decision 证据不能被误算成业务内容修改。
      () => validateOptimizationChanges(spec, protectedRevision, adopted),
      () => validateAuthoritativeScriptExecutionPolicy(spec, rawCandidate),
    ].flatMap((validate) => {
      try {
        validate();
        return [];
      } catch (error) {
        return [error instanceof Error ? error.message : String(error)];
      }
    });
    if (validationErrors.length) {
      throw new Error([...new Set(validationErrors)].join('；'));
    }
    const sanitizedCandidate = sanitizeOptimizationPromptSpec({ ...spec, ...rawCandidate }, failedResults);
    const evidenced: RevisedSpec = {
      objects: sanitizedCandidate.objects,
      operations: sanitizedCandidate.operations,
      rules: sanitizedCandidate.rules ?? [],
      metrics: sanitizedCandidate.metrics ?? [],
      scripts: sanitizedCandidate.scripts ?? [],
    };
    validationRevisionForRetry = evidenced;
    validateSpecIntegrity({ ...spec, ...evidenced });
    return evidenced;
  };

  const llm = llmOverride ?? buildTaskLlm({ maxTokensCap: OPTIMIZATION_MAX_OUTPUT_TOKENS });
  let lastValidationError = '';
  let lastRejectedRevision: RevisedSpec | undefined;
  const validationErrors: string[] = [];
  for (let attempt = 1; attempt <= OPTIMIZATION_MAX_ATTEMPTS; attempt += 1) {
    cap.revised = undefined;
    validationRevisionForRetry = undefined;
    const canReuseRejectedRevision = attempt > 1
      && Boolean(lastRejectedRevision)
      && rejectedPatchCanReturnToModel(lastValidationError);
    const previousPatch = canReuseRejectedRevision
      ? rejectedPatchForPrompt(spec, lastRejectedRevision, failedResults)
      : undefined;
    if (canReuseRejectedRevision && lastRejectedRevision) {
      const retryBaseRevision = stripUnplannedAdditionsForRetryBase(spec, lastRejectedRevision, adopted);
      const sanitizedBase = sanitizeOptimizationPromptSpec(
        { ...spec, ...retryBaseRevision },
        failedResults,
      );
      patchBase = {
        objects: sanitizedBase.objects,
        operations: sanitizedBase.operations,
        rules: sanitizedBase.rules ?? [],
        metrics: sanitizedBase.metrics ?? [],
        scripts: sanitizedBase.scripts ?? [],
      };
    } else {
      patchBase = originalPatchBase;
    }
    const accumulatedValidationErrors = [...new Set(
      validationErrors.filter((message) => rejectedPatchCanReturnToModel(message)),
    )];
    const repairPrompt = attempt === 1 ? body : previousPatch ? [
      '【定点修复上下文】',
      '这份候选补丁已经部分落实建议。本轮禁止重新设计或全量重写知识包，只修复下方确定性错误，并保留其余已完成修改。',
      '【上次提交未通过确定性校验】',
      lastValidationError,
      ...(accumulatedValidationErrors.length > 1 ? [
        '【本轮此前全部确定性校验问题】',
        ...accumulatedValidationErrors,
        '本次提交必须同时保持以上问题全部修复；不要为了修复最新问题而重新引入此前已发现的问题。',
      ] : []),
      '【上次未通过校验的候选补丁】',
      JSON.stringify(previousPatch),
      promptFieldContract,
      '【仍需满足的采纳建议】',
      JSON.stringify(promptSuggestions),
      '请优先使用 submit_optimization_patch，只提交本轮为修复错误而改动或新增的资源。必须逐字使用建议 target 指定的资源 key。',
    ].join('\n') : [
      body,
      '【上次提交未通过确定性校验】',
      lastValidationError,
      '候选补丁因安全原因不能回传。请依据当前规格重新提交资源补丁，且不得复用被拒绝的评测证据或样例数值。',
    ].join('\n');
    await onProgress?.({ phase: 'revision_started', attempt, maxAttempts: OPTIMIZATION_MAX_ATTEMPTS });
    const agent = makeAgent(llm, sys, [submitPatch, submitRevised] as any);
    await runAgent(agent, repairPrompt);
    try {
      const revised = validateRevision(cap.revised);
      await onProgress?.({ phase: 'validation_passed', attempt, maxAttempts: OPTIMIZATION_MAX_ATTEMPTS });
      return revised;
    } catch (error) {
      lastValidationError = error instanceof Error ? error.message : String(error);
      lastRejectedRevision = validationRevisionForRetry ?? cap.revised;
      validationErrors.push(`第 ${attempt} 次：${lastValidationError}`);
      await onProgress?.({
        phase: 'validation_failed', attempt, maxAttempts: OPTIMIZATION_MAX_ATTEMPTS, error: lastValidationError,
      });
      if (attempt === OPTIMIZATION_MAX_ATTEMPTS) {
        throw new OptimizationExpertReviewRequiredError(validationErrors);
      }
    }
  }
  throw new Error(`优化器未能正确落实建议：${lastValidationError}`);
}
