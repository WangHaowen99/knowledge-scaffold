import { AgenticRunsApi } from '../protocols/agentic/runs.js';
import { AgenticSessionsApi } from '../protocols/agentic/sessions.js';
import type { AbortableAsyncIterable } from '../streams/sse.js';
import type {
  AgentMode,
  AgenticRunContext,
  CreateSessionRequest,
  SessionDetail,
  SessionMessage,
  SessionSummary,
} from '../types/agentic.js';
import type { NormalizedStreamEvent } from '../types/events.js';

export interface ChatSendRequest {
  sessionId: string;
  message: string;
  runId?: string;
  context?: AgenticRunContext;
  agentMode?: AgentMode;
  selectedOntologyId?: string;
  signal?: AbortSignal;
}

const buildRunContext = (request: ChatSendRequest): AgenticRunContext => {
  const context: AgenticRunContext = { ...(request.context || {}) };

  if (request.agentMode) {
    context.agent_mode = request.agentMode;
  }
  if (request.selectedOntologyId) {
    context.selected_ontology_id = request.selectedOntologyId;
  }

  return context;
};

export class ChatFacade {
  constructor(
    private readonly runs: AgenticRunsApi,
    private readonly sessions: AgenticSessionsApi,
  ) {}

  send(request: ChatSendRequest): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    return this.runs.stream(
      {
        session_id: request.sessionId,
        request: request.message,
        run_id: request.runId,
        context: buildRunContext(request),
      },
      { signal: request.signal },
    );
  }

  listSessions(options: { signal?: AbortSignal } = {}): Promise<SessionSummary[]> {
    return this.sessions.list(options);
  }

  createSession(request: CreateSessionRequest = {}, options: { signal?: AbortSignal } = {}): Promise<SessionSummary> {
    return this.sessions.create(request, options);
  }

  getSession(sessionId: string, options: { signal?: AbortSignal } = {}): Promise<SessionDetail> {
    return this.sessions.get(sessionId, options);
  }

  getMessages(
    sessionId: string,
    options: { limit?: number; signal?: AbortSignal } = {},
  ): Promise<SessionMessage[]> {
    return this.sessions.messages(sessionId, options);
  }
}
