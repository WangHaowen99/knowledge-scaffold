const EVENT_ID_SEQUENCE = /:(\d+)$/;

export const normalizeSequence = (value) => {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
};

export const getEnvelopeSequence = (envelope) => {
  const explicit = normalizeSequence(envelope?.seq);
  if (explicit) return explicit;
  const match = EVENT_ID_SEQUENCE.exec(String(envelope?.event_id || ''));
  return match ? normalizeSequence(match[1]) : 0;
};

export const getEnvelopeIdentity = (envelope) => {
  const eventId = String(envelope?.event_id || '').trim();
  if (eventId) return eventId;
  const sequence = getEnvelopeSequence(envelope);
  const runId = String(envelope?.run_id || '').trim();
  return sequence && runId ? `${runId}:${sequence}` : '';
};

export const classifyEnvelope = (envelope, cursor = 0, seenEventIds = new Set()) => {
  const sequence = getEnvelopeSequence(envelope);
  const identity = getEnvelopeIdentity(envelope);
  const duplicate = Boolean(
    (sequence && sequence <= normalizeSequence(cursor))
    || (identity && seenEventIds.has(identity))
  );
  return {
    duplicate,
    identity,
    sequence,
    nextCursor: duplicate ? normalizeSequence(cursor) : Math.max(normalizeSequence(cursor), sequence),
  };
};

export const buildRunEventsUrl = (runId, afterSequence = 0) => {
  const encodedRunId = encodeURIComponent(String(runId || '').trim());
  const cursor = normalizeSequence(afterSequence);
  return `/agentic/runs/${encodedRunId}/events${cursor ? `?after_seq=${cursor}` : ''}`;
};

export const buildRunStatusUrl = (runId) => (
  `/agentic/runs/${encodeURIComponent(String(runId || '').trim())}/status`
);
