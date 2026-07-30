import type { JsonObject } from '../authoring/types.js';
import type {
  AnalysisResult,
  CandidateBusinessProcess,
} from '../types.js';
import type { ModelingBrief } from './modeling-brief.js';

export type ChoiceQuestion = JsonObject & {
  id: string;
  kind: 'single' | 'multi';
  q: string;
  required: true;
  allowOther: true;
  otherLabel?: string;
  otherPlaceholder?: string;
  options: JsonObject[];
  defaultValue?: string[];
};

export type TextQuestion = JsonObject & {
  id: string;
  kind: 'text';
  q: string;
  required: true;
  allowOther: true;
  options: [];
  other?: string;
};

interface OptionInput {
  value: string;
  label?: string;
  description?: string;
}

function compact(values: Array<string | undefined>, max = 4): string[] {
  return [...new Set(values.map((value) => value?.trim() ?? '').filter(Boolean))].slice(0, max);
}

function options(values: OptionInput[], fallback: OptionInput): JsonObject[] {
  const seen = new Set<string>();
  const result = values.flatMap((item) => {
    const value = item.value.trim().slice(0, 4_000);
    if (!value || seen.has(value)) return [];
    seen.add(value);
    return [{
      value,
      label: (item.label?.trim() || value).slice(0, 180),
      ...(item.description?.trim() ? { description: item.description.trim().slice(0, 400) } : {}),
    } as JsonObject];
  }).slice(0, 4);
  return result.length ? result : [{
    value: fallback.value,
    label: fallback.label ?? fallback.value,
    ...(fallback.description ? { description: fallback.description } : {}),
  }];
}

function withDefault(
  values: JsonObject[],
  defaultValue?: string,
): { options: JsonObject[]; defaultValue?: string[] } {
  const value = defaultValue?.trim();
  if (!value) return { options: values };
  const alreadyIncluded = values.some((item) => item.value === value);
  return {
    options: alreadyIncluded
      ? values
      : [{ value, label: `保留当前答案：${value}`.slice(0, 180) }, ...values].slice(0, 4),
    defaultValue: [value],
  };
}

function withDefaults(
  values: JsonObject[],
  defaults: string[] | undefined,
): { options: JsonObject[]; defaultValue?: string[] } {
  const selected = compact(defaults ?? [], 4);
  if (!selected.length) return { options: values };
  const existing = new Set(values.map((item) => String(item.value ?? '')));
  const additions = selected
    .filter((value) => !existing.has(value))
    .map((value) => ({ value, label: `保留当前答案：${value}`.slice(0, 180) } as JsonObject));
  return { options: [...additions, ...values].slice(0, 4), defaultValue: selected };
}

function question(input: {
  id: string;
  kind: 'single' | 'multi';
  q: string;
  options: JsonObject[];
  defaultValue?: string[];
  otherLabel?: string;
  otherPlaceholder?: string;
}): ChoiceQuestion {
  return { ...input, required: true, allowOther: true } as ChoiceQuestion;
}

export function buildModelingBriefQuestions(
  analysis: AnalysisResult | null | undefined,
  defaults: ModelingBrief | null = null,
): Array<ChoiceQuestion | TextQuestion> {
  if (!analysis) {
    const other = defaults ? [
      `核心问题：${defaults.problem}`,
      `目标用户：${defaults.targetUsers}`,
      `典型场景：${defaults.scenarios}`,
      `成功标准：${defaults.successCriteria}`,
    ].join('\n') : '';
    return [{
      id: 'goal_text',
      kind: 'text',
      q: '请描述本次建模目标：要解决什么问题、谁来使用、在哪些场景使用，以及期望得到什么结果？',
      required: true,
      allowOther: true,
      options: [],
      ...(other ? { other } : {}),
    }];
  }
  const goals = analysis?.candidateGoals ?? [];
  const problem = withDefault(options(goals.map((goal) => ({
    value: goal.problem, label: goal.title, description: goal.rationale,
  })), {
    value: analysis?.domainOverview?.trim() || '围绕现有资料形成可执行、可追溯的业务知识',
    label: '采用资料分析形成的目标假设',
  }), defaults?.problem);
  const targetUsers = withDefault(options(compact(goals.flatMap((goal) => goal.targetUsers)).map((value) => ({ value })), {
    value: '业务人员', label: '业务人员', description: '由后续访谈进一步明确具体角色',
  }), defaults?.targetUsers);
  const scenarios = withDefault(options(compact(goals.flatMap((goal) => goal.scenarios)).map((value) => ({ value })), {
    value: '日常业务处理与决策支持', label: '日常业务处理与决策支持',
  }), defaults?.scenarios);
  const successCriteria = withDefault(options(compact(goals.flatMap((goal) => goal.successCriteria)).map((value) => ({ value })), {
    value: '结论可追溯、流程可执行、相同输入得到一致结果',
    label: '可追溯、可执行且结果稳定',
  }), defaults?.successCriteria);

  return [
    question({ id: 'problem', kind: 'single', q: '以下哪个业务目标最接近您要解决的问题？', ...problem }),
    question({ id: 'target_users', kind: 'multi', q: '主要由哪些角色使用？可多选。', ...targetUsers }),
    question({ id: 'scenarios', kind: 'multi', q: '优先覆盖哪些典型场景？可多选。', ...scenarios }),
    question({ id: 'success_criteria', kind: 'multi', q: '用哪些标准判断建模成功？可多选。', ...successCriteria }),
  ];
}

