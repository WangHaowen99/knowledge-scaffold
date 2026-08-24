import { api } from '../../api.js';

const P = (projectId) => `/api/projects/${encodeURIComponent(projectId)}/agent/fusion-plans`;

const request = async (promise) => {
  try {
    return await promise;
  } catch (error) {
    const message = error?.data?.display?.text || error?.data?.reason || error?.data?.message
      || error?.message || '融合操作失败';
    throw Object.assign(new Error(message), {
      status: error?.status,
      code: error?.data?.code,
      details: error?.data,
    });
  }
};

export const listFusionPlans = (projectId) => request(api(P(projectId)));

export const createFusionPlan = (projectId, input) => request(api(P(projectId), {
  method: 'POST',
  body: JSON.stringify(input),
}));

export const updateFusionPlan = (projectId, planId, input) => request(api(
  `${P(projectId)}/${encodeURIComponent(planId)}`,
  { method: 'PATCH', body: JSON.stringify(input) },
));

export const compileFusionPlan = (projectId, planId) => request(api(
  `${P(projectId)}/${encodeURIComponent(planId)}/compile`,
  { method: 'POST', body: '{}' },
));
