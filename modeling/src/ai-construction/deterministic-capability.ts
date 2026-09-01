import type { SpecMetric, SpecOperation } from '../types.js';

export interface ScriptCandidate {
  operationKey: string;
  title: string;
  confidence: 'medium' | 'high';
  reason: string;
  suggestedContract?: string;
  invokesOperationKeys?: string[];
}

export interface ScriptDecision {
  selectedOperationKeys: string[];
}

export interface ScriptContract {
  operationKey: string;
  title: string;
  contract: string;
}

export class ScriptDecisionInputError extends Error {
  readonly code = 'INVALID_INPUT';

  constructor(message: string) {
    super(message);
    this.name = 'ScriptDecisionInputError';
  }
}

const HIGH_SIGNAL = /(计算|公式|聚合|汇总|求和|平均|比率|比例|收率|换算|日期|天数|周期|金额|成本|产量|数量|数值|精确转换)/;
const MEDIUM_SIGNAL = /(排序|排名|分配|分流|路由|二选一|唯一选择|条件分支|去重|格式转换|编码转换|批量转换)/;
const OPEN_SEMANTIC = /(经验判断|综合研判|开放式|语义分析|主观判断|人工判断)/;
const NUMBER_TYPE = /^(number|integer|float|double|decimal|int|long)$/i;

function operationContractSuggestion(operation: SpecOperation): string {
  const inputs = operation.inputs?.length
    ? operation.inputs.map((item) => `${item.param}(${item.type}，${item.required ? '必填' : '可选'})`).join('、')
    : '以业务操作定义的输入和关联对象作为受控输入';
  const outputs = operation.outputs?.length
    ? operation.outputs.map((item) => `${item.field}(${item.type})`).join('、')
    : '以业务操作定义的业务结果作为结构化输出';
  return [
    `输入：${inputs}`,
    `输出：${outputs}`,
    '测试：同一合法输入重复执行结果一致；缺少必填输入时明确失败；结果满足已确认的业务操作步骤、规则和指标约束。',
  ].join('\n');
}

export function identifyScriptCandidates(
  operations: SpecOperation[],
  metrics: SpecMetric[],
): ScriptCandidate[] {
  const metricKeys = new Set(metrics.map((item) => item.key));
  const candidates = operations.flatMap<ScriptCandidate>((operation): ScriptCandidate[] => {
    if (operation.script?.key) return [];
    const text = [operation.title, operation.description, ...(operation.flow ?? [])].join('\n');
    const numericOutput = operation.outputs?.some((item) => NUMBER_TYPE.test(item.type.trim()));
    const usesMetric = operation.usesMetrics?.some((key) => metricKeys.has(key)) ?? false;
    const highSignals = [
      ...(usesMetric ? ['引用明确指标口径'] : []),
      ...(numericOutput ? ['包含数值输出'] : []),
      ...(HIGH_SIGNAL.test(text) ? ['流程包含计算、聚合或精确换算'] : []),
    ];
    if (highSignals.length) {
      return [{
        operationKey: operation.key,
        title: operation.title,
        confidence: 'high' as const,
        reason: highSignals.join('；'),
        suggestedContract: operationContractSuggestion(operation),
      }];
    }
    if (MEDIUM_SIGNAL.test(text) && !OPEN_SEMANTIC.test(text)) {
      return [{
        operationKey: operation.key,
        title: operation.title,
        confidence: 'medium' as const,
        reason: '流程包含分流、排序、分配、条件分支或确定性转换步骤',
        suggestedContract: operationContractSuggestion(operation),
      }];
    }
    return [];
  });
  const candidateKeys = new Set(candidates.map((candidate) => candidate.operationKey));
  const operationByKey = new Map(operations.map((operation) => [operation.key, operation]));
  return candidates.map((candidate) => {
    const operation = operationByKey.get(candidate.operationKey);
    const operationText = operation
      ? [operation.title, operation.description, ...(operation.flow ?? [])].join('\n')
      : '';
    const invokesOperationKeys = [...candidateKeys]
      .filter((key) => key !== candidate.operationKey && operationText.includes(key));
    return invokesOperationKeys.length ? { ...candidate, invokesOperationKeys } : candidate;
  });
}

