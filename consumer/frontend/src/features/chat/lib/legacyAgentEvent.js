import {
  isPublicEvent,
  normalizePublicEventStatus,
  PUBLIC_EVENT_SCHEMA_VERSION,
} from './publicEvent.js';

export const LEGACY_AGENT_STREAM_PROTOCOL = 'agent-stream.v2';
export const LEGACY_AGENT_STREAM_EVENT = 'agent.v2';

export const isLegacyAgentEventEnvelope = (value) => (
  value
  && typeof value === 'object'
  && value.protocol === LEGACY_AGENT_STREAM_PROTOCOL
  && typeof value.type === 'string'
);

export const isLegacyStoredAgentEvent = (value) => (
  value
  && typeof value === 'object'
  && value.event === LEGACY_AGENT_STREAM_EVENT
  && isLegacyAgentEventEnvelope(value.data)
);

const positiveInteger = (value, fallback) => {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};

const legacyCreatedAt = (value, fallbackNow) => {
  const numeric = Number(value);
  const parsed = Number.isFinite(numeric) && numeric > 0
    ? numeric
    : Date.parse(String(value || ''));
  const timestamp = Number.isFinite(parsed) ? parsed : fallbackNow;
  return new Date(timestamp).toISOString();
};

const legacyStatus = (envelope, eventType) => {
  const explicitStatus = normalizePublicEventStatus(envelope?.status);
  if (explicitStatus) return explicitStatus;
  if (eventType === 'turn.finished') {
    return normalizePublicEventStatus(envelope?.payload?.status, 'completed');
  }
  if (eventType === 'interaction.requested') return 'awaiting_input';
  return 'running';
};

export const legacyAgentEventToPublicEvent = (
  value,
  { index = 0, now = Date.now() } = {},
) => {
  const envelope = isLegacyStoredAgentEvent(value) ? value.data : value;
  if (!isLegacyAgentEventEnvelope(envelope)) return null;

  const sequence = positiveInteger(envelope.seq, Math.max(1, index + 1));
  const runId = String(envelope.run_id || '').trim() || 'unknown-run';
  const eventType = String(envelope.type || '').trim();
  const status = legacyStatus(envelope, eventType);
  const payload = envelope.payload && typeof envelope.payload === 'object'
    ? { ...envelope.payload }
    : {};
  if (eventType === 'turn.finished') payload.status = status;

  const source = envelope.source && typeof envelope.source === 'object'
    ? { ...envelope.source }
    : {};
  if (typeof envelope.span_key === 'string' && envelope.span_key) {
    source.span_key = envelope.span_key;
  }
  if (typeof envelope.parent_key === 'string' && envelope.parent_key) {
    source.parent_key = envelope.parent_key;
  }

  const turnId = positiveInteger(envelope.turn_id, 0);
  const event = {
    schema_version: PUBLIC_EVENT_SCHEMA_VERSION,
    event_id: String(envelope.event_id || '').trim() || `${runId}:legacy:${sequence}`,
    run_id: runId,
    seq: sequence,
    event_type: eventType,
    status,
    source,
    payload,
    created_at: legacyCreatedAt(envelope.ts, now),
    ...(typeof envelope.session_id === 'string' ? { session_id: envelope.session_id } : {}),
    ...(turnId ? { turn_id: turnId } : {}),
  };

  return isPublicEvent(event) ? event : null;
};

export const storedAgentEventToPublicEvent = (value, options = {}) => {
  if (isPublicEvent(value)) return value;
  if (isPublicEvent(value?.data)) return value.data;
  return legacyAgentEventToPublicEvent(value, options);
};
