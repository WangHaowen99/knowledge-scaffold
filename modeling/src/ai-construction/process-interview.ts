import type { JsonObject } from '../authoring/types.js';
import type { CandidateBusinessProcess, InterviewOutcome } from '../types.js';
import { buildProcessInterviewQuestions } from './interaction-options.js';
import { normalizeGeneratedProcessDraft } from './process-draft-generator.js';

export interface ProcessInterviewCore {
  title: string;
  trigger: string;
  actors: string[];
  outputs: string[];
}

export interface ProcessInterviewDetails {
  inputs: string[];
  stepLines: string[];
  exceptions: string[];
  handoffs: string[];
}

export interface ProcessSelectionResult {
  candidates: CandidateBusinessProcess[];
  customNote?: string;
}

type ProcessInterviewStage = 'core' | 'details';

const EMPTY_ANSWERS = new Set(['无', '没有', '暂无', '不适用', '无异常', '无交接']);

export function processInterviewQuestions(
  stage: ProcessInterviewStage,
  defaults: Partial<ProcessInterviewCore & ProcessInterviewDetails> = {},
  candidates: CandidateBusinessProcess[] = [],
) {
  return buildProcessInterviewQuestions(stage, candidates, defaults.title, defaults);
}

function answerText(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const answer = value as Record<string, unknown>;
  if (answer.skipped === true) return '';
  const selected = Array.isArray(answer.selected)
    ? answer.selected.map(String).map((item) => item.trim()).filter(Boolean)
    : [];
  const other = typeof answer.other === 'string' ? answer.other.trim() : '';
  return [...selected, ...(other ? [other] : [])].join('；');
}

function answerMap(response: JsonObject): Map<string, string> {
  if (!Array.isArray(response.answers)) return new Map();
  const answers = new Map<string, string>();
  response.answers.forEach((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const answer = value as Record<string, unknown>;
    const id = typeof answer.questionId === 'string' ? answer.questionId.trim() : '';
    if (id && !answers.has(id)) answers.set(id, answerText(answer));
  });
  return answers;
}

export function parseProcessSelectionResponse(
  response: JsonObject,
  candidates: CandidateBusinessProcess[],
): ProcessSelectionResult {
  const rawAnswer = Array.isArray(response.answers)
    ? response.answers.find((value) => (
      value && typeof value === 'object' && !Array.isArray(value)
      && (value as Record<string, unknown>).questionId === 'selected_processes'
    )) as Record<string, unknown> | undefined
    : undefined;
  const skipped = rawAnswer?.skipped === true;
  const selected = !skipped && Array.isArray(rawAnswer?.selected)
    ? rawAnswer.selected.map(String).map((item) => item.trim())
      .filter((item) => item && item !== '__other__')
    : [];
  const other = !skipped && typeof rawAnswer?.other === 'string' ? rawAnswer.other.trim() : '';
  const values = [...new Set(selected)];
  if (!values.length && !other) throw new Error('请至少选择一个流程或填写补充说明');
  const byId = new Map(candidates.flatMap((candidate) => {
    const candidateId = candidate.candidateId?.trim();
    return candidateId ? [[candidateId, candidate] as const] : [];
  }));
  const selectedCandidates = values.map((value) => {
    const identified = byId.get(value);
    if (identified) return identified;
    const titleMatches = candidates.filter((candidate) => candidate.title.trim() === value);
    if (titleMatches.length > 1) {
      throw new Error('存在同名候选流程，请刷新页面后重新选择');
    }
    if (titleMatches.length === 1) return titleMatches[0];
    throw new Error('候选流程已更新，请刷新后重新选择');
  });
  return {
    candidates: selectedCandidates,
    ...(other ? { customNote: other } : {}),
  };
}

export function isCompleteProcessCandidate(candidate: CandidateBusinessProcess): boolean {
  return Boolean(candidate.title.trim() && candidate.trigger.trim() && candidate.actors.length
    && candidate.steps.length && candidate.outputs.length);
}

function requireAnswers(
  response: JsonObject,
  fields: Array<[string, string]>,
  message: string,
): Record<string, string> {
  const answers = answerMap(response);
  const values: Record<string, string> = {};
  for (const [id, label] of fields) {
    const value = answers.get(id)?.trim() ?? '';
    if (!value) throw new Error(`${message}：请填写${label}`);
    if (value.length > 4_000) throw new Error(`${label}不能超过 4000 字`);
    values[id] = value;
  }
  return values;
}

function listValues(value: string): string[] {
  if (EMPTY_ANSWERS.has(value.trim())) return [];
  return value.split(/(?:\r?\n|[；;、，,])/u)
    .map((item) => item.trim().replace(/^[-*•]\s*/u, ''))
    .filter((item) => item && !EMPTY_ANSWERS.has(item));
}

function stepValues(value: string): string[] {
  return value.split(/(?:\r?\n|\s*(?:→|->)\s*|[；;])/u)
    .map((item) => item.trim().replace(/^\s*(?:第?[一二三四五六七八九十百]+|\d+)\s*[.、)）-]?\s*/u, ''))
    .filter(Boolean);
}

export function parseProcessInterviewCoreResponse(response: JsonObject): ProcessInterviewCore {
  const values = requireAnswers(response, [
    ['process_title', '流程名称'], ['trigger', '触发条件'], ['actors', '参与角色'], ['outputs', '流程输出'],
  ], '请完整填写核心流程');
  const actors = listValues(values.actors);
  const outputs = listValues(values.outputs);
  if (!actors.length) throw new Error('请完整填写核心流程：请填写参与角色');
  if (!outputs.length) throw new Error('请完整填写核心流程：请填写流程输出');
  return {
    title: values.process_title,
    trigger: values.trigger,
    actors,
    outputs,
  };
}

