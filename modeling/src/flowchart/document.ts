import { createHash } from 'node:crypto';

import type {
  FlowchartDiagram,
  FlowchartDocument,
  FlowchartEdgeKind,
  FlowchartEdge,
  FlowchartNodeKind,
  FlowchartNode,
  FlowchartProcessMeta,
  FlowchartValidationIssue,
} from './types.js';
import { diagramFilePath } from './file-path.js';

const NODE_KINDS = new Set<FlowchartNodeKind>([
  'start', 'end', 'action', 'decision', 'object', 'data',
]);
const EDGE_KINDS = new Set<FlowchartEdgeKind>([
  'sequence', 'condition', 'exception', 'data',
]);
const MAX_DIAGRAMS_PER_DOCUMENT = 30;

export function normalizeFlowchartDocument(value: unknown): FlowchartDocument {
  const root = asRecord(value, '流程图文档');
  const sourceDiagrams = requiredArray(root.diagrams, '流程图列表');
  if (sourceDiagrams.length > MAX_DIAGRAMS_PER_DOCUMENT) {
    throw new Error(`单份流程图文档最多只能包含 ${MAX_DIAGRAMS_PER_DOCUMENT} 张流程图`);
  }
  const diagrams = sourceDiagrams.map((diagram, index) => normalizeDiagram(diagram, index));
  return { diagrams };
}

export function validateFlowchartDocument(document: FlowchartDocument): FlowchartValidationIssue[] {
  const issues: FlowchartValidationIssue[] = [];
  const diagramIds = new Set<string>();
  const diagramPaths = new Set<string>();
  for (const diagram of document.diagrams) {
    if (diagramIds.has(diagram.id)) {
      issues.push(error('duplicate_diagram_id', '流程图 ID 重复', diagram.id));
    }
    diagramIds.add(diagram.id);
    try {
      const path = diagramFilePath(diagram.title).toLocaleLowerCase('en-US');
      if (diagramPaths.has(path)) {
        issues.push(error('duplicate_diagram_title', `流程名称重复：“${diagram.title}”`, diagram.id));
      }
      diagramPaths.add(path);
    } catch (cause) {
      issues.push(error(
        'invalid_diagram_title',
        cause instanceof Error ? cause.message : '流程名称不能用于流程图文件名',
        diagram.id,
      ));
    }
    issues.push(...validateDiagram(diagram));
  }
  return issues;
}

export function removeFlowchartNode(
  document: FlowchartDocument,
  diagramId: string,
  nodeId: string,
): FlowchartDocument {
  let diagramFound = false;
  let nodeFound = false;
  const diagrams = document.diagrams.map((diagram) => {
    if (diagram.id !== diagramId) return diagram;
    diagramFound = true;
    nodeFound = diagram.nodes.some((node) => node.id === nodeId);
    if (!nodeFound) return diagram;
    const positions = { ...(diagram.layout?.positions ?? {}) };
    delete positions[nodeId];
    return {
      ...diagram,
      nodes: diagram.nodes.filter((node) => node.id !== nodeId),
      edges: diagram.edges.filter((edge) => edge.from !== nodeId && edge.to !== nodeId),
      ...(diagram.layout ? { layout: { ...diagram.layout, positions } } : {}),
    };
  });
  if (!diagramFound) throw new Error(`流程图不存在：${diagramId}`);
  if (!nodeFound) throw new Error(`流程节点不存在：${nodeId}`);
  return { ...document, diagrams };
}

export function appendFlowchartDiagram(
  document: FlowchartDocument,
  diagram: FlowchartDiagram,
): FlowchartDocument {
  if (document.diagrams.length >= MAX_DIAGRAMS_PER_DOCUMENT) {
    throw new Error(`单份流程图文档最多只能包含 ${MAX_DIAGRAMS_PER_DOCUMENT} 张流程图`);
  }
  if (document.diagrams.some((item) => item.id === diagram.id)) {
    throw new Error(`流程图 ID 重复：${diagram.id}`);
  }
  const normalized = normalizeFlowchartDocument({ diagrams: [diagram] }).diagrams[0];
  return { ...document, diagrams: [...document.diagrams, normalized] };
}

export function appendBlankFlowchartDiagram(
  document: FlowchartDocument,
  input: { id: string; title?: string },
): FlowchartDocument {
  return appendFlowchartDiagram(document, {
    id: requiredText(input.id, '流程图 ID'),
    title: optionalText(input.title) ?? '未命名流程',
    nodes: [],
    edges: [],
    layout: { direction: 'TB', positions: {} },
  });
}

