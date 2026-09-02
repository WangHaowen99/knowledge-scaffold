import { Type } from '@earendil-works/pi-ai';
import type { LlmDeps } from '../llm/provider.js';
import { makeAgent, runAgentCaptured } from '../workflow/agent.js';
import type { DataDictTable, PackageSpec } from '../types.js';
import type { QualityGap } from '../workflow/data-quality.js';
import { mapWithConcurrency } from '../workflow/generation-planner.js';
import type { EvalResult } from '../workflow/m3-repo.js';
import { buildTools } from '../workflow/tools.js';
import type { EvaluationExecutionTrace } from './trace-client.js';

export type { EvaluationExecutionTrace } from './trace-client.js';

export type FailureCategory =
  | 'testcase_conflict'
  | 'testcase_gap'
  | 'runtime_error'
  | 'data_gap'
  | 'knowledge_gap'
  | 'routing_issue'
  | 'caliber_issue'
  | 'needs_script'
  | 'judge_issue';

export type RepairScope = 'testcase' | 'runtime' | 'data' | 'package' | 'script' | 'judge';

export function isKnowledgePackageRepairScope(scope: string): boolean {
  return scope === 'package' || scope === 'script';
}

// packageOptimizable 表示已经确认可优化；这里判断专家能否把该诊断纳入修缮范围。
export function isPackageRepairEligible(
  diagnosis: Pick<FailureDiagnosis, 'repairScope' | 'blocking'>,
): boolean {
  return isKnowledgePackageRepairScope(diagnosis.repairScope) && !diagnosis.blocking;
}

export interface FailureDiagnosis {
  category: FailureCategory;
  title: string;
  summary: string;
  evidence: string[];
  confidence: 'high' | 'medium' | 'low';
  blocking: boolean;
  repairScope: RepairScope;
  packageOptimizable: boolean;
  resultIds: string[];
  relatedQuestions: string[];
  suggestedAction: string;
}

export interface FailureDiagnosisReport {
  projectId: string;
  diagnoses: FailureDiagnosis[];
  canProposeOptimizations: boolean;
}

export interface DiagnoseEvaluationInput {
  projectId: string;
  results: EvalResult[];
  spec: PackageSpec;
  qualityGaps?: QualityGap[];
  traces?: Record<string, EvaluationExecutionTrace>;
  tables?: DataDictTable[];
  documents?: { filename: string; textContent?: string }[];
  /** 评测在执行用例前失败时的轮次级错误日志。 */
  roundFailure?: string;
}

export function hasOptimizableDiagnoses(diagnoses: FailureDiagnosis[]): boolean {
  return diagnoses.some((item) => item.packageOptimizable && isPackageRepairEligible(item));
}

const EVALUATION_ORACLE_EVIDENCE = /(?:标准答案|参考答案|ground\s*truth|(?:智能体|模型)(?:回答|答案|输出原文)|评测(?:答案|结果|得分|通过率)|(?:用例|案例).{0,24}(?:标准|判定|通过|失败|回答|结果)|(?:全部|所有|\d+\s*个)(?:用例|案例)|\ber_[A-Za-z0-9_-]+\b)/iu;

function withoutOracleSentences(value: string): string {
  return (String(value || '').match(/[^。！？；\n]+[。！？；]?/gu) ?? [])
    .filter((sentence) => !EVALUATION_ORACLE_EVIDENCE.test(sentence))
    .join('')
    .trim();
}

function guardSemanticDiagnosisEvidence(diagnosis: FailureDiagnosis): FailureDiagnosis {
  if (!diagnosis.packageOptimizable || diagnosis.blocking) return diagnosis;
  const independentEvidence = diagnosis.evidence.filter((item) => !EVALUATION_ORACLE_EVIDENCE.test(item));
  const coreConclusion = `${diagnosis.title}\n${diagnosis.suggestedAction}`;
  if (!EVALUATION_ORACLE_EVIDENCE.test(coreConclusion) && independentEvidence.length) {
    return {
      ...diagnosis,
      summary: withoutOracleSentences(diagnosis.summary)
        || '独立的资料、包内定义或执行轨迹已经确认该问题。',
      evidence: independentEvidence,
    };
  }
  return {
    ...diagnosis,
    category: 'judge_issue',
    title: '诊断缺少独立于评测答案的证据',
    summary: '当前结论缺少独立证据，且依赖评测答案或判定结果反推，不能据此修改知识包。',
    evidence: independentEvidence.length
      ? independentEvidence
      : ['诊断输出未提供可独立核验的资料、包内定义或执行轨迹证据。'],
    confidence: 'low',
    blocking: false,
    repairScope: 'judge',
    packageOptimizable: false,
    suggestedAction: '先补充来源资料、包内定义或执行轨迹证据，再判断是否需要修改知识包。',
  };
}

