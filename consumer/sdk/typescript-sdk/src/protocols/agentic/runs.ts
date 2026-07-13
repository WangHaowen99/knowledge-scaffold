import { mapAbortableStream, type AbortableAsyncIterable } from '../../streams/sse.js';
import { HttpClient } from '../../transport/http.js';
import { normalizeAgenticSseMessage, type NormalizedStreamEvent } from '../../types/events.js';
import type {
  AgenticRunContext,
  AgenticRunStatus,
  CancelRunRequest,
  CancelRunResponse,
  StartAgenticRunRequest,
} from '../../types/agentic.js';

export interface AgenticRunStreamOptions {
  signal?: AbortSignal;
}

export interface AgenticRunEventsOptions extends AgenticRunStreamOptions {
  afterSeq?: number;
}

export class AgenticRunsApi {
  constructor(private readonly http: HttpClient) {}

  async stream(
    request: StartAgenticRunRequest,
    options: AgenticRunStreamOptions = {},
  ): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    const payload: StartAgenticRunRequest = {
      ...request,
      context: { ...(request.context || {}) },
    };
    const source = await this.http.stream('POST', '/agentic/runs/stream', {
      json: payload,
      signal: options.signal,
    });
    return mapAbortableStream(source, normalizeAgenticSseMessage);
  }

  status(runId: string, options: { signal?: AbortSignal } = {}): Promise<AgenticRunStatus> {
    return this.http.get<AgenticRunStatus>(`/agentic/runs/${encodeURIComponent(runId)}/status`, {
      signal: options.signal,
    });
  }

  async events(
    runId: string,
    options: AgenticRunEventsOptions = {},
  ): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    const cursor = Math.max(0, Math.trunc(options.afterSeq || 0));
    const source = await this.http.stream(
      'GET',
      `/agentic/runs/${encodeURIComponent(runId)}/events`,
      {
        query: { after_seq: cursor },
        headers: cursor ? { 'Last-Event-ID': String(cursor) } : undefined,
        signal: options.signal,
      },
    );
    return mapAbortableStream(source, normalizeAgenticSseMessage);
  }

  resume(
    runId: string,
    options: AgenticRunEventsOptions = {},
  ): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    return this.events(runId, options);
  }

  cancel(
    runId: string,
    request: CancelRunRequest = {},
    options: { signal?: AbortSignal } = {},
  ): Promise<CancelRunResponse> {
    return this.http.post<CancelRunResponse, CancelRunRequest>(
      `/agentic/runs/${encodeURIComponent(runId)}/cancel`,
      {
        json: request,
        signal: options.signal,
      },
    );
  }
}
