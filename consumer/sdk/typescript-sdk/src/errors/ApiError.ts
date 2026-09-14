// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
export interface ApiErrorOptions {
  status: number;
  data?: unknown;
  response?: Response;
}

const extractDetail = (data: unknown): string | undefined => {
  if (!data || typeof data !== 'object') {
    return typeof data === 'string' && data.trim() ? data : undefined;
  }

  const record = data as Record<string, unknown>;
  const candidates = [record.detail, record.error, record.message];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) {
      return candidate;
    }
  }
  return undefined;
};

const parseResponseBody = async (response: Response): Promise<unknown> => {
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  try {
    const text = await response.text();
    return text || null;
  } catch {
    return null;
  }
};

export class ApiError extends Error {
  readonly status: number;
  readonly data?: unknown;
  readonly response?: Response;

  constructor(message: string, options: ApiErrorOptions) {
    super(message);
    this.name = 'ApiError';
    this.status = options.status;
    this.data = options.data;
    this.response = options.response;
  }

  static async fromResponse(response: Response): Promise<ApiError> {
    const data = await parseResponseBody(response);
    const detail = extractDetail(data) || `Request failed with status ${response.status}`;
    return new ApiError(detail, {
      status: response.status,
      data,
      response,
    });
  }
}
