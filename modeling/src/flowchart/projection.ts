import { createHash } from 'node:crypto';

import type { InterviewOutcome, InterviewProcessStep } from '../types.js';
import type {
  FlowchartDiagram,
  FlowchartDocument,
  FlowchartEdge,
  FlowchartNode,
} from './types.js';
import { diagramFileEntries } from './file-path.js';

export function documentToInterviewOutcome(document: FlowchartDocument): InterviewOutcome {
  const processes = document.diagrams.map((diagram) => {
    const steps = diagram.nodes
      .filter((node): node is FlowchartNode & { kind: 'action' | 'decision' } => (
        node.kind === 'action' || node.kind === 'decision'
      ))
      .map<InterviewProcessStep>((node) => ({
        id: node.id,
        label: node.label,
        kind: node.kind,
        ...(node.detail ? { detail: node.detail } : {}),
      }));
    const stepIds = new Set(steps.map((step) => step.id));
    const transitions = diagram.edges
      .filter((edge) => stepIds.has(edge.from) && stepIds.has(edge.to))
      .map((edge) => ({
        from: edge.from,
        to: edge.to,
        ...(edgeText(edge) ? { label: edgeText(edge) } : {}),
      }));
    const starts = diagram.nodes.filter((node) => node.kind === 'start');
    const ends = diagram.nodes.filter((node) => node.kind === 'end');
    const trigger = starts.map((node) => node.label.trim()).filter(Boolean).join('；');
    const outputs = ends.map((node) => node.label.trim()).filter(Boolean);
    return {
      id: diagram.id,
      title: diagram.title,
      trigger,
      actors: [...(diagram.processMeta?.actors ?? [])],
      inputs: [...(diagram.processMeta?.inputs ?? [])],
      steps,
      transitions,
      exceptions: [...(diagram.processMeta?.exceptions ?? [])],
      outputs,
      handoffs: [...(diagram.processMeta?.handoffs ?? [])],
    };
  });
  return {
    summary: processes.map((process) => process.title).filter(Boolean).join('；'),
    processes,
  };
}

export function renderDiagramFiles(document: FlowchartDocument): Record<string, string> {
  const entries = diagramFileEntries(document.diagrams);
  const rows = entries.map(({ path, title }) => (
    `- [${markdownLinkText(title)}](<${path.slice('diagrams/'.length)}>)`
  ));
  const files: Record<string, string> = {
    'diagrams/index.md': [
      '---', 'resource: diagram_index', 'key: diagrams', 'title: 流程图目录', '---', '',
      '# 流程图目录', '', ...rows, '',
    ].join('\n'),
    'diagrams/processes.json': `${JSON.stringify(document, null, 2)}\n`,
  };
  entries.forEach(({ diagram, path }) => {
    files[path] = renderMermaid(diagram);
  });
  return files;
}

function markdownLinkText(value: string): string {
  return value.replace(/[\\\[\]]/gu, '\\$&');
}

function renderMermaid(diagram: FlowchartDiagram): string {
  const nodeIds = new Map(diagram.nodes.map((node) => [node.id, mermaidNodeId(node.id)]));
  const direction = diagram.layout?.direction === 'TB' ? 'TD' : diagram.layout?.direction ?? 'TD';
  const lines = [`flowchart ${direction}`];
  for (const node of diagram.nodes) {
    const id = nodeIds.get(node.id)!;
    const label = mermaidText(node.label || '未命名节点');
    if (node.kind === 'start' || node.kind === 'end') lines.push(`  ${id}(["${label}"])`);
    else if (node.kind === 'decision') lines.push(`  ${id}{"${label}"}`);
    else if (node.kind === 'object' || node.kind === 'data') lines.push(`  ${id}[("${label}")]`);
    else lines.push(`  ${id}["${label}"]`);
  }
  for (const edge of diagram.edges) {
    const from = nodeIds.get(edge.from);
    const to = nodeIds.get(edge.to);
    if (!from || !to) continue;
    const arrow = edge.kind === 'exception' || edge.kind === 'data' ? '-.->' : '-->';
    const text = edgeText(edge);
    lines.push(text
      ? `  ${from} ${arrow}|"${mermaidText(text)}"| ${to}`
      : `  ${from} ${arrow} ${to}`);
  }
  return `${lines.join('\n')}\n`;
}

function edgeText(edge: Pick<FlowchartEdge, 'label' | 'conditionText'>): string {
  return [...new Set([edge.label, edge.conditionText].map((text) => text?.trim()).filter(Boolean))]
    .join(' / ');
}

function mermaidNodeId(value: string): string {
  if (/^[A-Za-z_][A-Za-z0-9_-]*$/u.test(value)) return value;
  return `node_${createHash('sha256').update(value).digest('hex').slice(0, 16)}`;
}

function mermaidText(value: string): string {
  return value
    .replace(/["\[\]{}|]/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim();
}
