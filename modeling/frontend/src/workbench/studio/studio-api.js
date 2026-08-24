import { api } from '../../api.js';

const P = (projectId) => `/api/projects/${encodeURIComponent(projectId)}/studio`;

function queryString(options = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(options)) {
    if (value !== undefined && value !== null && String(value) !== '') query.set(key, String(value));
  }
  return query.size ? `?${query}` : '';
}

export const getStudioSchema = (projectId) => api(`${P(projectId)}/schema`);

export const getStudioElements = (projectId, options = {}) => api(
  `${P(projectId)}/elements${queryString(options)}`,
);

export const getStudioElement = (projectId, elementId, workingRevision) => api(
  `${P(projectId)}/elements/${encodeURIComponent(elementId)}${queryString({ workingRevision })}`,
);

export const getStudioGraph = (projectId, options = {}) => api(
  `${P(projectId)}/graph${queryString(options)}`,
);

export const createStudioElement = (projectId, input) => api(`${P(projectId)}/elements`, {
  method: 'POST',
  body: JSON.stringify(input),
});

export const updateStudioElement = (projectId, elementId, input) => api(
  `${P(projectId)}/elements/${encodeURIComponent(elementId)}`,
  { method: 'PATCH', body: JSON.stringify(input) },
);

export const batchStudioElements = (projectId, input) => api(`${P(projectId)}/batch`, {
  method: 'POST',
  body: JSON.stringify(input),
});

export const previewStudioDeletions = (projectId, input) => api(`${P(projectId)}/deletions/preview`, {
  method: 'POST',
  body: JSON.stringify(input),
});

export const applyStudioDeletions = (projectId, input) => api(`${P(projectId)}/deletions/apply`, {
  method: 'POST',
  body: JSON.stringify(input),
});

export const requestStudioReview = (projectId, input) => api(`${P(projectId)}/review`, {
  method: 'POST',
  body: JSON.stringify(input),
});

export function studioError(error) {
  const code = error?.data?.code || '';
  return {
    code,
    conflict: error?.status === 409 && ['WORKING_REVISION_CHANGED', 'REVISION_CONFLICT'].includes(code),
    message: error?.data?.reason || error?.message || '知识包 Studio 暂时无法读取',
    expectedRevision: error?.data?.expectedRevision || '',
    actualRevision: error?.data?.actualRevision || '',
    fieldErrors: error?.data?.fieldErrors || {},
  };
}