export function readProcessInterviewCore(value: unknown): ProcessInterviewCore {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('流程访谈上下文已失效，请重新开始');
  }
  const raw = value as Record<string, unknown>;
  const title = typeof raw.title === 'string' ? raw.title.trim() : '';
  const trigger = typeof raw.trigger === 'string' ? raw.trigger.trim() : '';
  const actors = Array.isArray(raw.actors) ? raw.actors.map(String).map((item) => item.trim()).filter(Boolean) : [];
  const outputs = Array.isArray(raw.outputs) ? raw.outputs.map(String).map((item) => item.trim()).filter(Boolean) : [];
  if (!title || !trigger || !actors.length || !outputs.length) {
    throw new Error('流程访谈上下文已失效，请重新开始');
  }
  return { title, trigger, actors, outputs };
}

export function parseProcessInterviewDetailsResponse(response: JsonObject): ProcessInterviewDetails {
  const answers = answerMap(response);
  const steps = answers.get('steps')?.trim() ?? '';
  if (!steps) throw new Error('请填写流程步骤');
  const values = Object.fromEntries(['inputs', 'steps', 'exceptions', 'handoffs'].map((id) => {
    const value = answers.get(id)?.trim() ?? '';
    if (value.length > 4_000) throw new Error('单项回答不能超过 4000 字');
    return [id, value];
  }));
  const stepLines = stepValues(steps);
  if (!stepLines.length) throw new Error('请完整填写流程步骤：请填写流程步骤');
  if (stepLines.length > 50) throw new Error('流程步骤不能超过 50 步');
  return {
    inputs: listValues(values.inputs),
    stepLines,
    exceptions: listValues(values.exceptions),
    handoffs: listValues(values.handoffs),
  };
}

function normalizeStep(line: string, index: number) {
  const decision = /^判断\s*[:：]?/u.test(line) || /是否|如果|若|条件|检查|校验/u.test(line);
  const label = line.replace(/^判断\s*[:：]?\s*/u, '').trim();
  return { id: `step_${index + 1}`, label, kind: decision ? 'decision' as const : 'action' as const };
}

function processFromParts(
  id: string,
  core: ProcessInterviewCore,
  details: ProcessInterviewDetails,
): InterviewOutcome['processes'][number] {
  const steps = details.stepLines.map(normalizeStep);
  return {
    id,
    title: core.title,
    trigger: core.trigger,
    actors: core.actors,
    inputs: details.inputs,
    steps,
    transitions: steps.slice(1).map((step, index) => ({ from: steps[index].id, to: step.id })),
    exceptions: details.exceptions,
    outputs: core.outputs,
    handoffs: details.handoffs,
  };
}

export function buildInterviewOutcome(
  core: ProcessInterviewCore,
  details: ProcessInterviewDetails,
): InterviewOutcome {
  const processes = [processFromParts('process_1', core, details)];
  return {
    summary: '已梳理核心业务流程的触发、角色、输入、步骤、异常、输出和交接。',
    processes,
  };
}

export function buildInterviewOutcomeFromCandidates(
  candidates: CandidateBusinessProcess[],
): InterviewOutcome {
  if (!candidates.length) throw new Error('请至少选择一个核心业务流程');
  const processes = candidates.map((candidate, index) => {
    const normalized = isCompleteProcessCandidate(candidate)
      ? candidate
      : normalizeGeneratedProcessDraft(candidate as unknown as Record<string, unknown>);
    return processFromParts(`process_${index + 1}`, {
      title: normalized.title,
      trigger: normalized.trigger,
      actors: normalized.actors,
      outputs: normalized.outputs,
    }, {
      inputs: normalized.inputs,
      stepLines: normalized.steps,
      exceptions: normalized.exceptions,
      handoffs: normalized.handoffs,
    });
  });
  return {
    summary: `已从资料候选中确认 ${processes.length} 个核心业务流程。`,
    processes,
  };
}

function mermaidText(value: string): string {
  return value.replace(/["\[\]{}]/gu, ' ').replace(/\s+/gu, ' ').trim();
}

function mermaidFor(outcome: InterviewOutcome, index: number): string {
  const process = outcome.processes[index];
  const lines = [
    'flowchart TD',
    `  start(["${mermaidText(process.trigger || '流程开始')}"])`,
    ...process.steps.map((step) => step.kind === 'decision'
      ? `  ${step.id}{"${mermaidText(step.label)}"}`
      : `  ${step.id}["${mermaidText(step.label)}"]`),
    `  finish(["${mermaidText(process.outputs.join('、') || '流程完成')}"])`,
  ];
  if (process.steps.length) {
    lines.push(`  start --> ${process.steps[0].id}`);
    process.transitions.forEach((transition) => {
      lines.push(`  ${transition.from} --> ${transition.to}`);
    });
    lines.push(`  ${process.steps.at(-1)!.id} --> finish`);
  } else {
    lines.push('  start --> finish');
  }
  return `${lines.join('\n')}\n`;
}

export function buildProcessDiagramFiles(outcome: InterviewOutcome): Record<string, string> {
  const rows = outcome.processes.map((process, index) => (
    `- [${process.title}](process_${index + 1}.mmd)`
  ));
  const files: Record<string, string> = {
    'diagrams/index.md': [
      '---', 'resource: diagram_index', 'key: diagrams', 'title: 流程图目录', '---', '',
      '# 流程图目录', '', ...rows, '',
    ].join('\n'),
  };
  outcome.processes.forEach((_process, index) => {
    files[`diagrams/process_${index + 1}.mmd`] = mermaidFor(outcome, index);
  });
  return files;
}