export function parseScriptDecisionResponse(
  response: unknown,
  candidates: ScriptCandidate[],
): ScriptDecision {
  if (!response || typeof response !== 'object' || !Array.isArray((response as any).answers)) {
    throw new ScriptDecisionInputError('请选择需要生成脚本的业务操作，或选择本轮暂不生成');
  }
  const answer = (response as any).answers.find(
    (item: any) => String(item?.questionId ?? '') === 'script_candidates',
  );
  const selected: string[] = Array.isArray(answer?.selected)
    ? [...new Set<string>(answer.selected
      .map((item: unknown) => String(item).trim())
      .filter((item: string) => Boolean(item)))]
    : [];
  if (!selected.length) {
    throw new ScriptDecisionInputError('请选择需要生成脚本的业务操作，或选择本轮暂不生成');
  }
  if (selected.includes('__none__') && selected.length > 1) {
    throw new ScriptDecisionInputError('“本轮暂不生成脚本”不能同时与具体业务操作一起选择');
  }
  if (selected.length === 1 && selected[0] === '__none__') return { selectedOperationKeys: [] };
  const allowed = new Set(candidates.map((item) => item.operationKey));
  const unknown = selected.filter((item) => !allowed.has(item));
  if (unknown.length) {
    throw new ScriptDecisionInputError(`选择的 operation 不属于当前候选：${unknown.join('、')}`);
  }
  return { selectedOperationKeys: selected };
}

function answerText(answer: any): string {
  return [
    ...(Array.isArray(answer?.selected) ? answer.selected.map(String) : []),
    typeof answer?.other === 'string' ? answer.other : '',
  ].map((item) => item.trim()).filter(Boolean).join('；');
}

export function parseScriptContractResponse(
  response: unknown,
  selected: ScriptCandidate[],
): ScriptContract[] {
  if (!response || typeof response !== 'object' || !Array.isArray((response as any).answers)) {
    throw new Error('请确认每个脚本的输入、输出和测试案例');
  }
  const answers = new Map((response as any).answers.map((item: any) => [
    String(item?.questionId ?? ''),
    answerText(item),
  ]));
  return selected.map((candidate, index) => {
    const contract = String(answers.get(`script_contract_${index + 1}`) ?? '').trim();
    if (!contract) {
      throw new Error(`请补充 ${candidate.operationKey} 的输入、输出和测试案例`);
    }
    return { operationKey: candidate.operationKey, title: candidate.title, contract };
  });
}

export function buildScriptGenerationRequirement(
  selected: ScriptCandidate[],
  contracts: ScriptContract[],
): string {
  const contractByOperation = new Map(contracts.map((item) => [item.operationKey, item]));
  const tasks = selected.map((candidate, index) => {
    const contract = contractByOperation.get(candidate.operationKey);
    if (!contract) throw new Error(`缺少 ${candidate.operationKey} 的脚本契约`);
    return [
      `${index + 1}. operation ${candidate.operationKey}（${candidate.title}）`,
      `用户确认的契约与测试：${contract.contract}`,
    ].join('\n');
  }).join('\n');
  return [
    '请为以下已确认的确定性计算生成受控 Python 脚本，并绑定到对应业务操作。',
    '每个业务操作单独生成一个脚本；不得修改未列出的对象、操作、规则或指标。',
    '脚本必须使用 argparse 接收结构化输入并输出 JSON；不得访问网络、任意文件系统、启动子进程或动态执行代码。',
    '用户给出的测试案例必须体现在实现约束中，不能把某条测试答案硬编码进脚本。',
    tasks,
  ].join('\n');
}
