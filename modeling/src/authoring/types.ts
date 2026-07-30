export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export const AUTHORING_INTERACTION_TYPES = [
  'question_batch',
  'change_plan',
  'decision',
  'artifact_review',
  'finding_review',
  'knowledge_conflict',
  'flowchart_review',
  'scenario_review',
] as const;

export type AuthoringInteractionType = typeof AUTHORING_INTERACTION_TYPES[number];
export type AuthoringRunStatus = 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled';
export type AuthoringInteractionStatus = 'pending' | 'answered' | 'cancelled';

export interface AuthoringRun {
  id: string;
  projectId: string;
  intent: string;
  status: AuthoringRunStatus;
  baseRevision?: string;
  state: JsonObject;
  result?: JsonObject;
  error?: string;
  createdAt: string;
  updatedAt: string;
  finishedAt?: string;
}

export interface AuthoringInteraction {
  id: string;
  projectId: string;
  runId: string;
  type: AuthoringInteractionType;
  status: AuthoringInteractionStatus;
  baseRevision?: string;
  payload: JsonObject;
  responseId?: string;
  response?: JsonObject;
  createdAt: string;
  respondedAt?: string;
}

export interface ProjectEvent {
  id: string;
  projectId: string;
  seq: number;
  type: string;
  runId?: string;
  interactionId?: string;
  payload: JsonObject;
  createdAt: string;
}

export type AuthoringCommand =
  | {
    kind: 'start_run';
    projectId: string;
    intent: string;
    status?: 'queued' | 'running';
    baseRevision?: string;
    state?: JsonObject;
  }
  | {
    kind: 'update_run';
    projectId: string;
    runId: string;
    status: AuthoringRunStatus;
    state?: JsonObject;
    result?: JsonObject;
    error?: string;
  }
  | {
    kind: 'open_interaction';
    projectId: string;
    runId: string;
    type: AuthoringInteractionType;
    baseRevision?: string;
    payload: JsonObject;
  }
  | {
    kind: 'respond_interaction';
    projectId: string;
    interactionId: string;
    responseId: string;
    response: JsonObject;
  }
  | {
    kind: 'publish_event';
    projectId: string;
    type: string;
    runId?: string;
    interactionId?: string;
    payload?: JsonObject;
  };

export type AuthoringCommandResult =
  | { kind: 'run'; run: AuthoringRun; events: ProjectEvent[] }
  | {
    kind: 'interaction';
    interaction: AuthoringInteraction;
    run: AuthoringRun;
    replayed: boolean;
    events: ProjectEvent[];
  }
  | { kind: 'event'; event: ProjectEvent };

export interface AuthoringSnapshot {
  projectId: string;
  activeRuns: AuthoringRun[];
  pendingInteractions: AuthoringInteraction[];
  lastEventSeq: number;
}

export interface ProjectEventPage {
  events: ProjectEvent[];
  nextSeq: number;
  hasMore: boolean;
}

export type AuthoringStateErrorCode =
  | 'PROJECT_NOT_FOUND'
  | 'RUN_NOT_FOUND'
  | 'INTERACTION_NOT_FOUND'
  | 'INVALID_INPUT'
  | 'INVALID_TRANSITION'
  | 'INTERACTION_CLOSED'
  | 'IDEMPOTENCY_CONFLICT';

export class AuthoringStateError extends Error {
  constructor(
    public readonly code: AuthoringStateErrorCode,
    message: string,
  ) {
    super(message);
    this.name = 'AuthoringStateError';
  }
}
