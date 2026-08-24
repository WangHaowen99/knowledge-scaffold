export const normalizeSequence = (value) => {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
};

export const classifyPublicEvent = (event, cursor = 0, seenEventIds = new Set()) => {
  const sequence = normalizeSequence(event?.seq);
  const identity = String(event?.event_id || '').trim();
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
  return `/agentic/runs/${encodedRunId}/events?after_seq=${cursor}`;
};

export const buildRunUrl = (runId) => (
  `/agentic/runs/${encodeURIComponent(String(runId || '').trim())}`
);

export const buildRunCancelUrl = (runId) => (
  `${buildRunUrl(runId)}/cancel`
);