function validateDiagram(diagram: FlowchartDiagram): FlowchartValidationIssue[] {
  const issues: FlowchartValidationIssue[] = [];
  const nodeIds = new Set<string>();
  const edgeIds = new Set<string>();
  for (const node of diagram.nodes) {
    if (nodeIds.has(node.id)) {
      issues.push(error('duplicate_node_id', '节点 ID 重复', diagram.id, { nodeId: node.id }));
    }
    nodeIds.add(node.id);
  }
  for (const edge of diagram.edges) {
    if (edgeIds.has(edge.id)) {
      issues.push(error('duplicate_edge_id', '连线 ID 重复', diagram.id, { edgeId: edge.id }));
    }
    edgeIds.add(edge.id);
    if (!nodeIds.has(edge.from) || !nodeIds.has(edge.to)) {
      issues.push(error('missing_edge_endpoint', '连线引用了不存在的节点', diagram.id, { edgeId: edge.id }));
    }
  }
  for (const nodeId of Object.keys(diagram.layout?.positions ?? {})) {
    if (!nodeIds.has(nodeId)) {
      issues.push(error('orphan_layout_position', '布局引用了不存在的节点', diagram.id, { nodeId }));
    }
  }
  if (!diagram.nodes.length) {
    issues.push(warning('empty_diagram', '流程图尚未添加节点', diagram.id));
    return issues;
  }
  if (!diagram.nodes.some((node) => node.kind === 'start')) {
    issues.push(warning('missing_start', '流程图尚未标记开始节点', diagram.id));
  }
  if (!diagram.nodes.some((node) => node.kind === 'end')) {
    issues.push(warning('missing_end', '流程图尚未标记结束节点', diagram.id));
  }
  const connected = new Set(diagram.edges.flatMap((edge) => [edge.from, edge.to]));
  for (const node of diagram.nodes) {
    if (diagram.nodes.length > 1 && !connected.has(node.id)) {
      issues.push(warning('disconnected_node', '节点尚未连接到流程', diagram.id, { nodeId: node.id }));
    }
    if (node.kind === 'decision') {
      for (const edge of diagram.edges.filter((candidate) => candidate.from === node.id)) {
        if (!edge.label && !edge.conditionText) {
          issues.push(warning(
            'unlabeled_decision_branch',
            '判断节点的分支尚未填写文字或条件',
            diagram.id,
            { nodeId: node.id, edgeId: edge.id },
          ));
        }
      }
    }
  }
  if (hasCycle(diagram)) issues.push(warning('cycle', '流程图包含循环路径', diagram.id));
  return issues;
}

function hasCycle(diagram: FlowchartDiagram): boolean {
  const outgoing = new Map<string, string[]>();
  for (const node of diagram.nodes) outgoing.set(node.id, []);
  for (const edge of diagram.edges) {
    if (outgoing.has(edge.from) && outgoing.has(edge.to)) outgoing.get(edge.from)!.push(edge.to);
  }
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (nodeId: string): boolean => {
    if (visiting.has(nodeId)) return true;
    if (visited.has(nodeId)) return false;
    visiting.add(nodeId);
    if ((outgoing.get(nodeId) ?? []).some(visit)) return true;
    visiting.delete(nodeId);
    visited.add(nodeId);
    return false;
  };
  return diagram.nodes.some((node) => visit(node.id));
}

function error(
  code: FlowchartValidationIssue['code'],
  message: string,
  diagramId?: string,
  target: Pick<FlowchartValidationIssue, 'nodeId' | 'edgeId'> = {},
): FlowchartValidationIssue {
  return { severity: 'error', code, message, ...(diagramId ? { diagramId } : {}), ...target };
}

function warning(
  code: FlowchartValidationIssue['code'],
  message: string,
  diagramId?: string,
  target: Pick<FlowchartValidationIssue, 'nodeId' | 'edgeId'> = {},
): FlowchartValidationIssue {
  return { severity: 'warning', code, message, ...(diagramId ? { diagramId } : {}), ...target };
}

function normalizeDiagram(value: unknown, diagramIndex: number): FlowchartDiagram {
  const raw = asRecord(value, '流程图');
  const id = requiredText(raw.id, '流程图 ID');
  const nodes = requiredArray(raw.nodes, '流程图节点').map(normalizeNode);
  const edgeOccurrences = new Map<string, number>();
  const edges = requiredArray(raw.edges, '流程图连线').map((edge) => {
    const normalized = normalizeEdge(edge, id, edgeOccurrences);
    return normalized;
  });
  const processMeta = synchronizeProcessMeta(nodes, normalizeProcessMeta(raw));
  const diagram: FlowchartDiagram = {
    id,
    title: optionalText(raw.title) ?? `流程 ${diagramIndex + 1}`,
    nodes,
    edges,
  };
  const sourceCandidateId = optionalText(raw.sourceCandidateId);
  if (sourceCandidateId) diagram.sourceCandidateId = sourceCandidateId;
  const description = optionalText(raw.description);
  if (description) diagram.description = description;
  if (processMeta) diagram.processMeta = processMeta;
  if (raw.layout !== undefined) diagram.layout = normalizeLayout(raw.layout);
  return diagram;
}

