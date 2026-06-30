import { HttpClient } from '../../transport/http.js';
import type {
  CreateSessionRequest,
  GenerateTitleRequest,
  GenerateTitleResponse,
  SessionDetail,
  SessionListResponse,
  SessionMessagesResponse,
  SessionSummary,
} from '../../types/agentic.js';

export class AgenticSessionsApi {
  constructor(private readonly http: HttpClient) {}

  async list(options: { signal?: AbortSignal } = {}): Promise<SessionSummary[]> {
    const response = await this.http.get<SessionListResponse>('/agentic/sessions', {
      signal: options.signal,
    });
    return response.sessions;
  }

  create(request: CreateSessionRequest = {}, options: { signal?: AbortSignal } = {}): Promise<SessionSummary> {
    return this.http.post<SessionSummary, CreateSessionRequest>('/agentic/sessions', {
      json: request,
      signal: options.signal,
    });
  }

  get(sessionId: string, options: { signal?: AbortSignal } = {}): Promise<SessionDetail> {
    return this.http.get<SessionDetail>(`/agentic/sessions/${encodeURIComponent(sessionId)}`, {
      signal: options.signal,
    });
  }

  async messages(
    sessionId: string,
    options: { limit?: number; signal?: AbortSignal } = {},
  ): Promise<SessionMessagesResponse['messages']> {
    const response = await this.http.get<SessionMessagesResponse>(
      `/agentic/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        query: { limit: options.limit },
        signal: options.signal,
      },
    );
    return response.messages;
  }

  delete(sessionId: string, options: { signal?: AbortSignal } = {}): Promise<{ success: boolean }> {
    return this.http.delete<{ success: boolean }>(`/agentic/sessions/${encodeURIComponent(sessionId)}`, {
      signal: options.signal,
    });
  }

  generateTitle(
    sessionId: string,
    request: GenerateTitleRequest,
    options: { signal?: AbortSignal } = {},
  ): Promise<GenerateTitleResponse> {
    return this.http.post<GenerateTitleResponse, GenerateTitleRequest>(
      `/agentic/sessions/${encodeURIComponent(sessionId)}/generate-title`,
      {
        json: request,
        signal: options.signal,
      },
    );
  }
}
