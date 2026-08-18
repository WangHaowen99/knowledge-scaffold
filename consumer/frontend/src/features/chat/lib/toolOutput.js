const OPAQUE_OUTPUT_REF = /^[0-9a-f]{32}$/i;

export const resolveToolOutputUrl = (value, sessionId, runId) => {
  const outputRef = String(value || '').trim();
  const normalizedSessionId = String(sessionId || '').trim();
  const normalizedRunId = String(runId || '').trim();

  if (!OPAQUE_OUTPUT_REF.test(outputRef) || !normalizedSessionId || !normalizedRunId) {
    return '';
  }

  return `/agentic/sessions/${encodeURIComponent(normalizedSessionId)}`
    + `/runs/${encodeURIComponent(normalizedRunId)}`
    + `/tool-outputs/${outputRef.toLowerCase()}`;
};

export const formatOutputBytes = (value) => {
  if (value == null || value === '') return '';
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};
