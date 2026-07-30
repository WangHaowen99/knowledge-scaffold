export type FlowchartNodeKind = 'start' | 'end' | 'action' | 'decision' | 'object' | 'data';

export type FlowchartEdgeKind = 'sequence' | 'condition' | 'exception' | 'data';

export interface FlowchartNode {
  id: string;
  label: string;
  kind: FlowchartNodeKind;
  detail?: string;
}

export interface FlowchartEdge {
  id: string;
  from: string;
  to: string;
  kind: FlowchartEdgeKind;
  label?: string;
  conditionText?: string;
}

export interface FlowchartProcessMeta {
  trigger?: string;
  actors?: string[];
  inputs?: string[];
  exceptions?: string[];
  outputs?: string[];
  handoffs?: string[];
}

export interface FlowchartNodePosition {
  x: number;
  y: number;
}

export interface FlowchartLayout {
  direction?: 'TB' | 'LR';
  positions: Record<string, FlowchartNodePosition>;
}

export interface FlowchartDiagram {
  id: string;
  sourceCandidateId?: string;
  title: string;
  description?: string;
  processMeta?: FlowchartProcessMeta;
  nodes: FlowchartNode[];
  edges: FlowchartEdge[];
  layout?: FlowchartLayout;
}

export interface FlowchartDocument {
  diagrams: FlowchartDiagram[];
}

export type FlowchartValidationSeverity = 'error' | 'warning';

export type FlowchartValidationCode =
  | 'duplicate_diagram_id'
  | 'duplicate_node_id'
  | 'duplicate_edge_id'
  | 'missing_edge_endpoint'
  | 'orphan_layout_position'
  | 'empty_diagram'
  | 'missing_start'
  | 'missing_end'
  | 'disconnected_node'
  | 'unlabeled_decision_branch'
  | 'cycle';

export interface FlowchartValidationIssue {
  severity: FlowchartValidationSeverity;
  code: FlowchartValidationCode;
  message: string;
  diagramId?: string;
  nodeId?: string;
  edgeId?: string;
}