function processScope(
  candidates: CandidateBusinessProcess[],
  selectedTitle?: string,
): CandidateBusinessProcess[] {
  const title = selectedTitle?.trim();
  const matched = title ? candidates.filter((item) => item.title.trim() === title) : [];
  return matched.length ? matched : candidates.slice(0, 3);
}

export function buildProcessInterviewQuestions(
  stage: 'core' | 'details',
  candidates: CandidateBusinessProcess[] = [],
  selectedTitle?: string,
  defaults: {
    title?: string;
    trigger?: string;
    actors?: string[];
    outputs?: string[];
    inputs?: string[];
    stepLines?: string[];
    exceptions?: string[];
    handoffs?: string[];
  } = {},
): ChoiceQuestion[] {
  const scoped = processScope(candidates, selectedTitle);
  if (stage === 'core') {
    const completeCandidates = scoped.filter((item) => (
      item.title.trim() && item.trigger.trim() && item.actors.length
      && item.steps.length && item.outputs.length
    ));
    if (completeCandidates.length) {
      const candidateOptions = completeCandidates.slice(0, 3).map((item) => ({
        value: item.candidateId?.trim() || item.title,
        label: item.title,
        description: [item.rationale, item.trigger, item.outputs.join('、')]
          .map((value) => value.trim()).filter(Boolean).join(' · ').slice(0, 400),
      } as JsonObject));
      return [question({
        id: 'selected_processes',
        kind: 'single',
        q: '先添加哪个核心业务流程？保存后可以继续添加其他流程。',
        options: candidateOptions,
        defaultValue: candidateOptions.slice(0, 1).map((item) => String(item.value)),
        otherLabel: '补充其他流程',
        otherPlaceholder: '请输入补充说明，下一步再确认流程名称和细节',
      })];
    }
    return [
      question({
        id: 'process_title', kind: 'single', q: '先梳理哪个核心业务流程？',
        ...withDefault(options(scoped.map((item) => ({
          value: item.title, label: item.title, description: item.rationale,
        })), { value: '核心业务处理流程', label: '核心业务处理流程' }), defaults.title),
      }),
      question({
        id: 'trigger', kind: 'single', q: '这个流程通常由什么事件触发？',
        ...withDefault(options(scoped.map((item) => ({
          value: item.trigger, label: item.trigger, description: `候选流程：${item.title}`,
        })), { value: '收到业务处理请求', label: '收到业务处理请求' }), defaults.trigger),
      }),
      question({
        id: 'actors', kind: 'multi', q: '哪些角色会参与？可多选。',
        ...withDefaults(options(compact(scoped.flatMap((item) => item.actors)).map((value) => ({ value })), {
          value: '业务人员', label: '业务人员',
        }), defaults.actors),
      }),
      question({
        id: 'outputs', kind: 'multi', q: '流程结束时应产生哪些结果？可多选。',
        ...withDefaults(options(compact(scoped.flatMap((item) => item.outputs)).map((value) => ({ value })), {
          value: '业务处理结果', label: '业务处理结果',
        }), defaults.outputs),
      }),
    ];
  }

  return [
    question({
      id: 'inputs', kind: 'multi', q: '流程开始时需要哪些输入？可多选。',
      ...withDefaults(options(compact(scoped.flatMap((item) => item.inputs)).map((value) => ({ value })), {
        value: '业务请求及相关资料', label: '业务请求及相关资料',
      }), defaults.inputs),
    }),
    question({
      id: 'steps', kind: 'single', q: '哪套步骤最接近实际流程？',
      ...withDefault(options(scoped.filter((item) => item.steps.length).map((item) => ({
        value: item.steps.join('\n'),
        label: `采用“${item.title}”的 ${item.steps.length} 个步骤`,
        description: item.steps.slice(0, 4).join(' → '),
      })), {
        value: '接收业务输入\n核验关键信息\n形成处理结果',
        label: '接收输入 → 核验信息 → 形成结果',
      }), defaults.stepLines?.join('\n')),
    }),
    question({
      id: 'exceptions', kind: 'multi', q: '需要覆盖哪些常见异常？可多选。',
      ...withDefaults(options(compact(scoped.flatMap((item) => item.exceptions)).map((value) => ({ value })), {
        value: '无', label: '暂未发现需要单独处理的异常',
      }), defaults.exceptions),
    }),
    question({
      id: 'handoffs', kind: 'multi', q: '有哪些跨角色或跨系统交接？可多选。',
      ...withDefaults(options(compact(scoped.flatMap((item) => item.handoffs)).map((value) => ({ value })), {
        value: '无', label: '没有额外交接',
      }), defaults.handoffs),
    }),
  ];
}
