import type { FlowchartDiagram, FlowchartDocument } from '../flowchart/types.js';
import { normalizeFlowchartDocument } from '../flowchart/document.js';
import {
  StudioReadError,
  StudioReadService,
  type StudioWorkingPackage,
} from './studio-read-service.js';

export type StudioOperationFlowDefaultView = 'business-flow' | 'operation-steps' | 'empty';

export interface StudioOperationFlowIssue {
  code: 'DIAGRAM_DOCUMENT_INVALID' | 'DIAGRAM_NOT_FOUND' | 'FLOW_NODE_NOT_FOUND';
  message: string;
  reference?: string;
}

export interface StudioOperationFlowDiagram {
  id: string;
  title: string;
  document: FlowchartDiagram;
  focusedNodeIds: string[];
}

export interface StudioOperationFlowProjection {
  workingRevision: string;
  operationId: string;
  steps: string[];
  diagrams: StudioOperationFlowDiagram[];
  defaultView: StudioOperationFlowDefaultView;
  issues: StudioOperationFlowIssue[];
}

export class StudioOperationFlowService {
  constructor(private readonly workingPackage: StudioWorkingPackage) {}

  read(rawElementId: unknown): StudioOperationFlowProjection {
    const operation = new StudioReadService(this.workingPackage).detail(rawElementId).element;
    if (operation.kind !== 'operation') {
      throw new StudioReadError(
        'OPERATION_FLOW_TARGET_INVALID',
        '只有操作条目可以查看操作流程',
        400,
        { elementId: operation.id },
      );
    }

    const steps = stringArray(operation.values.flow);
    const evidence = stringArray(operation.values.evidence);
    const diagramIds = evidenceReferenceIds(evidence, 'diagram');
    const flowNodeIds = evidenceReferenceIds(evidence, 'flow-node');
    const issues: StudioOperationFlowIssue[] = [];
    const document = readFlowchartDocument(this.workingPackage.files, issues);
    const diagrams = document
      ? projectDiagrams(document, diagramIds, flowNodeIds, issues)
      : [];

    return {
      workingRevision: this.workingPackage.workingRevision,
      operationId: operation.id,
      steps,
      diagrams,
      defaultView: diagrams.length
        ? 'business-flow'
        : steps.length ? 'operation-steps' : 'empty',
      issues,
    };
  }
}

function readFlowchartDocument(
  files: Record<string, string>,
  issues: StudioOperationFlowIssue[],
): FlowchartDocument | null {
  const source = files['diagrams/processes.json'];
  if (!source) return { diagrams: [] };
  try {
    return normalizeFlowchartDocument(JSON.parse(source));
  } catch {
    issues.push({
      code: 'DIAGRAM_DOCUMENT_INVALID',
      message: '业务流程文档无法解析，当前仍可查看操作步骤',
    });
    return null;
  }
}

function projectDiagrams(
  document: FlowchartDocument,
  diagramIds: string[],
  flowNodeIds: string[],
  issues: StudioOperationFlowIssue[],
): StudioOperationFlowDiagram[] {
  const byId = new Map(document.diagrams.map((diagram) => [diagram.id, diagram]));
  const matchedNodeIds = new Set<string>();
  const diagrams = diagramIds.flatMap((diagramId): StudioOperationFlowDiagram[] => {
    const diagram = byId.get(diagramId);
    if (!diagram) {
      issues.push({
        code: 'DIAGRAM_NOT_FOUND',
        message: `关联的业务流程不存在：${diagramId}`,
        reference: `diagram:${diagramId}`,
      });
      return [];
    }
    const nodeIds = new Set(diagram.nodes.map((node) => node.id));
    const focusedNodeIds = flowNodeIds.filter((nodeId) => nodeIds.has(nodeId));
    focusedNodeIds.forEach((nodeId) => matchedNodeIds.add(nodeId));
    return [{
      id: diagram.id,
      title: diagram.title,
      document: diagram,
      focusedNodeIds,
    }];
  });
  for (const nodeId of flowNodeIds) {
    if (matchedNodeIds.has(nodeId)) continue;
    issues.push({
      code: 'FLOW_NODE_NOT_FOUND',
      message: `关联的流程节点无法定位：${nodeId}`,
      reference: `flow-node:${nodeId}`,
    });
  }
  return diagrams;
}

function evidenceReferenceIds(evidence: string[], kind: 'diagram' | 'flow-node'): string[] {
  const prefix = `${kind}:`;
  const result: string[] = [];
  const seen = new Set<string>();
  for (const entry of evidence) {
    const normalized = entry.trim();
    if (!normalized.startsWith(prefix)) continue;
    const id = normalized.slice(prefix.length).trim();
    if (!id || /[\s,;，；]/u.test(id) || seen.has(id)) continue;
    seen.add(id);
    result.push(id);
  }
  return result;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? '').trim()).filter(Boolean);
}
