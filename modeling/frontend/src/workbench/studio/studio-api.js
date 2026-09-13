import { api } from '../../api.js';

const P = (projectId) => `/api/projects/${encodeURIComponent(projectId)}/studio`;
const S = (projectId) => `/api/projects/${encodeURIComponent(projectId)}/schema-semantics`;
const STUDIO_BOOTSTRAP_CACHE_TTL_MS = 15_000;
const STUDIO_BOOTSTRAP_CACHE_LIMIT = 4;
const bootstrapRequests = new Map();

function queryString(options = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(options)) {
    if (value !== undefined && value !== null && String(value) !== '') query.set(key, String(value));
  }
  return query.size ? `?${query}` : '';
}

export const getStudioSchema = (projectId) => api(`${P(projectId)}/schema`, { cache: 'no-cache' });

export const getSchemaSemanticSummary = (projectId, options = {}) => api(
  `${S(projectId)}/summary`,
  options,
);

export const getSchemaSemanticItems = (projectId, options = {}, requestOptions = {}) => api(
  `${S(projectId)}/items${queryString(options)}`,
  requestOptions,
);

export function getStudioBootstrap(projectId, options = {}, requestOptions = {}) {
  const { signal, cache = 'default', ...apiOptions } = requestOptions;
  if (cache === 'reload') invalidateStudioBootstrap(projectId);
  const key = `${projectId}:${queryString(options)}`;
  let entry = bootstrapRequests.get(key);
  if (entry && performance.now() - entry.createdAt > STUDIO_BOOTSTRAP_CACHE_TTL_MS) {
    bootstrapRequests.delete(key);
    entry = null;
  }
  if (!entry) {
    const promise = api(`${P(projectId)}/bootstrap${queryString(options)}`, apiOptions);
    entry = { projectId, createdAt: performance.now(), promise };
    bootstrapRequests.set(key, entry);
    promise.catch(() => {
      if (bootstrapRequests.get(key) === entry) bootstrapRequests.delete(key);
    });
    while (bootstrapRequests.size > STUDIO_BOOTSTRAP_CACHE_LIMIT) {
      bootstrapRequests.delete(bootstrapRequests.keys().next().value);
    }
  }
  return waitForStudioBootstrap(entry.promise, signal);
}

export function prefetchStudioBootstrap(projectId, options = { limit: 200 }) {
  return getStudioBootstrap(projectId, options);
}

export function invalidateStudioBootstrap(projectId) {
  bootstrapRequests.forEach((entry, key) => {
    if (entry.projectId === projectId) bootstrapRequests.delete(key);
  });
}

function waitForStudioBootstrap(promise, signal) {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const abort = () => reject(abortError());
    signal.addEventListener('abort', abort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

function abortError() {
  const error = new Error('Studio bootstrap request aborted');
  error.name = 'AbortError';
  return error;
}

export const getStudioElements = (projectId, options = {}) => api(
  `${P(projectId)}/elements${queryString(options)}`,
);

export const getStudioElement = (projectId, elementId, workingRevision) => api(
  `${P(projectId)}/elements/${encodeURIComponent(elementId)}${queryString({ workingRevision })}`,
);

export const getStudioOperationFlow = (projectId, elementId, workingRevision, options = {}) => api(
  `${P(projectId)}/elements/${encodeURIComponent(elementId)}/flow${queryString({ workingRevision })}`,
  options,
);

export const getStudioGraph = (projectId, options = {}) => api(
  `${P(projectId)}/graph${queryString(options)}`,
);

export const createStudioElement = async (projectId, input) => {
  const response = await api(`${P(projectId)}/elements`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
  invalidateStudioBootstrap(projectId);
  return response;
};

export const updateStudioElement = async (projectId, elementId, input) => {
  const response = await api(
    `${P(projectId)}/elements/${encodeURIComponent(elementId)}`,
    { method: 'PATCH', body: JSON.stringify(input) },
  );
  invalidateStudioBootstrap(projectId);
  return response;
};

export const batchStudioElements = async (projectId, input) => {
  const response = await api(`${P(projectId)}/batch`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
  invalidateStudioBootstrap(projectId);
  return response;
};

export const previewStudioDeletions = (projectId, input) => api(`${P(projectId)}/deletions/preview`, {
  method: 'POST',
  body: JSON.stringify(input),
});

export const applyStudioDeletions = async (projectId, input) => {
  const response = await api(`${P(projectId)}/deletions/apply`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
  invalidateStudioBootstrap(projectId);
  return response;
};

export const requestStudioReview = async (projectId, input) => {
  const response = await api(`${P(projectId)}/review`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
  invalidateStudioBootstrap(projectId);
  return response;
};

export function studioError(error) {
  const code = error?.data?.code || '';
  return {
    code,
    conflict: error?.status === 409 && ['WORKING_REVISION_CHANGED', 'REVISION_CONFLICT'].includes(code),
    message: error?.message || '知识包 Studio 暂时无法读取',
    expectedRevision: error?.data?.expectedRevision || '',
    actualRevision: error?.data?.actualRevision || '',
    fieldErrors: error?.data?.fieldErrors || {},
  };
}
