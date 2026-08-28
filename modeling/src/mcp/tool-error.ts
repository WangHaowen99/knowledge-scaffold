export class ModelingMcpToolError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly statusCode = 400,
    public readonly next: string[] = [],
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'ModelingMcpToolError';
  }
}

export type ModelingMcpToolPhase = 'input' | 'load' | 'validate' | 'persist';
export type ModelingMcpToolIssueLevel = 'blocking' | 'warning' | 'info';

export interface ModelingMcpToolResourceRef {
  type: string;
  id: string;
  key: string;
}

export interface ModelingMcpToolIssue {
  code: string;
  phase: ModelingMcpToolPhase;
  level: ModelingMcpToolIssueLevel;
  target: string;
  message: string;
  suggestion?: string;
}

export interface ModelingMcpToolDetails {
  phase?: ModelingMcpToolPhase;
  resource?: ModelingMcpToolResourceRef;
  reason_code?: string;
  field_errors?: Record<string, string>;
  issues?: ModelingMcpToolIssue[];
  changed_paths?: string[];
  affected_resources?: Array<{ type: string; key: string; relation?: string }>;
  historical_problem_count?: number;
  retryable?: boolean;
  [key: string]: unknown;
}

export interface PublicMcpToolError {
  code: string;
  message: string;
  next: string[];
  details: Record<string, unknown>;
}

export function publicMcpToolError(error: ModelingMcpToolError): PublicMcpToolError {
  return {
    code: error.code,
    message: error.message,
    next: [...error.next],
    details: structuredClone(error.details),
  };
}
