import type { AnalysisResult, InterviewProcess, PackageSpec } from '../types.js';
import { normalizeFlowchartDocument } from '../flowchart/document.js';
import type {
  FlowchartDocument,
  FlowchartEdge,
  FlowchartNode,
} from '../flowchart/types.js';

export type { FlowchartDiagram, FlowchartEdge, FlowchartNode } from '../flowchart/types.js';
export type FlowchartOutput = FlowchartDocument;

type FlowchartEdgeDraft = Pick<FlowchartEdge, 'from' | 'to'>
  & Partial<Pick<FlowchartEdge, 'id' | 'kind' | 'label' | 'conditionText'>>;

export interface ScenarioDraft {
  name: string;
  input: string;
  expected: string;
  output: string;
  checks: Array<{
    kind: 'standard' | 'boundary' | 'exception';
    title: string;
    input: string;
    expected: string;
  }>;
  coverage: { rules: string[]; metrics: string[]; scripts: string[] };
}

export function buildFlowchart(
  spec: PackageSpec | undefined,
  analysis: AnalysisResult | undefined,
  interviewProcesses: InterviewProcess[] = [],
): FlowchartOutput {
  if (interviewProcesses.length) {
    return normalizeFlowchartDocument({
      diagrams: interviewProcesses.map((process, processIndex) => {
        const prefix = process.id || `process-${processIndex + 1}`;
        const stepIds = new Set(process.steps.map((step) => step.id));
        const outgoing = new Set(process.transitions
          .filter((transition) => stepIds.has(transition.from) && stepIds.has(transition.to))
          .map((transition) => transition.from));
        const firstStep = process.steps[0];
        const terminalSteps = process.steps.filter((step) => !outgoing.has(step.id));
        const startDetail = [...process.actors, ...process.inputs].filter(Boolean).join(' · ');
        const endDetail = process.handoffs.join('、');
        const description = [
          process.exceptions.length ? `异常：${process.exceptions.join('；')}` : '',
          process.handoffs.length ? `交接：${process.handoffs.join('；')}` : '',
        ].filter(Boolean).join('\n');
        const nodes: FlowchartNode[] = [
          {
            id: `start:${prefix}`, label: process.trigger || '流程开始', kind: 'start',
            ...(startDetail ? { detail: startDetail } : {}),
          },
          ...process.steps.map((step) => ({
            id: `step:${prefix}:${step.id}`, label: step.label, kind: step.kind,
            ...(step.detail ? { detail: step.detail } : {}),
          })),
          {
            id: `end:${prefix}`, label: process.outputs.join('、') || '流程完成', kind: 'end',
            ...(endDetail ? { detail: endDetail } : {}),
          },
        ];
        const transitions = process.transitions
          .filter((transition) => stepIds.has(transition.from) && stepIds.has(transition.to))
          .map((transition) => ({
            from: `step:${prefix}:${transition.from}`,
            to: `step:${prefix}:${transition.to}`,
            ...(transition.label ? { label: transition.label } : {}),
          }));
        const edges: FlowchartEdgeDraft[] = [
          ...(firstStep ? [{ from: `start:${prefix}`, to: `step:${prefix}:${firstStep.id}` }] : []),
          ...transitions,
          ...terminalSteps.map((step) => ({ from: `step:${prefix}:${step.id}`, to: `end:${prefix}` })),
        ];
        return {
          id: prefix,
          title: process.title,
          ...(description ? { description } : {}),
          nodes,
          edges: uniqueEdges(edges),
          trigger: process.trigger,
          actors: process.actors,
          inputs: process.inputs,
          exceptions: process.exceptions,
          outputs: process.outputs,
          handoffs: process.handoffs,
        };
      }),
    });
  }
  if (spec?.operations.length) {
    const objectsByKey = new Map(spec.objects.map((object) => [object.key, object]));
    return normalizeFlowchartDocument({
      diagrams: spec.operations.map((operation) => {
        const relatedObjects = (operation.relatedObjects ?? [])
          .map((key) => objectsByKey.get(key))
          .filter((object): object is NonNullable<typeof object> => Boolean(object));
        const steps = operation.flow.map((step) => step.trim()).filter(Boolean);
        if (!steps.length) steps.push(operation.title);

        const requiredInputs = operation.inputs.filter((input) => input.required);
        const inputs = requiredInputs.length ? requiredInputs : operation.inputs;
        const inputDetail = inputs.length
          ? inputs.map((input) => input.cnName || input.param).join('、')
          : '标准业务信息';
        const outputDetail = operation.outputs.length
          ? operation.outputs.map((output) => output.cnName || output.field).join('、')
          : operation.description || '处理结果';
        const firstStepId = `step:${operation.key}:0`;
        const nodes: FlowchartNode[] = [
          { id: `start:${operation.key}`, label: '输入', detail: inputDetail, kind: 'start' },
          ...relatedObjects.map((object) => ({
            id: `object:${operation.key}:${object.key}`,
            label: object.title,
            detail: object.description,
            kind: 'object' as const,
          })),
          ...steps.map((step, index) => ({
            id: `step:${operation.key}:${index}`,
            label: step,
            kind: isDecisionStep(step) ? 'decision' as const : 'action' as const,
          })),
          { id: `end:${operation.key}`, label: '输出', detail: outputDetail, kind: 'end' },
        ];
        const edges: FlowchartEdgeDraft[] = [
          { from: `start:${operation.key}`, to: firstStepId },
          ...relatedObjects.map((object) => ({
            from: `object:${operation.key}:${object.key}`,
            to: firstStepId,
            label: '数据',
          })),
          ...steps.slice(1).map((_, index) => ({
            from: `step:${operation.key}:${index}`,
            to: `step:${operation.key}:${index + 1}`,
          })),
          { from: `step:${operation.key}:${steps.length - 1}`, to: `end:${operation.key}` },
        ];
        return {
          id: operation.key,
          title: operation.title,
          description: operation.description,
          nodes,
          edges: uniqueEdges(edges),
        };
      }),
    });
  }

  const candidates = analysis?.candidateObjects ?? [];
  return normalizeFlowchartDocument({
    diagrams: candidates.length ? [{
      id: 'domain-objects',
      title: '领域对象',
      nodes: candidates.map((object, index) => ({
        id: `object:${index}`,
        label: object.name,
        kind: 'object',
      })),
      edges: [],
    }] : [],
  });
}

