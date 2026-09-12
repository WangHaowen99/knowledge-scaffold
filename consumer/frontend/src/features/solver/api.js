import { authFetch } from '../../shared/api/auth.js';

const cleanText = (value) => String(value ?? '').trim();

const networkError = (fallbackMessage, cause) => {
  const error = new Error(`${fallbackMessage}：网络异常或后端服务不可用`, {
    cause,
  });
  error.status = 0;
  error.code = 'network_error';
  error.retryable = true;
  return error;
};

export async function requestSolverJson(
  request,
  url,
  options = {},
  fallbackMessage = '求解器请求失败',
) {
  let response;
  try {
    response = await request(url, options);
  } catch (cause) {
    if (cause?.name === 'AbortError') {
      throw cause;
    }
    throw networkError(fallbackMessage, cause);
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      payload.error ||
        payload.detail ||
        `${fallbackMessage}（HTTP ${response.status}）`,
    );
    error.status = response.status;
    error.code = cleanText(payload.code);
    error.retryable = payload.retryable === true;
    throw error;
  }
  return payload;
}

const jsonOptions = (body, { signal, headers = {} } = {}) => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json', ...headers },
  body: JSON.stringify(body),
  signal,
});

export function createCandidateSet(
  payload,
  { request = authFetch, signal } = {},
) {
  return requestSolverJson(
    request,
    '/api/solver/runs',
    jsonOptions(payload, { signal }),
    '生成候选集失败',
  );
}

export function createValidationRun(
  payload,
  idempotencyKey,
  { request = authFetch, signal } = {},
) {
  const normalizedKey = cleanText(idempotencyKey);
  if (!normalizedKey) {
    return Promise.reject(new Error('缺少校验请求幂等键'));
  }
  return requestSolverJson(
    request,
    '/api/solver/validations',
    jsonOptions(payload, {
      signal,
      headers: { 'Idempotency-Key': normalizedKey },
    }),
    '校验候选集失败',
  );
}

export function getSolverRun(runId, { request = authFetch, signal } = {}) {
  const normalizedRunId = cleanText(runId);
  if (!normalizedRunId) {
    return Promise.reject(new Error('缺少求解运行标识'));
  }
  return requestSolverJson(
    request,
    `/api/solver/runs/${encodeURIComponent(normalizedRunId)}`,
    { signal },
    '加载求解运行失败',
  );
}

export function getSolverHistory(
  { cursor = '', limit = 50 } = {},
  { request = authFetch, signal } = {},
) {
  const params = new URLSearchParams();
  if (cleanText(cursor)) {
    params.set('cursor', cleanText(cursor));
  }
  params.set('limit', String(Math.min(100, Math.max(1, Number(limit) || 50))));
  return requestSolverJson(
    request,
    `/api/solver/history?${params.toString()}`,
    { signal },
    '加载求解历史失败',
  );
}

export function getSolverEvidence(jobId, { request = authFetch, signal } = {}) {
  const normalizedJobId = cleanText(jobId);
  if (!normalizedJobId) {
    return Promise.reject(new Error('缺少求解 Job 标识'));
  }
  return requestSolverJson(
    request,
    `/api/solver/history/${encodeURIComponent(normalizedJobId)}/evidence`,
    { signal },
    '加载验证证据失败',
  );
}

export function getSolverCandidateResults(
  jobId,
  { request = authFetch, signal } = {},
) {
  const normalizedJobId = cleanText(jobId);
  if (!normalizedJobId) {
    return Promise.reject(new Error('缺少求解 Job 标识'));
  }
  return requestSolverJson(
    request,
    `/api/solver/history/${encodeURIComponent(normalizedJobId)}/candidate-results`,
    { signal },
    '加载候选方案失败',
  );
}
