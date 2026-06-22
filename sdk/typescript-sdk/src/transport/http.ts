import { ApiError } from '../errors/ApiError.js';
import { streamSse, type AbortableAsyncIterable, type SseEventMessage } from '../streams/sse.js';
import type { ClientOptions, RequestOptions, ValueProvider } from '../types/common.js';

const ensureFetch = (fetchImpl?: typeof globalThis.fetch): typeof globalThis.fetch => {
  if (fetchImpl) {
    return fetchImpl;
  }
  if (typeof globalThis.fetch !== 'function') {
    throw new Error('No fetch implementation available. Please provide options.fetch.');
  }
  return globalThis.fetch.bind(globalThis);
};

const normalizeBaseUrl = (baseUrl: string): string => {
  const trimmed = baseUrl.trim();
  if (!trimmed) {
    throw new Error('baseUrl is required');
  }
  return trimmed.endsWith('/') ? trimmed : `${trimmed}/`;
};

const resolveProvider = async <T>(provider?: ValueProvider<T>): Promise<T | undefined> => {
  if (typeof provider === 'function') {
    return await (provider as () => T | Promise<T>)();
  }
  return provider;
};

const buildUrl = (baseUrl: string, path: string, query?: Record<string, unknown>): URL => {
  const url = new URL(path, baseUrl);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) {
        continue;
      }
      url.searchParams.set(key, String(value));
    }
  }
  return url;
};

const mergeSignals = (primary?: AbortSignal, secondary?: AbortSignal): AbortSignal | undefined => {
  if (primary && secondary) {
    const abortSignalCtor = AbortSignal as typeof AbortSignal & {
      any?: (signals: AbortSignal[]) => AbortSignal;
    };
    if (typeof abortSignalCtor.any === 'function') {
      return abortSignalCtor.any([primary, secondary]);
    }
  }
  return primary || secondary;
};

export class HttpClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof globalThis.fetch;
  private tokenProvider?: ValueProvider<string | null | undefined>;
  private workspaceProvider?: ValueProvider<string | null | undefined>;
  private headersProvider?: ValueProvider<HeadersInit | undefined>;

  constructor(options: ClientOptions) {
    this.baseUrl = normalizeBaseUrl(options.baseUrl);
    this.fetchImpl = ensureFetch(options.fetch);
    this.tokenProvider = options.token;
    this.workspaceProvider = options.workspaceId;
    this.headersProvider = options.headers;
  }

  setToken(token: string | null | undefined): void {
    this.tokenProvider = token;
  }

  setWorkspace(workspaceId: string | null | undefined): void {
    this.workspaceProvider = workspaceId;
  }

  private async createHeaders(extraHeaders?: HeadersInit, hasJsonBody = false): Promise<Headers> {
    const headers = new Headers((await resolveProvider(this.headersProvider)) || {});
    if (extraHeaders) {
      new Headers(extraHeaders).forEach((value, key) => headers.set(key, value));
    }

    const token = await resolveProvider(this.tokenProvider);
    if (token) {
      headers.set('Authorization', `Bearer ${token}`);
    }

    const workspaceId = await resolveProvider(this.workspaceProvider);
    if (workspaceId) {
      headers.set('X-Workspace-Id', encodeURIComponent(workspaceId));
    }

    if (hasJsonBody && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }

    return headers;
  }

  async request<TResponse, TJson = unknown>(
    method: string,
    path: string,
    options: RequestOptions<TJson> = {},
  ): Promise<TResponse> {
    const hasJsonBody = options.json !== undefined;
    const headers = await this.createHeaders(options.headers, hasJsonBody);
    const url = buildUrl(this.baseUrl, path, options.query);
    const body = hasJsonBody ? JSON.stringify(options.json) : options.body;

    const response = await this.fetchImpl(url, {
      method,
      headers,
      body,
      signal: options.signal,
    });

    if (!response.ok) {
      throw await ApiError.fromResponse(response);
    }

    if (response.status === 204) {
      return undefined as TResponse;
    }

    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      return (await response.json()) as TResponse;
    }

    return (await response.text()) as TResponse;
  }

  get<TResponse>(path: string, options?: RequestOptions): Promise<TResponse> {
    return this.request<TResponse>('GET', path, options);
  }

  post<TResponse, TJson = unknown>(path: string, options?: RequestOptions<TJson>): Promise<TResponse> {
    return this.request<TResponse, TJson>('POST', path, options);
  }

  patch<TResponse, TJson = unknown>(path: string, options?: RequestOptions<TJson>): Promise<TResponse> {
    return this.request<TResponse, TJson>('PATCH', path, options);
  }

  put<TResponse, TJson = unknown>(path: string, options?: RequestOptions<TJson>): Promise<TResponse> {
    return this.request<TResponse, TJson>('PUT', path, options);
  }

  delete<TResponse>(path: string, options?: RequestOptions): Promise<TResponse> {
    return this.request<TResponse>('DELETE', path, options);
  }

  async stream<TJson = unknown>(
    method: string,
    path: string,
    options: RequestOptions<TJson> = {},
  ): Promise<AbortableAsyncIterable<SseEventMessage>> {
    const controller = new AbortController();
    const headers = await this.createHeaders(options.headers, options.json !== undefined);
    const url = buildUrl(this.baseUrl, path, options.query);
    const body = options.json !== undefined ? JSON.stringify(options.json) : options.body;

    const response = await this.fetchImpl(url, {
      method,
      headers,
      body,
      signal: options.signal || controller.signal,
    });

    if (!response.ok) {
      throw await ApiError.fromResponse(response);
    }

    return streamSse(response, controller);
  }
}
