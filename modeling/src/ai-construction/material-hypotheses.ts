import type {
  AnalysisResult,
  CandidateBusinessGoal,
  CandidateBusinessProcess,
  ModelingBrief,
} from '../types.js';

const MAX_HYPOTHESES = 3;

function short(value: string | undefined, max: number): string {
  return value?.trim().slice(0, max) ?? '';
}

function compact(values: string[] | undefined, maxLength = 500): string[] {
  return [...new Set((values ?? []).map((value) => short(value, maxLength)).filter(Boolean))];
}

function ordered(values: string[] | undefined, maxLength = 500): string[] {
  return (values ?? []).map((value) => short(value, maxLength)).filter(Boolean);
}

function normalizeGoal(value: CandidateBusinessGoal): CandidateBusinessGoal | null {
  const title = short(value.title, 160);
  const problem = short(value.problem, 2_000);
  if (!title || !problem) return null;
  return {
    title,
    problem,
    targetUsers: compact(value.targetUsers),
    scenarios: compact(value.scenarios),
    successCriteria: compact(value.successCriteria),
    rationale: short(value.rationale, 500) || '根据当前建模资料整理的待确认假设。',
  };
}

function normalizeProcess(value: CandidateBusinessProcess): CandidateBusinessProcess | null {
  const title = short(value.title, 160);
  if (!title) return null;
  return {
    ...(short(value.candidateId, 100) ? { candidateId: short(value.candidateId, 100) } : {}),
    title,
    trigger: short(value.trigger, 1_000) || '收到业务处理请求',
    actors: compact(value.actors),
    inputs: compact(value.inputs),
    // 流程可能合法地重复同一动作或回到先前环节，不能用 Set 去重破坏顺序语义。
    steps: ordered(value.steps),
    exceptions: compact(value.exceptions),
    outputs: compact(value.outputs),
    handoffs: compact(value.handoffs),
    rationale: short(value.rationale, 500) || '根据当前建模资料整理的待确认假设。',
  };
}

function assignProcessCandidateIds(processes: CandidateBusinessProcess[]): CandidateBusinessProcess[] {
  const used = new Set<string>();
  return processes.map((process, index) => {
    let candidateId = process.candidateId?.trim() ?? '';
    if (!candidateId || used.has(candidateId)) {
      let suffix = index + 1;
      do candidateId = `process_candidate_${suffix++}`;
      while (used.has(candidateId));
    }
    used.add(candidateId);
    return { ...process, candidateId };
  });
}

function fallbackTopic(analysis: AnalysisResult): string {
  const operation = analysis.candidateArtifacts?.find((item) => item.kind === 'operation')?.name?.trim();
  const object = analysis.candidateObjects.find((item) => item.name.trim())?.name.trim();
  return operation || object || '核心业务处理';
}

function briefItems(value: string): string[] {
  return compact(value.split(/[\n；;,，]+/u));
}

/**
 * 保证资料分析可以给后续访谈提供少量、明确标注为待确认的候选。
 * 模型结果优先；只有模型没有按新协议提交候选时才从既有分析摘要生成兜底假设。
 */
export function ensureMaterialHypotheses(
  analysis: AnalysisResult,
  confirmedBrief?: ModelingBrief,
): AnalysisResult {
  const goals = (analysis.candidateGoals ?? [])
    .map(normalizeGoal)
    .filter((item): item is CandidateBusinessGoal => Boolean(item))
    .slice(0, MAX_HYPOTHESES);
  const processes = (analysis.candidateProcesses ?? [])
    .map(normalizeProcess)
    .filter((item): item is CandidateBusinessProcess => Boolean(item))
    .slice(0, MAX_HYPOTHESES);
  const topic = confirmedBrief
    ? short(briefItems(confirmedBrief.scenarios)[0], 160) || short(confirmedBrief.problem, 160)
    : fallbackTopic(analysis);
  const confirmedGoal = confirmedBrief ? {
    title: short(confirmedBrief.problem, 160),
    problem: confirmedBrief.problem,
    targetUsers: briefItems(confirmedBrief.targetUsers),
    scenarios: briefItems(confirmedBrief.scenarios),
    successCriteria: briefItems(confirmedBrief.successCriteria),
    rationale: '已确认建模目标，作为资料分析范围。',
  } : null;
  const candidateProcesses = assignProcessCandidateIds(processes.length ? processes : [{
    title: topic,
    trigger: '收到业务处理请求',
    actors: ['业务人员'],
    inputs: [],
    steps: ['接收业务输入', '核验关键信息', '形成处理结果'],
    exceptions: [],
    outputs: ['业务处理结果'],
    handoffs: [],
    rationale: confirmedBrief
      ? '模型未提交完整流程候选，由已确认建模目标生成的待确认假设。'
      : '模型未提交完整流程候选，由资料中的 operation 或对象名称生成的待确认假设。',
  }]);

  return {
    ...analysis,
    ...(confirmedBrief ? { modelingBrief: confirmedBrief } : {}),
    candidateGoals: confirmedGoal ? [confirmedGoal] : goals.length ? goals : [{
      title: `围绕${topic}形成可执行知识`,
      problem: short(analysis.domainOverview, 2_000) || `支持${topic}相关业务判断和处理`,
      targetUsers: ['业务人员'],
      scenarios: [topic],
      successCriteria: ['能够基于资料和专家确认结果给出可追溯、可执行的业务结论'],
      rationale: '模型未提交完整目标候选，由现有分析摘要生成的待确认假设。',
    }],
    candidateProcesses,
  };
}
