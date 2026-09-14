// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
const OPAQUE_OUTPUT_REF = /^[0-9a-f]{32}$/i;

export const resolveToolOutputUrl = (value, sessionId, runId) => {
  const outputRef = String(value || '').trim();
  const normalizedSessionId = String(sessionId || '').trim();
  const normalizedRunId = String(runId || '').trim();

  if (
    !OPAQUE_OUTPUT_REF.test(outputRef) ||
    !normalizedSessionId ||
    !normalizedRunId
  ) {
    return '';
  }

  return (
    `/agentic/sessions/${encodeURIComponent(normalizedSessionId)}` +
    `/runs/${encodeURIComponent(normalizedRunId)}` +
    `/tool-outputs/${outputRef.toLowerCase()}`
  );
};

export const formatOutputBytes = (value) => {
  if (value == null || value === '') {
    return '';
  }
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '';
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

export const outputResponseDigestMatches = (response, expectedSha256) => {
  const expected = String(expectedSha256 || '').trim();
  const stored = String(
    response?.headers?.get?.('X-Tool-Output-SHA256') || '',
  ).trim();
  return !expected || expected === stored;
};

export const parseOutputResponse = async (response, { raw = false } = {}) => {
  if (raw) {
    return response.text();
  }
  const contentType = response.headers.get('Content-Type') || '';
  if (contentType.includes('application/json')) {
    const payload = await response.json().catch(() => ({}));
    const output = payload.output ?? payload.content ?? payload.data;
    if (typeof output === 'string') {
      return output;
    }
    if (output != null) {
      return JSON.stringify(output, null, 2);
    }
    if (payload.error || payload.detail) {
      throw new Error(payload.error || payload.detail);
    }
    return JSON.stringify(payload, null, 2);
  }
  return response.text();
};
