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