function normalizeNode(value: unknown): FlowchartNode {
  const raw = asRecord(value, '流程图节点');
  const kind = requiredText(raw.kind, '节点类型') as FlowchartNodeKind;
  if (!NODE_KINDS.has(kind)) throw new Error(`节点类型不支持：${kind}`);
  const node: FlowchartNode = {
    id: requiredText(raw.id, '节点 ID'),
    label: optionalText(raw.label) ?? '',
    kind,
  };
  const detail = optionalText(raw.detail);
  if (detail) node.detail = detail;
  return node;
}

function normalizeEdge(
  value: unknown,
  diagramId: string,
  occurrences: Map<string, number>,
): FlowchartEdge {
  const raw = asRecord(value, '流程图连线');
  const from = requiredText(raw.from, '连线起点');
  const to = requiredText(raw.to, '连线终点');
  const label = optionalText(raw.label);
  const conditionText = optionalText(raw.conditionText);
  const kindValue = optionalText(raw.kind) ?? 'sequence';
  if (!EDGE_KINDS.has(kindValue as FlowchartEdgeKind)) {
    throw new Error(`连线类型不支持：${kindValue}`);
  }
  const identity = JSON.stringify([diagramId, from, to, label ?? '', conditionText ?? '', kindValue]);
  const occurrence = occurrences.get(identity) ?? 0;
  occurrences.set(identity, occurrence + 1);
  const edge: FlowchartEdge = {
    id: optionalText(raw.id) ?? stableEdgeId(identity, occurrence),
    from,
    to,
    kind: kindValue as FlowchartEdgeKind,
  };
  if (label) edge.label = label;
  if (conditionText) edge.conditionText = conditionText;
  return edge;
}

function stableEdgeId(identity: string, occurrence: number): string {
  return `edge_${createHash('sha256').update(identity).update(`\u0000${occurrence}`).digest('hex').slice(0, 16)}`;
}

function normalizeProcessMeta(raw: Record<string, unknown>): FlowchartProcessMeta | undefined {
  if (raw.processMeta === undefined) return undefined;
  const source = asRecord(raw.processMeta, '流程元数据');
  const meta: FlowchartProcessMeta = {};
  const trigger = optionalText(source.trigger);
  if (trigger) meta.trigger = trigger;
  for (const key of ['actors', 'inputs', 'exceptions', 'outputs', 'handoffs'] as const) {
    if (source[key] !== undefined) meta[key] = optionalTextArray(source[key], key);
  }
  return Object.keys(meta).length ? meta : undefined;
}

function synchronizeProcessMeta(
  nodes: FlowchartNode[],
  current: FlowchartProcessMeta | undefined,
): FlowchartProcessMeta | undefined {
  const startLabels = nodes
    .filter((node) => node.kind === 'start')
    .map((node) => node.label.trim())
    .filter(Boolean);
  const endLabels = nodes
    .filter((node) => node.kind === 'end')
    .map((node) => node.label.trim())
    .filter(Boolean);
  if (!current && !startLabels.length && !endLabels.length) return undefined;
  const { trigger: _trigger, outputs: _outputs, ...preserved } = current ?? {};
  return {
    ...preserved,
    ...(startLabels.length ? { trigger: startLabels.join('；') } : {}),
    outputs: endLabels,
  };
}

function normalizeLayout(value: unknown): FlowchartDiagram['layout'] {
  const raw = asRecord(value, '流程图布局');
  const directionValue = optionalText(raw.direction);
  let direction: 'TB' | 'LR' | undefined;
  if (directionValue === 'TB' || directionValue === 'LR') direction = directionValue;
  else if (directionValue) throw new Error('流程图布局方向不支持');
  const positionsRaw = asRecord(raw.positions ?? {}, '节点位置');
  const positions: Record<string, { x: number; y: number }> = {};
  for (const [nodeId, positionValue] of Object.entries(positionsRaw)) {
    const position = asRecord(positionValue, '节点位置');
    if (typeof position.x !== 'number' || !Number.isFinite(position.x)
      || typeof position.y !== 'number' || !Number.isFinite(position.y)) {
      throw new Error(`节点位置不正确：${nodeId}`);
    }
    positions[nodeId] = { x: position.x, y: position.y };
  }
  return { ...(direction ? { direction } : {}), positions };
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label}格式不正确`);
  return value as Record<string, unknown>;
}

function requiredArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${label}格式不正确`);
  return value;
}

function optionalTextArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${label}格式不正确`);
  return value.map((item) => requiredText(item, label));
}

function requiredText(value: unknown, label: string): string {
  const text = optionalText(value);
  if (!text) throw new Error(`${label}不能为空`);
  return text;
}

function optionalText(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') throw new Error('文本格式不正确');
  const text = value.trim();
  return text || undefined;
}
