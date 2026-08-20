export const AGENTIC_RUNS_ENDPOINT = '/agentic/runs';

const requiredText = (value, fieldName) => {
  const normalized = String(value ?? '').trim();
  if (!normalized) throw new Error(`Run admission response is missing ${fieldName}`);
  return normalized;
};

export const createIdempotencyKey = () => {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return randomUuid;
  return `request-${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

export const buildRunRequestHeaders = (idempotencyKey) => {
  const normalizedKey = String(idempotencyKey || '').trim();
  if (!normalizedKey) throw new Error('Idempotency-Key is required to create a run');
  return {
    'Content-Type': 'application/json',
    'Idempotency-Key': normalizedKey,
  };
};

export const buildRunRequestPayload = (message, sessionId, options = {}) => ({
  request: String(message || '').trim(),
  session_id: String(sessionId || '').trim(),
  agent_mode: options.agentMode || 'quick',
  model_profile_id: options.modelProfileId || '',
  arguments: options.arguments || null,
  include_trace: options.includeTrace === true,
  ...(options.interactionResponse
    ? { interaction_response: options.interactionResponse }
    : {}),
});

export const normalizeRunAdmission = (payload, expectedSessionId = '') => {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Run admission response must be an object');
  }

  const runId = requiredText(payload.run_id, 'run_id');
  const sessionId = requiredText(payload.session_id, 'session_id');
  const expected = String(expectedSessionId || '').trim();
  if (expected && sessionId !== expected) {
    throw new Error('Run admission response session_id does not match the request');
  }
  if (payload.turn_id == null || String(payload.turn_id).trim() === '') {
    throw new Error('Run admission response is missing turn_id');
  }
  const status = requiredText(payload.status, 'status');
  if (typeof payload.created !== 'boolean') {
    throw new Error('Run admission response is missing created');
  }
  const links = payload.links;
  if (!links || typeof links !== 'object' || Array.isArray(links)) {
    throw new Error('Run admission response is missing links');
  }
  for (const relation of ['self', 'events', 'cancel']) {
    requiredText(links[relation], `links.${relation}`);
  }

  return {
    runId,
    sessionId,
    turnId: payload.turn_id,
    status,
    created: payload.created,
    links,
  };
};
