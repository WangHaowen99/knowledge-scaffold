import type { FlowchartOutput } from '../../src/authoring/workflow-extras.js';

export interface ConfirmedProcessFixture {
  id: string;
  title: string;
  trigger?: string;
  actors?: string[];
  inputs?: string[];
  steps?: string[];
  exceptions?: string[];
  outputs?: string[];
  handoffs?: string[];
}

export function operationTargetId(diagramId: string): string {
  return `process_${diagramId}`;
}

export function operationTargetQuestion(diagramTitle: string): string {
  return `按照已确认的「${diagramTitle}」流程完成业务处理并返回明确结果`;
}

export function confirmedFlowchart(processes: readonly ConfirmedProcessFixture[]): FlowchartOutput {
  if (!processes.length) throw new Error('测试必须提供至少一个已确认流程');

  return {
    diagrams: processes.map((process) => {
      const steps = process.steps?.length ? process.steps : [process.title];
      const startId = `${process.id}:start`;
      const endId = `${process.id}:end`;
      const actionIds = steps.map((_, index) => `${process.id}:action:${index + 1}`);
      const nodeIds = [startId, ...actionIds, endId];

      return {
        id: process.id,
        title: process.title,
        processMeta: {
          trigger: process.trigger ?? `开始${process.title}`,
          actors: process.actors ?? ['业务人员'],
          inputs: process.inputs ?? [`${process.title}输入`],
          exceptions: process.exceptions ?? [],
          outputs: process.outputs ?? [`${process.title}结果`],
          handoffs: process.handoffs ?? [],
        },
        nodes: [
          { id: startId, label: process.trigger ?? `开始${process.title}`, kind: 'start' as const },
          ...steps.map((step, index) => ({ id: actionIds[index], label: step, kind: 'action' as const })),
          { id: endId, label: (process.outputs ?? [`${process.title}结果`])[0], kind: 'end' as const },
        ],
        edges: nodeIds.slice(0, -1).map((from, index) => ({
          id: `${process.id}:edge:${index + 1}`,
          from,
          to: nodeIds[index + 1],
          kind: 'sequence' as const,
        })),
      };
    }),
  };
}
