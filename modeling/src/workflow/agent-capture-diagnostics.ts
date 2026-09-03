export type AgentCaptureOutcome =
  | 'captured'
  | 'model_error'
  | 'output_truncated'
  | 'tool_not_called'
  | 'tool_validation_failed';

export type NormalizedAgentStopReason = 'stop' | 'length' | 'toolUse' | 'error' | 'aborted';
export type AgentCaptureErrorKind =
  | 'aborted'
  | 'context_length_exceeded'
  | 'provider_error'
  | 'request_timeout';
export type AgentCaptureToolChoiceMode = 'named' | 'required' | 'auto';

export interface AgentCaptureAttemptDiagnostics {
  attempt: number;
  outcome: AgentCaptureOutcome;
  targetToolName: string;
  stopReason?: NormalizedAgentStopReason;
  targetToolStartedCount: number;
  targetToolSucceededCount: number;
  targetToolFailedCount: number;
  inputTokens: number;
  outputTokens: number;
  validationError?: string;
  modelApi?: string;
  errorKind?: AgentCaptureErrorKind;
  toolChoiceMode?: AgentCaptureToolChoiceMode;
}

const STOP_REASON_PRIORITY: Record<NormalizedAgentStopReason, number> = {
  stop: 1,
  toolUse: 2,
  aborted: 3,
  error: 4,
  length: 5,
};

const VALIDATION_HEADER = /^Validation failed for tool "[^"\n]{1,200}":$/u;
const VALIDATION_DETAIL = /^(?:  - .+|Unknown validation error)$/u;
const MAX_VALIDATION_LINES = 20;
const MAX_VALIDATION_SUMMARY_LENGTH = 2_000;

export function normalizeAgentStopReason(value: unknown): NormalizedAgentStopReason | undefined {
  if (typeof value !== 'string') return undefined;
  switch (value.trim().toLowerCase().replace(/[_-]/gu, '')) {
    case 'stop': return 'stop';
    case 'length': return 'length';
    case 'tooluse': return 'toolUse';
    case 'error': return 'error';
    case 'aborted': return 'aborted';
    default: return undefined;
  }
}

export function sanitizeToolValidationError(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const lines = value.replace(/\r\n?/gu, '\n').trim().split('\n');
  if (!VALIDATION_HEADER.test(lines[0] ?? '')) return undefined;

  const safeLines = [lines[0]];
  for (const line of lines.slice(1, MAX_VALIDATION_LINES + 1)) {
    if (!VALIDATION_DETAIL.test(line)) break;
    safeLines.push(line.slice(0, 500));
  }
  return safeLines.join('\n').slice(0, MAX_VALIDATION_SUMMARY_LENGTH);
}

export function extractToolValidationError(result: unknown): string | undefined {
  if (!result || typeof result !== 'object') return undefined;
  const content = (result as { content?: unknown }).content;
  if (!Array.isArray(content)) return undefined;
  for (const block of content) {
    if (!block || typeof block !== 'object') continue;
    const candidate = block as { type?: unknown; text?: unknown };
    if (candidate.type !== 'text') continue;
    const summary = sanitizeToolValidationError(candidate.text);
    if (summary) return summary;
  }
  return undefined;
}

function tokenCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : 0;
}

export class AgentCaptureAttemptTracker {
  private strongestStopReason?: NormalizedAgentStopReason;
  private targetToolStartedCount = 0;
  private targetToolSucceededCount = 0;
  private targetToolFailedCount = 0;
  private inputTokens = 0;
  private outputTokens = 0;
  private validationError?: string;

  constructor(
    private readonly attempt: number,
    private readonly targetToolName: string,
    acceptedToolNames: readonly string[] = [targetToolName],
  ) {
    this.acceptedToolNames = new Set([targetToolName, ...acceptedToolNames]);
  }

  private readonly acceptedToolNames: ReadonlySet<string>;

  observe(event: unknown): void {
    if (!event || typeof event !== 'object') return;
    const candidate = event as {
      type?: unknown;
      toolName?: unknown;
      isError?: unknown;
      result?: unknown;
      message?: {
        stopReason?: unknown;
        usage?: { input?: unknown; output?: unknown };
      };
    };

    if (candidate.type === 'turn_end') {
      const stopReason = normalizeAgentStopReason(candidate.message?.stopReason);
      if (stopReason && (!this.strongestStopReason
        || STOP_REASON_PRIORITY[stopReason] > STOP_REASON_PRIORITY[this.strongestStopReason])) {
        this.strongestStopReason = stopReason;
      }
      this.inputTokens += tokenCount(candidate.message?.usage?.input);
      this.outputTokens += tokenCount(candidate.message?.usage?.output);
      return;
    }

    if (typeof candidate.toolName !== 'string' || !this.acceptedToolNames.has(candidate.toolName)) return;
    if (candidate.type === 'tool_execution_start') {
      this.targetToolStartedCount += 1;
      return;
    }
    if (candidate.type !== 'tool_execution_end') return;

    if (candidate.isError === true) {
      this.targetToolFailedCount += 1;
      this.validationError ??= extractToolValidationError(candidate.result);
    } else {
      this.targetToolSucceededCount += 1;
    }
  }

  finish(captured: boolean, options: { modelError?: boolean } = {}): AgentCaptureAttemptDiagnostics {
    let stopReason = this.strongestStopReason;
    if (options.modelError && (!stopReason
      || STOP_REASON_PRIORITY.error > STOP_REASON_PRIORITY[stopReason])) {
      stopReason = 'error';
    }
    const outcome: AgentCaptureOutcome = stopReason === 'length'
      ? 'output_truncated'
      : stopReason === 'error'
        ? 'model_error'
        : this.targetToolFailedCount > 0
          ? 'tool_validation_failed'
          : captured && this.targetToolSucceededCount > 0
            ? 'captured'
            : 'tool_not_called';
    return {
      attempt: Number.isFinite(this.attempt) && this.attempt > 0 ? Math.floor(this.attempt) : 1,
      outcome,
      targetToolName: this.targetToolName,
      ...(stopReason ? { stopReason } : {}),
      targetToolStartedCount: this.targetToolStartedCount,
      targetToolSucceededCount: this.targetToolSucceededCount,
      targetToolFailedCount: this.targetToolFailedCount,
      inputTokens: this.inputTokens,
      outputTokens: this.outputTokens,
      ...(this.validationError ? { validationError: this.validationError } : {}),
    };
  }
}
