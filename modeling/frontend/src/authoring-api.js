import { api, authFetch } from './api.js';
import { createProjectEventParser } from './project-events.js';

const DEFAULT_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 15_000;
const requestIds = new Map();

const projectPath = (id) => `/api/projects/${encodeURIComponent(id)}`;

function stableValue(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (Array.isArray(value)) {
    if (seen.has(value)) throw new Error('回答格式不正确');
    seen.add(value);
    const result = value.map((item) => stableValue(item, seen));
    seen.delete(value);
    return result;
  }
  if (value && typeof value === 'object') {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) throw new Error('回答格式不正确');
    if (seen.has(value)) throw new Error('回答格式不正确');
    seen.add(value);
    const result = {};
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) result[key] = stableValue(value[key], seen);
    }
    seen.delete(value);
    return result;
  }
  throw new Error('回答格式不正确');
}

function responseKey(projectId, interactionId, response) {
  return JSON.stringify([String(projectId), String(interactionId), stableValue(response)]);
}

function rememberRequestId(key, supplied) {
  const existing = requestIds.get(key);
  const requested = supplied == null ? '' : String(supplied).trim();
  const id = requested || existing || globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  requestIds.delete(key);
  requestIds.set(key, id);
  while (requestIds.size > 200) requestIds.delete(requestIds.keys().next().value);
  return id;
}

export const getProjectSession = (id) => api(`${projectPath(id)}/session`);

export const getAiConstruction = (id) => api(`${projectPath(id)}/ai-construction`);

export const applyAiConstructionCommand = (id, command) => api(
  `${projectPath(id)}/ai-construction/commands`,
  { method: 'POST', body: JSON.stringify({ command }) },
);

export const previewAiConstructionTaskAdjustment = (id, taskId, action) => api(
  `${projectPath(id)}/ai-construction/tasks/${encodeURIComponent(taskId)}/adjustment-preview?action=${encodeURIComponent(action)}`,
);

export const adjustAiConstructionTask = (id, taskId, input) => api(
  `${projectPath(id)}/ai-construction/tasks/${encodeURIComponent(taskId)}/adjust`,
  { method: 'POST', body: JSON.stringify(input) },
);

export const previewAiConstructionTaskEdit = (id, edit) => api(
  `${projectPath(id)}/ai-construction/task-edit-preview`,
  { method: 'POST', body: JSON.stringify({ edit }) },
);

export const editAiConstructionTasks = (id, input) => api(
  `${projectPath(id)}/ai-construction/task-edit`,
  { method: 'POST', body: JSON.stringify(input) },
);

export function respondToInteraction(id, interactionId, response, requestId) {
  if (!response || typeof response !== 'object' || Array.isArray(response)) {
    return Promise.reject(new Error('回答格式不正确'));
  }
  const key = responseKey(id, interactionId, response);
  const stableRequestId = rememberRequestId(key, requestId);
  return api(`${projectPath(id)}/interactions/${encodeURIComponent(interactionId)}/respond`, {
    method: 'POST',
    body: JSON.stringify({ requestId: stableRequestId, response }),
  });
}

export const listRevisions = (id) => api(`${projectPath(id)}/revisions`);

export const listCandidateRevisions = (id, status = '') => {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  return api(`${projectPath(id)}/candidate-revisions${query}`);
};

export const previewCandidateRevision = (id, candidateId, groupIds = []) => {
  const query = groupIds.length
    ? `?groups=${encodeURIComponent(groupIds.join(','))}`
    : '';
  return api(`${projectPath(id)}/candidate-revisions/${encodeURIComponent(candidateId)}/preview${query}`);
};

export const respondCandidateRevision = (id, candidateId, response) => api(
  `${projectPath(id)}/candidate-revisions/${encodeURIComponent(candidateId)}/respond`,
  { method: 'POST', body: JSON.stringify(response) },
);

export const getRevisionDiff = (id, from, to) => api(
  `${projectPath(id)}/revisions/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
);

export const restoreRevision = (id, payload) => api(`${projectPath(id)}/revisions/restore`, {
  method: 'POST',
  body: JSON.stringify(payload),
});

const abortError = (error) => error?.name === 'AbortError';

async function responseError(response) {
  const data = await response.json().catch(() => ({}));
  return new Error(data.error || `连接失败（${response.status}）`);
}

/**
 * Opens the project's event stream and reconnects from the last received seq.
 * Returns a synchronous unsubscribe function.
 */
export function subscribeProjectEvents(id, {
  after = 0,
  onEvent,
  onError,
} = {}) {
  let cursor = Number.isSafeInteger(Number(after)) && Number(after) >= 0 ? Number(after) : 0;
  let stopped = false;
  let reconnectTimer = null;
  let controller = null;
  let reader = null;
  let failures = 0;
  let serverRetryMs = DEFAULT_RECONNECT_MS;

  const reportError = (error) => {
    if (stopped) return;
    try { onError?.(error instanceof Error ? error : new Error('事件连接失败')); } catch { /* callback errors stay local */ }
  };

  const deliver = (event) => {
    if (stopped) return;
    if (event.seq !== null && event.seq > cursor) cursor = event.seq;
    failures = 0;
    try { onEvent?.(event); } catch (error) { reportError(error); }
  };

  const scheduleReconnect = (connect) => {
    if (stopped) return;
    const multiplier = 2 ** Math.min(failures, 4);
    const delay = Math.min(MAX_RECONNECT_MS, Math.max(250, serverRetryMs) * multiplier);
    failures += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      void connect();
    }, delay);
  };

  const connect = async () => {
    if (stopped) return;
    const activeController = new AbortController();
    let activeReader = null;
    controller = activeController;
    const parser = createProjectEventParser({ lastSeq: cursor });
    try {
      const response = await authFetch(`${projectPath(id)}/events?after=${encodeURIComponent(cursor)}`, {
        headers: { Accept: 'text/event-stream' },
        signal: activeController.signal,
      });
      if (!response.ok) throw await responseError(response);
      if (!String(response.headers.get('Content-Type')).toLowerCase().includes('text/event-stream')) {
        throw new Error('事件连接不可用');
      }
      if (!response.body) throw new Error('事件连接不可用');

      activeReader = response.body.getReader();
      reader = activeReader;
      while (!stopped) {
        const { done, value } = await activeReader.read();
        if (done) break;
        for (const event of parser.push(value)) {
          if (stopped) break;
          deliver(event);
        }
        const retry = parser.getRetry();
        if (Number.isFinite(retry)) serverRetryMs = Math.min(MAX_RECONNECT_MS, Math.max(250, retry));
      }
      if (stopped) return;
      for (const event of parser.flush()) deliver(event);
      throw new Error('事件连接已断开');
    } catch (error) {
      if (stopped || activeController.signal.aborted || abortError(error)) return;
      reportError(error);
      scheduleReconnect(connect);
    } finally {
      if (activeReader) {
        try { activeReader.releaseLock(); } catch { /* already released */ }
      }
      if (controller === activeController) controller = null;
      if (reader === activeReader) reader = null;
    }
  };

  void connect();

  return () => {
    if (stopped) return;
    stopped = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    controller?.abort();
    reader?.cancel().catch(() => undefined);
    controller = null;
    reader = null;
  };
}
