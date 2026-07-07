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