export function normalizeEvaluationQuestion(value: string): string {
  return String(value || '')
    .normalize('NFKC')
    .toLocaleLowerCase('zh-CN')
    .replace(/[\s\p{P}\p{S}]+/gu, '');
}

function testcaseConflictDiagnoses(results: EvalResult[]): FailureDiagnosis[] {
  const groups = new Map<string, EvalResult[]>();
  for (const result of results) {
    const key = normalizeEvaluationQuestion(result.question);
    const items = groups.get(key) ?? [];
    items.push(result);
    groups.set(key, items);
  }

  const diagnoses: FailureDiagnosis[] = [];
  for (const items of groups.values()) {
    const truths = new Map<string, string>();
    for (const item of items) {
      const normalized = normalizeEvaluationQuestion(item.groundTruth);
      if (!truths.has(normalized)) truths.set(normalized, item.groundTruth.trim());
    }
    if (truths.size <= 1) continue;
    const evidence = [...truths.values()].map((truth, index) => `标准答案 ${index + 1}：${truth}`);
    diagnoses.push({
      category: 'testcase_conflict',
      title: '相同问题存在冲突的标准答案',
      summary: '相同语义的问题配置了不同标准答案，无法通过修改知识包同时满足。',
      evidence,
      confidence: 'high',
      blocking: true,
      repairScope: 'testcase',
      packageOptimizable: false,
      resultIds: items.map((item) => item.id),
      relatedQuestions: [items[0].question],
      suggestedAction: '先确认正确业务口径，合并或修正冲突用例，再重新评测。',
    });
  }
  return diagnoses;
}

function scriptRuntimeFailures(trace: EvaluationExecutionTrace | undefined) {
  return (trace?.toolFailures ?? []).filter((failure) => failure.toolName === 'execute_script');
}

function scriptRuntimeFailureDiagnoses(
  results: EvalResult[],
  traces: Record<string, EvaluationExecutionTrace>,
): FailureDiagnosis[] {
  return results.flatMap((result) => {
    const failures = scriptRuntimeFailures(traces[result.id]);
    if (!failures.length) return [];
    return [{
      category: 'runtime_error' as const,
      title: '确定性脚本执行失败',
      summary: '评测已路由到知识包脚本，但脚本运行失败；应修复脚本接口、参数或实现，而不是回退到手工计算。',
      evidence: failures.slice(0, 3).map((failure) => `execute_script：${failure.error}`),
      confidence: 'high' as const,
      blocking: false,
      repairScope: 'script' as const,
      packageOptimizable: true,
      resultIds: [result.id],
      relatedQuestions: [result.question],
      suggestedAction: '根据工具错误修复确定性脚本，并用独立变体数据验证脚本接口、输入口径和计算结果。',
    }];
  });
}

function runtimeErrorDiagnoses(
  results: EvalResult[],
  traces: Record<string, EvaluationExecutionTrace>,
): FailureDiagnosis[] {
  const runtimePattern = /unauthorized|forbidden|\b40[13]\b|timeout|timed out|超时|限流|\b429\b|\b50[0234]\b|provider|connection|连接失败|fetch failed/i;
  return results
    .filter((result) => !scriptRuntimeFailures(traces[result.id]).length
      && !evaluationNeedsInput(result)
      && (result.verdict === 'error' || runtimePattern.test(result.reason ?? '')))
    .map((result) => ({
      category: 'runtime_error' as const,
      title: '评测运行环境失败',
      summary: '用例未进入可判定的业务回答阶段，修改知识包不能解决该错误。',
      evidence: [result.reason || '评测结果标记为 error'],
      confidence: 'high' as const,
      blocking: false,
      repairScope: 'runtime' as const,
      packageOptimizable: false,
      resultIds: [result.id],
      relatedQuestions: [result.question],
      suggestedAction: '修复凭据、连接或运行状态后重新执行该用例，不要据此修改知识包。',
    }));
}

