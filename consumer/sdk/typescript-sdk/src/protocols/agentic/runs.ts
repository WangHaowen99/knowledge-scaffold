import { mapAbortableStream, type AbortableAsyncIterable } from '../../streams/sse.js';
import { HttpClient } from '../../transport/http.js';
import { normalizeAgenticSseMessage, type NormalizedStreamEvent } from '../../types/events.js';
import type {
  AgenticRunStatus,
  CancelRunRequest,
  StartAgenticRunRequest,
  StartAgenticRunResponse,
} from '../../types/agentic.js';

export interface AgenticRunCreateOptions {
  signal?: AbortSignal;
  idempotencyKey?: string;
}

export interface AgenticRunEventsOptions {
  signal?: AbortSignal;
  afterSeq?: number;
}

export class AgenticRunsApi {
  constructor(private readonly http: HttpClient) {}

  create(
    request: StartAgenticRunRequest,
    options: AgenticRunCreateOptions = {},
  ): Promise<StartAgenticRunResponse> {
    const payload: StartAgenticRunRequest = {
      ...request,
      arguments: request.arguments ? { ...request.arguments } : undefined,
    };
    return this.http.post<StartAgenticRunResponse, StartAgenticRunRequest>(
      '/agentic/runs',
      {
        json: payload,
        headers: {
          'Idempotency-Key': options.idempotencyKey || crypto.randomUUID(),
        },
        signal: options.signal,
      },
    );
  }

  get(runId: string, options: { signal?: AbortSignal } = {}): Promise<AgenticRunStatus> {
    return this.http.get<AgenticRunStatus>(`/agentic/runs/${encodeURIComponent(runId)}`, {
      signal: options.signal,
    });
  }

  events(
    runId: string,
    options: AgenticRunEventsOptions = {},
  ): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    return this.eventsFromLink(
      `/agentic/runs/${encodeURIComponent(runId)}/events`,
      options,
    );
  }

  async eventsFromLink(
    eventsLink: string,
    options: AgenticRunEventsOptions = {},
  ): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    const path = relativeApiPath(eventsLink, 'eventsLink');
    const cursor = Math.max(0, Math.trunc(options.afterSeq || 0));
    const source = await this.http.stream(
      'GET',
      path,
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
  ): Promise<AgenticRunStatus> {
    return this.http.post<AgenticRunStatus, CancelRunRequest>(
      `/agentic/runs/${encodeURIComponent(runId)}/cancel`,
      {
        json: request,
        signal: options.signal,
      },
    );
  }
}

const relativeApiPath = (value: string, field: string): string => {
  const path = value.trim();
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new Error(`${field} must be a relative API path`);
  }
  return path;
};
