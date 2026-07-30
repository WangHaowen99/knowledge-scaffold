import type { JsonObject } from '../authoring/types.js';
import type { ModelingBrief } from '../types.js';

export type { ModelingBrief } from '../types.js';

const REQUIRED = [
  ['problem', '核心问题'],
  ['target_users', '目标用户'],
  ['scenarios', '典型场景'],
  ['success_criteria', '成功标准'],
] as const;

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

export function parseModelingBriefResponse(response: JsonObject): ModelingBrief {
  if (!Array.isArray(response.answers)) throw new Error('请完整填写建模目标');
  const answers = new Map<string, string>();
  response.answers.forEach((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const answer = value as Record<string, unknown>;
    const id = typeof answer.questionId === 'string' ? answer.questionId.trim() : '';
    if (id && !answers.has(id)) answers.set(id, answerText(answer));
  });
  const values = Object.fromEntries(REQUIRED.map(([id, label]) => {
    const text = answers.get(id) ?? '';
    if (!text) throw new Error(`请填写${label}`);
    if (text.length > 2_000) throw new Error(`${label}不能超过 2000 字`);
    return [id, text];
  }));
  return {
    problem: values.problem,
    targetUsers: values.target_users,
    scenarios: values.scenarios,
    successCriteria: values.success_criteria,
  };
}

export function summarizeModelingBrief(brief: ModelingBrief): string {
  return [
    `核心问题：${brief.problem}`,
    `目标用户：${brief.targetUsers}`,
    `典型场景：${brief.scenarios}`,
    `成功标准：${brief.successCriteria}`,
  ].join('\n');
}
