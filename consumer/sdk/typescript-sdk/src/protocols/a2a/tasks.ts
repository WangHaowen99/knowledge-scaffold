import { mapAbortableStream, type AbortableAsyncIterable } from '../../streams/sse.js';
import { HttpClient } from '../../transport/http.js';
import { normalizeA2ASseMessage, type NormalizedStreamEvent } from '../../types/events.js';
import type {
  A2ACancelTaskResponse,
  A2ACreateTaskRequest,
  A2AStreamCreateRequest,
  A2ATaskResponse,
} from '../../types/a2a.js';
export class A2ATasksApi {
  constructor(private readonly http: HttpClient) {}

  create(request: A2ACreateTaskRequest, options: { signal?: AbortSignal } = {}): Promise<A2ATaskResponse> {
    const payload: A2ACreateTaskRequest = {
      ...request,
      context: { ...(request.context || {}) },
    };
    return this.http.post<A2ATaskResponse, A2ACreateTaskRequest>('/a2a/tasks', {
      json: payload,
      signal: options.signal,
    });
  }

  get(taskId: string, options: { sessionId?: string; signal?: AbortSignal } = {}): Promise<A2ATaskResponse> {
    return this.http.get<A2ATaskResponse>(`/a2a/tasks/${encodeURIComponent(taskId)}`, {
      query: { session_id: options.sessionId },
      signal: options.signal,
    });
  }

  cancel(taskId: string, options: { signal?: AbortSignal } = {}): Promise<A2ACancelTaskResponse> {
    return this.http.delete<A2ACancelTaskResponse>(`/a2a/tasks/${encodeURIComponent(taskId)}`, {
      signal: options.signal,
    });
  }

  async events(
    taskId: string,
    options: { sessionId?: string; signal?: AbortSignal } = {},
  ): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    const source = await this.http.stream('GET', `/a2a/tasks/${encodeURIComponent(taskId)}/events`, {
      query: { session_id: options.sessionId },
      signal: options.signal,
    });
    return mapAbortableStream(source, normalizeA2ASseMessage);
  }

  async streamCreate(
    request: A2AStreamCreateRequest,
    options: { signal?: AbortSignal } = {},
  ): Promise<AbortableAsyncIterable<NormalizedStreamEvent>> {
    const payload: A2AStreamCreateRequest = {
      ...request,
      context: { ...(request.context || {}) },
    };
    const source = await this.http.stream('POST', '/a2a/tasks/stream', {
      json: payload,
      signal: options.signal,
    });
    return mapAbortableStream(source, normalizeA2ASseMessage);
  }
}
