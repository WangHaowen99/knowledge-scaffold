import type { JsonObject } from '../authoring/types.js';
import { generateModelingBriefFromGoal } from './modeling-goal-generator.js';
import { parseModelingBriefResponse, type ModelingBrief } from './modeling-brief.js';

export class ModelingGoalInputError extends Error {
  readonly code = 'INVALID_GOAL_INPUT';

  constructor(message: string) {
    super(message);
    this.name = 'ModelingGoalInputError';
  }
}

type ModelingBriefGenerator = (goal: string) => Promise<ModelingBrief>;

function inputError(error: unknown): ModelingGoalInputError {
  return error instanceof ModelingGoalInputError
    ? error
    : new ModelingGoalInputError(error instanceof Error ? error.message : '请完整填写建模目标');
}

function goalText(response: JsonObject): string | null {
  if (!Array.isArray(response.answers)) {
    throw new ModelingGoalInputError('请完整填写建模目标');
  }
  const answer = response.answers.find((item) => (
    item && typeof item === 'object' && !Array.isArray(item)
      && (item as Record<string, unknown>).questionId === 'goal_text'
  ));
  if (!answer) return null;
  const record = answer as Record<string, unknown>;
  const selected = Array.isArray(record.selected)
    ? record.selected.map(String).map((item) => item.trim()).find(Boolean) ?? ''
    : '';
  const text = String(record.other ?? selected).trim();
  if (record.skipped === true || !text) throw new ModelingGoalInputError('请填写建模目标');
  if (text.length > 2_000) throw new ModelingGoalInputError('建模目标不能超过 2000 字');
  return text;
}

export async function resolveModelingBriefSubmission(
  response: JsonObject,
  generate: ModelingBriefGenerator = async (goal) => generateModelingBriefFromGoal({ goal }),
): Promise<ModelingBrief> {
  let text: string | null;
  try {
    text = goalText(response);
    if (text === null) return parseModelingBriefResponse(response);
  } catch (error) {
    throw inputError(error);
  }
  return generate(text);
}
