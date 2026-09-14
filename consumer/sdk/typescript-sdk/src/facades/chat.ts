// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { AgenticRunsApi } from '../protocols/agentic/runs.js';
import { AgenticSessionsApi } from '../protocols/agentic/sessions.js';
import type { AbortableAsyncIterable } from '../streams/sse.js';
import type {
  AgentMode,
  AgenticInteractionResponse,
  CreateSessionRequest,
  SessionDetail,
  SessionMessage,
  SessionSummary,
} from '../types/agentic.js';
import type { NormalizedStreamEvent } from '../types/events.js';

export interface ChatSendRequest {
  sessionId: string;
  message: string;
  agentMode?: AgentMode;
  llmRequestMode?: 'stream' | 'sync';
  modelProfileId?: string;
  arguments?: Record<string, unknown>;
  includeTrace?: boolean;
  selectedOntologyId?: string;
  interactionResponse?: AgenticInteractionResponse;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

const buildRunArguments = (request: ChatSendRequest): Record<string, unknown> | undefined => {
  const args = { ...(request.arguments || {}) };
  if (request.selectedOntologyId) {
    args.selected_ontology_id = request.selectedOntologyId;
  }
  return Object.keys(args).length ? args : undefined;
};

export class ChatFacade {
  constructor(
    private readonly runs: AgenticRunsApi,
    private readonly sessions: AgenticSessionsApi,
  ) {}

  async send(request: ChatSendRequest): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    const admission = await this.runs.create(
      {
        session_id: request.sessionId,
        request: request.message,
        agent_mode: request.agentMode,
        llm_request_mode: request.llmRequestMode,
        model_profile_id: request.modelProfileId,
        arguments: buildRunArguments(request),
        include_trace: request.includeTrace,
        interaction_response: request.interactionResponse,
      },
      {
        signal: request.signal,
        idempotencyKey: request.idempotencyKey,
      },
    );
    return this.runs.eventsFromLink(admission.links.events, {
      afterSeq: 0,
      signal: request.signal,
    });
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