export function buildScenarioDrafts(spec: PackageSpec): ScenarioDraft[] {
  const rules = new Map((spec.rules ?? []).map((rule) => [rule.key, rule]));
  const metrics = new Map((spec.metrics ?? []).map((metric) => [metric.key, metric]));
  return spec.operations.map((operation) => {
    const requiredInputs = operation.inputs.filter((input) => input.required);
    const input = requiredInputs.length
      ? requiredInputs.map((item) => item.cnName || item.param).join('、')
      : '标准业务信息';
    const expected = operation.outputs.length
      ? operation.outputs.map((item) => item.cnName || item.field).join('、')
      : operation.description;
    const ruleItems = (operation.usesRules ?? []).map((key) => rules.get(key)).filter(Boolean);
    const metricItems = (operation.usesMetrics ?? []).map((key) => metrics.get(key)).filter(Boolean);
    const boundaryEvidence = metricItems.flatMap((metric) => [
      `${metric!.key}：${metric!.caliber}`,
      ...metric!.boundaries.map((boundary) => `${metric!.key}：${boundary}`),
    ]);
    const exceptionEvidence = [
      ...ruleItems.flatMap((rule) => (rule!.exceptions ?? []).map((exception) => `${rule!.key}：${exception}`)),
      ...(operation.notes ?? []),
    ].filter(Boolean);
    const boundaryExpected = boundaryEvidence.length
      ? `按以下指标口径和边界处理：${boundaryEvidence.join('；')}`
      : `缺少必需输入时不得猜测结果，应明确指出缺少的字段：${input}`;
    const exceptionExpected = exceptionEvidence.length
      ? `按以下例外和注意事项处理：${exceptionEvidence.join('；')}`
      : `不满足规则或输入冲突时停止正常流程，说明原因并给出可验证的处理建议`;
    return {
      name: operation.title,
      input,
      expected,
      output: operation.flow.join(' -> '),
      checks: [
        { kind: 'standard', title: '标准流程', input, expected },
        { kind: 'boundary', title: '边界与口径', input: `边界值、空值或临界条件下的 ${input}`, expected: boundaryExpected },
        { kind: 'exception', title: '异常与例外', input: `不满足规则、资料冲突或异常状态下的 ${input}`, expected: exceptionExpected },
      ],
      coverage: {
        rules: operation.usesRules ?? [],
        metrics: operation.usesMetrics ?? [],
        scripts: operation.script?.key ? [operation.script.key] : [],
      },
    };
  });
}

function uniqueEdges(edges: FlowchartEdgeDraft[]): FlowchartEdgeDraft[] {
  const seen = new Set<string>();
  return edges.filter((edge) => {
    const key = `${edge.from}\u0000${edge.to}\u0000${edge.label ?? ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isDecisionStep(step: string): boolean {
  return /判断|是否|若|如果|校验|检查|比较|满足|否则|风险/.test(step);
}
