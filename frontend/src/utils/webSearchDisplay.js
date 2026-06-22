const MESSAGE_SUCCESS_STATUSES = new Set(['completed', 'finished', 'success']);
const MESSAGE_ERROR_STATUSES = new Set(['aborted', 'cancelled', 'canceled', 'error', 'failed']);

const isPlainObject = (value) => (
  value != null
  && typeof value === 'object'
  && !Array.isArray(value)
);

const parseJsonObject = (value) => {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return isPlainObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
};

const normalizeElapsedMs = (value) => {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
};

const extractQuery = (meta = {}, parsedOutput = null) => String(
  meta?.input?.query
  ?? meta?.tool_args?.query
  ?? meta?.tool_input?.query
  ?? meta?.query
  ?? parsedOutput?.query
  ?? ''
).trim();

const extractDomain = (url) => {
  const text = String(url || '').trim();
  if (!text) return '';
  try {
    return new URL(text).hostname.toLowerCase();
  } catch {
    return '';
  }
};

const normalizeSources = (parsedOutput) => {
  const rawResults = Array.isArray(parsedOutput?.results) ? parsedOutput.results : [];
  return rawResults
    .filter((item) => isPlainObject(item))
    .map((item, index) => {
      const url = String(item.url || '').trim();
      const domain = String(item.domain || '').trim() || extractDomain(url);
      return {
        rank: Number.isFinite(Number(item.rank)) ? Number(item.rank) : index + 1,
        title: String(item.title || domain || url || `来源 ${index + 1}`).trim(),
        url,
        domain,
        snippet: String(item.snippet || item.content || '').trim(),
        publishedAt: String(item.published_at || item.published_date || '').trim(),
      };
    });
};

const resolveResultCount = (parsedOutput, timeline, sources) => {
  if (Array.isArray(parsedOutput?.results)) return parsedOutput.results.length;
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const match = String(timeline[index]?.content || '').match(/返回\s*(\d+)\s*条结果/u);
    if (match) return Number(match[1]);
  }
  return sources.length;
};

const resolveElapsedMs = ({ segment, events, isTerminal, nowTs }) => {
  const metaElapsedMs = normalizeElapsedMs(segment?.meta?.elapsed_ms);
  if (metaElapsedMs != null) return metaElapsedMs;

  const startTs = normalizeElapsedMs(segment?.startTs);
  const endTs = normalizeElapsedMs(segment?.endTs);
  if (startTs != null) {
    return Math.max(0, (endTs ?? nowTs ?? Date.now()) - startTs);
  }

  const eventTs = events
    .map((event) => normalizeElapsedMs(event?.ts))
    .filter((value) => value != null);
  if (eventTs.length === 0) return null;
  const firstTs = eventTs[0];
  const lastTs = eventTs[eventTs.length - 1];
  return Math.max(0, (isTerminal ? lastTs : (nowTs ?? Date.now())) - firstTs);
};

export function buildWebSearchProcessModel({ segment, messageStatus = '', nowTs = null } = {}) {
  const meta = segment?.meta || {};
  const toolName = typeof meta?.tool_name === 'string' ? meta.tool_name.trim() : '';
  if (toolName !== 'web_search') return null;

  const parsedOutput = parseJsonObject(meta?.output_preview);
  const timeline = (Array.isArray(meta?.web_search_events) ? meta.web_search_events : [])
    .filter((event) => isPlainObject(event))
    .map((event) => ({
      type: String(event.type || '').trim().toLowerCase(),
      content: String(event.content || '').trim(),
      ts: normalizeElapsedMs(event.ts),
    }))
    .filter((event) => event.content.length > 0);
  const sources = normalizeSources(parsedOutput);
  const query = extractQuery(meta, parsedOutput);
  const errorText = String(parsedOutput?.error || meta?.error || '').trim();

  const normalizedMessageStatus = String(messageStatus || '').toLowerCase();
  const normalizedToolStatus = String(segment?.toolStatus || segment?.state || meta?.status || '').toLowerCase();
  const isFailed = Boolean(errorText)
    || MESSAGE_ERROR_STATUSES.has(normalizedMessageStatus)
    || normalizedToolStatus === 'error'
    || timeline.some((event) => event.type === 'error');
  const isCompleted = !isFailed && (
    Boolean(parsedOutput?.ok)
    || MESSAGE_SUCCESS_STATUSES.has(normalizedMessageStatus)
    || normalizedToolStatus === 'completed'
    || Boolean(segment?.finished)
    || timeline.some((event) => event.type === 'final')
  );
  const isTerminal = isCompleted || isFailed || Boolean(segment?.finished);
  const resultCount = resolveResultCount(parsedOutput, timeline, sources);
  const elapsedMs = resolveElapsedMs({ segment, events: timeline, isTerminal, nowTs });

  if (!query && timeline.length === 0 && sources.length === 0 && !errorText) {
    return null;
  }

  return {
    query,
    timeline,
    sources,
    resultCount,
    provider: String(parsedOutput?.provider || '').trim(),
    responseTime: normalizeElapsedMs(parsedOutput?.response_time),
    elapsedMs,
    errorText,
    isFailed,
    isCompleted,
    isTerminal,
  };
}