function judgeProtocolDiagnoses(results: EvalResult[]): FailureDiagnosis[] {
  const affected = results.filter((result) => /judge\s*未产出结论|评测判分服务未按协议提交结论/i.test(result.reason ?? ''));
  if (!affected.length) return [];
  return [{
    category: 'judge_issue',
    title: '评测判分服务未返回结构化结论',
    summary: '评测回答尚未得到有效的自动判分，不能把该轮结果当作知识包内容失败。',
    evidence: [...new Set(affected.map((result) => result.reason || 'Judge 未返回结论'))].slice(0, 3),
    confidence: 'high',
    blocking: false,
    repairScope: 'judge',
    packageOptimizable: false,
    resultIds: affected.map((result) => result.id),
    relatedQuestions: affected.map((result) => result.question),
    suggestedAction: '修复或切换可用的判分模型后重新评测；不要据此修改知识包。',
  }];
}

function evaluationNeedsInput(result: EvalResult): boolean {
  return /评测用例缺少运行所需输入/.test(result.reason ?? '');
}

function roundFailureDiagnoses(message: string | undefined): FailureDiagnosis[] {
  const detail = String(message ?? '').trim();
  if (!detail) return [];
  const evidence = detail.split('\n').map((line) => line.trim()).filter(Boolean).slice(-5);
  return [{
    category: 'runtime_error',
    title: '评测准备阶段失败',
    summary: '评测尚未进入用例执行阶段，知识包导入、认证、连接或评测库准备过程发生错误。',
    evidence: evidence.length ? evidence : ['评测轮次标记为失败'],
    confidence: 'high',
    blocking: false,
    repairScope: 'runtime',
    packageOptimizable: false,
    resultIds: [],
    relatedQuestions: [],
    suggestedAction: '先修复认证、连接或评测库权限，再重新运行评测；不要据此修改知识包。',
  }];
}

function compactText(value: string): string {
  return String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN').replace(/[\s\p{P}\p{S}]+/gu, '');
}

function gapMatchesResult(gap: QualityGap, result: EvalResult): boolean {
  const evidence = compactText(`${gap.target}${gap.message}`);
  const testcase = compactText(`${result.question}${result.groundTruth}${result.reason ?? ''}`);
  if (!evidence || !testcase) return false;
  const asciiTokens = `${gap.target} ${gap.message}`.match(/[A-Za-z_][A-Za-z0-9_.]{2,}/g) ?? [];
  if (asciiTokens.some((token) => testcase.includes(compactText(token)))) return true;
  for (let size = Math.min(8, evidence.length); size >= 4; size--) {
    for (let start = 0; start + size <= evidence.length; start++) {
      if (testcase.includes(evidence.slice(start, start + size))) return true;
    }
  }
  return false;
}

function dataGapDiagnoses(results: EvalResult[], gaps: QualityGap[]): FailureDiagnosis[] {
  const diagnoses: FailureDiagnosis[] = [];
  for (const gap of gaps) {
    const related = results.filter((result) => result.verdict !== 'error' && gapMatchesResult(gap, result));
    if (!related.length) continue;
    diagnoses.push({
      category: 'data_gap',
      title: `${gap.kind}：${gap.target}`,
      summary: gap.message,
      evidence: [gap.message, `补齐方式：${gap.how}`],
      confidence: 'high',
      blocking: true,
      repairScope: 'data',
      packageOptimizable: false,
      resultIds: related.map((item) => item.id),
      relatedQuestions: related.map((item) => item.question),
      suggestedAction: gap.how,
    });
  }
  return diagnoses;
}

