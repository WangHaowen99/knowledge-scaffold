export const PUBLIC_EVENT_SCHEMA_VERSION = '1.0';

const PUBLIC_EVENT_STATUSES = new Set([
  'queued',
  'running',
  'awaiting_input',
  'completed',
  'failed',
  'cancelled',
]);

const TERMINAL_PUBLIC_EVENT_STATUSES = new Set([
  'awaiting_input',
  'completed',
  'failed',
  'cancelled',
]);

const STATUS_ALIASES = {
  awaiting: 'awaiting_input',
  canceled: 'cancelled',
  error: 'failed',
  finished: 'completed',
  success: 'completed',
  succeeded: 'completed',
};

const isPlainObject = (value) =>
  value != null && typeof value === 'object' && !Array.isArray(value);

const isPositiveInteger = (value) => Number.isInteger(value) && value > 0;

const hasCanonicalRequiredFields = (value) =>
  value.schema_version === PUBLIC_EVENT_SCHEMA_VERSION &&
  Boolean(String(value.event_id || '').trim()) &&
  Boolean(String(value.run_id || '').trim()) &&
  isPositiveInteger(value.seq) &&
  Boolean(String(value.event_type || '').trim()) &&
  PUBLIC_EVENT_STATUSES.has(value.status) &&
  isPlainObject(value.payload);

const hasValidOptionalSource = (value) =>
  value.source == null || isPlainObject(value.source);

const OPTIONAL_TRACE_FIELDS = [
  'span_key',
  'parent_key',
  'trace_id',
  'span_id',
  'parent_span_id',
];

const hasValidOptionalIdentifiers = (value) => {
  const traceFieldsValid = OPTIONAL_TRACE_FIELDS.every(
    (key) => value[key] == null || typeof value[key] === 'string',
  );
  const sessionIdValid =
    value.session_id == null || typeof value.session_id === 'string';
  const turnIdValid = value.turn_id == null || isPositiveInteger(value.turn_id);
  return traceFieldsValid && sessionIdValid && turnIdValid;
};

const hasValidCreatedAt = (value) =>
  typeof value.created_at === 'string' &&
  Number.isFinite(Date.parse(value.created_at));

const hasValidTerminalStatus = (value) => {
  if (value.event_type !== 'turn.finished') {
    return true;
  }
  if (!TERMINAL_PUBLIC_EVENT_STATUSES.has(value.status)) {
    return false;
  }
  if (value.payload.status == null) {
    return true;
  }
  return normalizePublicEventStatus(value.payload.status) === value.status;
};

export const normalizePublicEventStatus = (value, fallback = '') => {
  const normalized = String(value || '')
    .trim()
    .toLowerCase();
  const resolved = STATUS_ALIASES[normalized] || normalized;
  if (PUBLIC_EVENT_STATUSES.has(resolved)) {
    return resolved;
  }
  return PUBLIC_EVENT_STATUSES.has(fallback) ? fallback : '';
};

export const isPublicEvent = (value) => {
  if (!isPlainObject(value)) {
    return false;
  }
  return (
    hasCanonicalRequiredFields(value) &&
    hasValidOptionalSource(value) &&
    hasValidOptionalIdentifiers(value) &&
    hasValidCreatedAt(value) &&
    hasValidTerminalStatus(value)
  );
};

export const publicEventTimestamp = (event, fallback = Date.now()) => {
  const parsed = Date.parse(String(event?.created_at || ''));
  if (Number.isFinite(parsed)) {
    return parsed;
  }
  const normalizedFallback = Number(fallback);
  return Number.isFinite(normalizedFallback) ? normalizedFallback : Date.now();
};

export class PublicEventProtocolError extends Error {
  constructor(message) {
    super(message);
    this.name = 'PublicEventProtocolError';
  }
}

export const decodePublicEventFrame = (
  frame,
  { expectedRunId = '', expectedSessionId = '' } = {},
) => {
  if (!frame || typeof frame !== 'object' || !isPublicEvent(frame.data)) {
    throw new PublicEventProtocolError('Invalid PublicEvent payload');
  }

  const event = frame.data;
  const eventLabel = String(frame.event || '').trim();
  if (eventLabel !== event.event_type) {
    throw new PublicEventProtocolError(
      'SSE event name does not match PublicEvent.event_type',
    );
  }

  const sseId = String(frame.id || '').trim();
  if (!sseId || sseId !== String(event.seq)) {
    throw new PublicEventProtocolError('SSE id does not match PublicEvent.seq');
  }

  const normalizedRunId = String(expectedRunId || '').trim();
  if (normalizedRunId && event.run_id !== normalizedRunId) {
    throw new PublicEventProtocolError(
      'PublicEvent.run_id does not match the active run',
    );
  }

  const normalizedSessionId = String(expectedSessionId || '').trim();
  if (
    normalizedSessionId &&
    event.session_id &&
    event.session_id !== normalizedSessionId
  ) {
    throw new PublicEventProtocolError(
      'PublicEvent.session_id does not match the active session',
    );
  }

  return event;
};
