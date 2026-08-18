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

export const listAiConstructionMessages = (id, { beforeSeq, limit = 100 } = {}) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeSeq != null) params.set('beforeSeq', String(beforeSeq));
  return api(`${projectPath(id)}/ai-construction/messages?${params}`);
};

// 进入工作台时调用：服务端先对账状态机再返回会话。事件驱动的刷新用 getProjectSession。
export const advanceProjectSession = (id) => api(`${projectPath(id)}/session/advance`, {
  method: 'POST', body: '{}',
});

export const confirmAiConstructionMilestone = (id, milestoneId, expectedRevision, note = '') => (
  applyAiConstructionCommand(id, {
    type: 'confirm_milestone',
    milestoneId,
    expectedRevision,
    ...(note.trim() ? { note: note.trim() } : {}),
  })
);

export const startAiConstructionIncrement = (id) => api(
  `${projectPath(id)}/ai-construction/increment/start`, { method: 'POST', body: '{}' },
);

export const proposeAiConstructionIncrement = (id) => api(
  `${projectPath(id)}/ai-construction/increment/propose`, { method: 'POST', body: '{}' },
);

export const cancelAiConstructionOperation = (id, operation) => api(
  `${projectPath(id)}/ai-construction/operations/cancel`,
  { method: 'POST', body: JSON.stringify({ operation }) },
);

export const reviseAiConstructionModelingBrief = (id) => api(
  `${projectPath(id)}/ai-construction/modeling-brief/revise`,
  { method: 'POST', body: '{}' },
);

export const generateAiConstructionKnowledge = (id) => api(
  `${projectPath(id)}/ai-construction/knowledge/generate`,
  { method: 'POST', body: '{}' },
);

export const repairAiConstructionKnowledge = (id) => api(
  `${projectPath(id)}/ai-construction/knowledge/repair`,
  { method: 'POST', body: '{}' },
);

// 体检面板的两个按钮都只是「把这条发现交给建模助手」，处理本身在助手里完成
export const reviewPackageCheckFinding = (id, findingId) => api(
  `${projectPath(id)}/package/check/findings/review`,
  { method: 'POST', body: JSON.stringify({ findingId }) },
);

export const ignorePackageCheckFinding = (id, findingId) => api(
  `${projectPath(id)}/package/check/findings/ignore`,
  { method: 'POST', body: JSON.stringify({ findingId }) },
);

export const deferAiConstructionKnowledgeErrors = (id, reason) => api(
  `${projectPath(id)}/ai-construction/knowledge/defer-errors`,
  { method: 'POST', body: JSON.stringify({ reason: String(reason ?? '').trim() }) },
);

export const deferAiConstructionScripts = (id) => api(
  `${projectPath(id)}/ai-construction/scripts/defer`,
  { method: 'POST', body: '{}' },
);

export const retryAiConstructionScripts = (id) => api(
  `${projectPath(id)}/ai-construction/scripts/retry`,
  { method: 'POST', body: '{}' },
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

export const getWorkflowStep = (id, stepId) => api(
  `${projectPath(id)}/workflow/steps/${encodeURIComponent(stepId)}`,
);

export const getFlowchartDraft = (id, interactionId) => api(
  `${projectPath(id)}/interactions/${encodeURIComponent(interactionId)}/flowchart-draft`,
);

export const saveFlowchartDraft = (id, interactionId, expectedRevision, document) => api(
  `${projectPath(id)}/interactions/${encodeURIComponent(interactionId)}/flowchart-draft`,
  {
    method: 'PUT',
    body: JSON.stringify({ expectedRevision, document }),
  },
);

export const addFlowchartDiagram = (id, interactionId, input) => api(
  `${projectPath(id)}/interactions/${encodeURIComponent(interactionId)}/flowchart-draft/diagrams`,
  {
    method: 'POST',
    body: JSON.stringify(input),
  },
);

export const startScenario = (id) => api(`${projectPath(id)}/workflow/scenario/start`, {
  method: 'POST', body: '{}',
});

export function respondToScenarioReview(id, interactionId, response, requestId) {
  if (!response || typeof response !== 'object' || Array.isArray(response)) {
    return Promise.reject(new Error('回答格式不正确'));
  }
  const key = responseKey(id, interactionId, response);
  const stableRequestId = rememberRequestId(key, requestId);
  return api(
    `${projectPath(id)}/workflow/scenario/reviews/${encodeURIComponent(interactionId)}/respond`,
    {
      method: 'POST',
      body: JSON.stringify({ requestId: stableRequestId, response }),
    },
  );
}

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
  return Object.assign(new Error(data.error || `连接失败（${response.status}）`), {
    status: response.status,
    data,
  });
}

export function isTerminalProjectEventError(error) {
  return [401, 403, 404, 410].includes(Number(error?.status));
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
      if (isTerminalProjectEventError(error)) return;
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