function scriptNeedDiagnoses(
  results: EvalResult[],
  traces: Record<string, EvaluationExecutionTrace>,
): FailureDiagnosis[] {
  const calculationPattern = /计算|统计|汇总|负荷|效益|容量|收率|金额|百分比|公式|最优|规划/;
  return results.flatMap((result) => {
    if (result.verdict === 'error' || !calculationPattern.test(`${result.question}${result.groundTruth}`)) return [];
    const trace = traces[result.id];
    if (!trace || trace.status === 'unavailable') return [];
    const toolCalls = trace?.toolCalls ?? [];
    const businessSearches = toolCalls.filter((name) => name === 'business_data_search').length;
    const hasScript = toolCalls.includes('execute_script');
    const batchCount = (result.question.match(/第\s*\d+\s*批/g) ?? []).length;
    if (hasScript || (businessSearches < 2 && batchCount < 3)) return [];
    return [{
      category: 'needs_script' as const,
      title: '复杂计算需要确定性脚本',
      summary: '该问题包含多批次或多次数据查询后的精确计算，继续依赖模型手工运算会产生不稳定结果。',
      evidence: [
        `business_data_search 调用 ${businessSearches} 次，execute_script 调用 ${hasScript ? 1 : 0} 次`,
        batchCount ? `问题包含 ${batchCount} 个批次` : '问题要求输出精确数值结果',
      ],
      confidence: 'high' as const,
      blocking: false,
      repairScope: 'script' as const,
      packageOptimizable: true,
      resultIds: [result.id],
      relatedQuestions: [result.question],
      suggestedAction: '生成受控脚本并绑定对应 operation，用固定公式、输入校验和结构化输出完成计算。',
    }];
  });
}

const LLM_CATEGORIES = ['testcase_gap', 'data_gap', 'knowledge_gap', 'routing_issue', 'caliber_issue', 'judge_issue'] as const;
const REPAIR_SCOPES = ['testcase', 'runtime', 'data', 'package', 'script', 'judge'] as const;
const SEMANTIC_DIAGNOSIS_BATCH_ITEMS = 4;
const SEMANTIC_DIAGNOSIS_BATCH_CHARS = 45_000;
const SEMANTIC_DIAGNOSIS_CONCURRENCY = 4;

function semanticDiagnosisBatches(candidates: EvalResult[]): EvalResult[][] {
  const batches: EvalResult[][] = [];
  let current: EvalResult[] = [];
  let currentChars = 0;
  for (const result of candidates) {
    const resultChars = JSON.stringify({
      question: result.question,
      groundTruth: result.groundTruth,
      finalAnswer: result.finalAnswer ?? '',
      reason: result.reason ?? '',
    }).length;
    if (current.length && (
      current.length >= SEMANTIC_DIAGNOSIS_BATCH_ITEMS
      || currentChars + resultChars > SEMANTIC_DIAGNOSIS_BATCH_CHARS
    )) {
      batches.push(current);
      current = [];
      currentChars = 0;
    }
    current.push(result);
    currentChars += resultChars;
  }
  if (current.length) batches.push(current);
  return batches;
}

function semanticDiagnosisFailureCode(error: unknown): string {
  const code = error && typeof error === 'object' && 'code' in error
    ? String((error as { code?: unknown }).code ?? '').trim()
    : '';
  if (/^[A-Z][A-Z0-9_-]{2,80}$/u.test(code)) return code;
  const message = error instanceof Error ? error.message : String(error ?? '');
  if (/timeout|timed\s*out|超时/iu.test(message)) return 'MODEL_REQUEST_TIMEOUT';
  if (/rate\s*limit|too\s*many\s*requests|限流|\b429\b/iu.test(message)) return 'MODEL_RATE_LIMITED';
  if (/未按协议|未调用|未提交|submit|capture/iu.test(message)) return 'MODEL_CAPTURE_FAILED';
  const name = error instanceof Error ? error.name.trim() : '';
  return name && name !== 'Error' && /^[A-Za-z][A-Za-z0-9_-]{2,80}$/u.test(name)
    ? name
    : 'MODEL_DIAGNOSIS_FAILED';
}

