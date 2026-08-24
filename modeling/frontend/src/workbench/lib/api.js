import {
  api,
  authFetch,
  createProject,
  downloadDocument,
  exportPackageUrl,
  listMaterialRevisions,
  requireOkResponse,
  uploadDocument,
  updateDocumentDirectives,
} from '../../api.js';
import { createProjectEventParser } from '../../project-events.js';

const P = (id) => `/api/projects/${encodeURIComponent(id)}`;

export const getSnapshot = (id) => api(`${P(id)}/agent/snapshot`);
export const getAgentState = (id) => api(`${P(id)}/agent/state`);
export const getPackageFile = (id, path) => api(
  `${P(id)}/agent/file?path=${encodeURIComponent(path)}`,
);
export const getCandidateDiff = (id) => api(`${P(id)}/agent/diff`);
export const listRevisions = (id) => api(`${P(id)}/revisions`);
export const getRevisionDiff = (id, from, to) => api(
  `${P(id)}/revisions/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
);
export const restoreRevision = (id, payload) => api(`${P(id)}/revisions/restore`, {
  method: 'POST', body: JSON.stringify(payload),
});
export const listPackageSources = (id) => api(`${P(id)}/agent/package-sources`);
export const uploadPackageSource = (id, file) => {
  const form = new FormData();
  form.append('file', file);
  return api(`${P(id)}/agent/package-sources`, { method: 'POST', body: form });
};
export const getEvaluationDataSource = (id) => api(`${P(id)}/data-source`);
export const saveEvaluationDataSource = (id, input) => api(`${P(id)}/data-source?workflow=agent`, {
  method: 'PUT', body: JSON.stringify(input),
});
export const listEvaluationTestcases = (id) => api(`${P(id)}/testcases`);
export const addEvaluationTestcase = (id, question, groundTruth) => api(`${P(id)}/testcases?workflow=agent`, {
  method: 'POST', body: JSON.stringify({ question, groundTruth }),
});
export const updateEvaluationTestcase = (id, testcaseId, patch) => api(
  `${P(id)}/testcases/${encodeURIComponent(testcaseId)}?workflow=agent`,
  { method: 'PATCH', body: JSON.stringify(patch) },
);
export const deleteEvaluationTestcase = (id, testcaseId) => api(
  `${P(id)}/testcases/${encodeURIComponent(testcaseId)}?workflow=agent`,
  { method: 'DELETE' },
);
export const listEvaluationRounds = (id) => api(`${P(id)}/rounds`);
export const getEvaluationRound = (id, roundId) => api(
  `${P(id)}/rounds/${encodeURIComponent(roundId)}`,
);
export const updateConfirmPolicy = (id, tier, mode) => api(`${P(id)}/agent/confirm-policy`, {
  method: 'PUT', body: JSON.stringify({ tier, mode }),
});
export const getMaterial = (id, documentId) => api(
  `${P(id)}/agent/materials/${encodeURIComponent(documentId)}`,
);
export const getMaterialReferrers = (id, documentId, anchor = '') => api(
  `${P(id)}/materials/${encodeURIComponent(documentId)}/referrers${anchor ? `?anchor=${encodeURIComponent(anchor)}` : ''}`,
);
export const locateEvidence = (id, reference) => api(
  `${P(id)}/evidence/locate?ref=${encodeURIComponent(reference)}`,
);
export const startAgent = (id, message, baselineRevision = '') => api(`${P(id)}/agent/runs`, {
  method: 'POST', body: JSON.stringify({ message, baselineRevision }),
});
export const steerAgent = (id, message) => api(`${P(id)}/agent/steer`, {
  method: 'POST', body: JSON.stringify({ message }),
});
export const answerAgent = (id, interactionId, answer) => api(
  `${P(id)}/agent/interactions/${encodeURIComponent(interactionId)}/answer`,
  { method: 'POST', body: JSON.stringify({ answer }) },
);
export const cancelAgent = (id) => api(`${P(id)}/agent/cancel`, { method: 'POST', body: '{}' });
export const uploadMaterial = uploadDocument;
export const updateMaterialDirectives = updateDocumentDirectives;
export const getMaterialRevisions = (id) => listMaterialRevisions(id);
export const downloadMaterial = downloadDocument;
export const downloadPackage = async (id) => requireOkResponse(await authFetch(exportPackageUrl(id)));
export const createModelingProject = (input) => createProject({ ...input, runtime: 'agent' });

export function subscribeAgent(id, { after = 0, onEvent } = {}) {
  let stopped = false;
  let cursor = Math.max(0, Number(after) || 0);
  let controller = null;
  let reconnect = null;
  let failures = 0;

  const connect = async () => {
    if (stopped) return;
    const active = new AbortController();
    controller = active;
    const parser = createProjectEventParser({ lastSeq: cursor });
    try {
      const response = await authFetch(`${P(id)}/agent/stream?after=${cursor}`, {
        headers: { Accept: 'text/event-stream' }, signal: active.signal,
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw Object.assign(new Error(data.error || '助手事件流不可用'), { status: response.status });
      }
      const reader = response.body?.getReader();
      if (!reader) throw new Error('助手事件流不可用');
      while (!stopped) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const frame of parser.push(value)) {
          cursor = Math.max(cursor, frame.seq ?? 0);
          failures = 0;
          onEvent?.({ ...frame.data, eventType: frame.type });
        }
      }
      if (!stopped) throw new Error('助手事件流已断开');
    } catch {
      if (stopped || active.signal.aborted) return;
      // 事件日志可按 cursor 续传，短暂断线只需在这里重试，不能冒充内容读取失败。
      const delay = Math.min(15_000, 1_000 * (2 ** Math.min(failures++, 4)));
      reconnect = setTimeout(connect, delay);
    } finally {
      if (controller === active) controller = null;
    }
  };

  void connect();
  return () => {
    stopped = true;
    controller?.abort();
    if (reconnect) clearTimeout(reconnect);
  };
}