async function semanticDiagnoses(
  input: DiagnoseEvaluationInput,
  llm: LlmDeps,
  existing: FailureDiagnosis[],
): Promise<FailureDiagnosis[]> {
  const excluded = new Set(existing
    .filter((diagnosis) => diagnosis.category === 'testcase_conflict'
      || diagnosis.category === 'runtime_error'
      || diagnosis.category === 'judge_issue')
    .flatMap((diagnosis) => diagnosis.resultIds));
  const candidates = input.results.filter((result) => (
    result.verdict !== 'error' || evaluationNeedsInput(result)
  ) && !excluded.has(result.id));
  if (!candidates.length) return [];
  const batches = semanticDiagnosisBatches(candidates);
  if (batches.length > 1) {
    const outcomes = await mapWithConcurrency(
      batches,
      SEMANTIC_DIAGNOSIS_CONCURRENCY,
      async (batch, index) => {
        try {
          return {
            diagnoses: await semanticDiagnoses(
              { ...input, results: batch },
              llm,
              existing,
            ),
            failedResults: [] as EvalResult[],
            failureEvidence: '',
          };
        } catch (error) {
          return {
            diagnoses: [] as FailureDiagnosis[],
            failedResults: batch,
            failureEvidence: `诊断批次 ${index + 1}：${semanticDiagnosisFailureCode(error)}`,
          };
        }
      },
    );
    const failedResults = outcomes.flatMap((outcome) => outcome.failedResults);
    const diagnoses = outcomes.flatMap((outcome) => outcome.diagnoses);
    if (failedResults.length) {
      diagnoses.push({
        category: 'runtime_error',
        title: '部分失败原因分析未完成',
        summary: `${failedResults.length} 条失败用例的语义诊断未完整返回；其他已完成批次的诊断结果仍然保留。`,
        evidence: outcomes.map((outcome) => outcome.failureEvidence).filter(Boolean),
        confidence: 'high',
        blocking: false,
        repairScope: 'runtime',
        packageOptimizable: false,
        resultIds: failedResults.map((result) => result.id),
        relatedQuestions: failedResults.map((result) => result.question),
        suggestedAction: '确认后台任务模型可用后重新诊断；不要根据未完成批次直接修改知识包。',
      });
    }
    return diagnoses;
  }

  const cap: { diagnoses?: FailureDiagnosis[] } = {};
  const submit = {
    name: 'submit_failure_diagnoses',
    label: '提交失败原因诊断',
    description: '只提交失败原因、证据、影响范围和处理方向；本阶段禁止提交知识包修改内容。',
    parameters: Type.Object({
      diagnoses: Type.Array(Type.Object({
        category: Type.Union(LLM_CATEGORIES.map((item) => Type.Literal(item)) as any),
        title: Type.String(),
        summary: Type.String(),
        evidence: Type.Array(Type.String()),
        confidence: Type.Union([Type.Literal('high'), Type.Literal('medium'), Type.Literal('low')]),
        blocking: Type.Boolean(),
        repairScope: Type.Union(REPAIR_SCOPES.map((item) => Type.Literal(item)) as any),
        packageOptimizable: Type.Boolean(),
        resultIds: Type.Array(Type.String()),
        relatedQuestions: Type.Array(Type.String()),
        suggestedAction: Type.String(),
      })),
    }),
    async execute(_id: string, params: { diagnoses: FailureDiagnosis[] }) {
      cap.diagnoses = params.diagnoses;
      return { content: [{ type: 'text' as const, text: `已提交 ${params.diagnoses.length} 条失败原因诊断。` }], details: {}, terminate: true };
    },
  };

  const traces = Object.fromEntries(candidates.map((result) => [result.id, input.traces?.[result.id] ?? null]));
  const system = [
    '你是评测失败诊断员。只定位根因，不修改知识包，不输出 operation/rule/metric/script 的具体补丁。',
    '按证据区分：testcase_gap=用例问题本身缺少不可推断的必需决策或条件；data_gap=上传的源表或文档中缺少必需值，必须先补源数据；knowledge_gap=源资料已包含业务知识但知识包未表达；',
    'routing_issue=正确资源存在但未走到；',
    '对“评测用例缺少运行所需输入”要核对问题原文：若值已给定但 operation 仍要求用户重复选择，属于 routing_issue；不得当成运行环境故障。',
    'caliber_issue=公式、统计粒度或边界错误；judge_issue=标准答案或判分依据不可靠。',
    '数据缺口、需要脚本与现有脚本的独立公式错误可以同时存在，不得因已有确定性诊断而跳过语义根因检查。',
    '若 Trace 显示已执行脚本但结果错误，应核对脚本规格、公式、输入口径和现有资料证据；不能把父级数量、派生数量或其他不同粒度默认为等价。',
    '若来源字段、对象定义或公式把输入列为多个组成项，必须逐项核对脚本是否覆盖；不得从其中一项的路由条件推断其他固定组成项不存在。',
    '诊断的 summary 与 evidence 必须一致；证据同时显示多个必需项时，不得把根因缩减成只缺其中一项。',
    '资料评估中的 qualityGaps 是待核对假设，不是资料缺失的最终证明。有证据工具时，先查看相关表字段、数据行或文档段落再下结论。',
    '诊断证据应引用精确的表名、字段名、代表行或文档定位；不得仅以标准答案与回答不同作为公式证据。',
    '不能从标准答案反向编造规则。证据不足时降低 confidence，并明确需要补什么证据。',
    '任何标记为可优化的诊断，title、summary、evidence 和 suggestedAction 都不得引用标准答案、用例判定或智能体回答作为修改依据。',
    '用 submit_failure_diagnoses 提交结构化结果。',
  ].join('\n');
  const body = JSON.stringify({
    failures: candidates.map((result) => ({
      id: result.id, question: result.question, groundTruth: result.groundTruth,
      finalAnswer: result.finalAnswer ?? '', verdict: result.verdict, reason: result.reason ?? '',
    })),
    traces,
    packageSpec: input.spec,
    qualityGaps: input.qualityGaps ?? [],
    availableEvidence: {
      tables: (input.tables ?? []).map((table) => ({ name: table.name, rowCount: table.rowCount })),
      documents: (input.documents ?? []).map((document) => ({
        filename: document.filename, characters: document.textContent?.length ?? 0,
      })),
    },
  });
  const evidenceTools = buildTools({
    tables: input.tables ?? [], documents: input.documents ?? [], captured: {},
  });
  const agent = makeAgent(llm, system, [...evidenceTools, submit] as any);
  await runAgentCaptured(
    agent,
    body,
    () => Boolean(cap.diagnoses),
    '不要继续输出分析正文。请把刚才的结论整理为结构化参数，并立即调用 submit_failure_diagnoses 提交；未通过工具提交的内容不会被保存。',
  );
  if (!cap.diagnoses) throw new Error('失败诊断器未按协议提交诊断结果');

  const validIds = new Set(candidates.map((item) => item.id));
  return cap.diagnoses.map((diagnosis) => {
    const resultIds = diagnosis.resultIds.filter((id) => validIds.has(id));
    if (diagnosis.category === 'testcase_gap') {
      return {
        ...diagnosis,
        resultIds,
        repairScope: 'testcase' as const,
        packageOptimizable: false,
        blocking: true,
        suggestedAction: diagnosis.suggestedAction || '补齐评测用例中无法从上下文推断的必需输入后重新评测。',
      };
    }
    if (diagnosis.category === 'data_gap') {
      return {
        ...diagnosis,
        resultIds,
        repairScope: 'data' as const,
        packageOptimizable: false,
        blocking: true,
        suggestedAction: diagnosis.suggestedAction || '补齐上传资料中的必需源数据后重新评测。',
      };
    }
    if (diagnosis.category === 'judge_issue') {
      return {
        ...diagnosis, resultIds, repairScope: 'judge' as const,
        packageOptimizable: false, blocking: false,
        suggestedAction: diagnosis.suggestedAction || '核对标准答案和 Judge 判分依据后重新评测。',
      };
    }
    return guardSemanticDiagnosisEvidence({
      ...diagnosis,
      resultIds,
      repairScope: diagnosis.repairScope === 'script' ? 'script' as const : 'package' as const,
      packageOptimizable: true, blocking: false,
    });
  }).filter((diagnosis) => diagnosis.resultIds.length > 0);
}

export async function diagnoseEvaluationFailures(
  input: DiagnoseEvaluationInput,
  llmOverride?: LlmDeps,
): Promise<FailureDiagnosisReport> {
  const deterministic = [
    ...roundFailureDiagnoses(input.roundFailure),
    ...testcaseConflictDiagnoses(input.results),
    ...scriptRuntimeFailureDiagnoses(input.results, input.traces ?? {}),
    ...runtimeErrorDiagnoses(input.results, input.traces ?? {}),
    ...judgeProtocolDiagnoses(input.results),
    ...dataGapDiagnoses(input.results, input.qualityGaps ?? []),
    ...scriptNeedDiagnoses(input.results, input.traces ?? {}),
  ];
  const diagnoses = llmOverride
    ? [...deterministic, ...await semanticDiagnoses(input, llmOverride, deterministic)]
    : deterministic;
  return {
    projectId: input.projectId,
    diagnoses,
    canProposeOptimizations: hasOptimizableDiagnoses(diagnoses),
  };
}
