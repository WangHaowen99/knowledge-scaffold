/**
 * Hook for managing multiple concurrent SSE streams across sessions.
 *
 * Enables:
 * - Multiple parallel streams (one per session)
 * - Session switching without interrupting background streams
 * - Per-session message caching
 * - Stream status indicators for sidebar
 */

import { useState, useRef, useCallback, useMemo } from 'react';
import createLogger from '../../../shared/lib/logger';
import { authFetch } from '../../../shared/api/auth';
import { AGENT_STREAM_EVENT, isAgentV2Envelope } from '../lib/streamHelpers';
import { parseSSEMessage, takeSSEFrames } from '../lib/sse';
import {
  buildRunEventsUrl,
  buildRunStatusUrl,
  classifyEnvelope,
  normalizeSequence,
} from '../lib/streamResume';
import {
  abortStreamController,
  abortStreamControllers,
  classifyRunProgressFromEnvelope,
  isSilentStreamAbort,
  isTerminalRunStatus,
  resolveRunStartedAt,
  streamStatusForAdmission,
  streamStatusForConnection,
  streamStatusForError,
  streamStatusForTerminal,
} from '../lib/streamLifecycle';

const logger = createLogger('StreamManager');
const QUEUE_POLL_INTERVAL_MS = 600;
const RECONNECT_DELAY_MS = 800;
const MAX_RECONNECT_ATTEMPTS = 8;

const abortError = () => {
  const error = new Error('Stream aborted');
  error.name = 'AbortError';
  return error;
};

const waitForDelay = (delayMs, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) {
    reject(abortError());
    return;
  }
  const onAbort = () => {
    clearTimeout(timer);
    reject(abortError());
  };
  const timer = setTimeout(() => {
    signal?.removeEventListener('abort', onAbort);
    resolve();
  }, delayMs);
  signal?.addEventListener('abort', onAbort, { once: true });
});

const buildInitialProgressState = (now = Date.now(), phase = 'starting') => ({
  startedAt: now,
  progressPhase: phase,
});

/**
 * Build cancel URL from stream URL and runId.
 */
const buildCancelUrl = (streamUrl, runId) => {
  if (!streamUrl || !runId) return null;

  const baseOrigin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost';
  let url;
  try {
    url = new URL(streamUrl, baseOrigin);
  } catch (error) {
    logger.warn('Failed to resolve stream URL for cancel', { streamUrl, error: error.message });
    return null;
  }

  if (!url.pathname.endsWith('/agentic/runs/stream')) {
    return null;
  }

  url.pathname = url.pathname.replace(/\/agentic\/runs\/stream\/?$/, `/agentic/runs/${runId}/cancel`);
  url.search = '';
  url.hash = '';
  return url.toString();
};

const useStreamManager = () => {
  // Stream contexts: Map<sessionId, StreamContext>
  const [streams, setStreams] = useState(new Map());

  // Message cache: Map<sessionId, Message[]>
  const [messagesCache, setMessagesCache] = useState(new Map());

  // AbortControllers: Map<sessionId, AbortController>
  const abortControllersRef = useRef(new Map());

  // Stream metadata: Map<sessionId, { runId, streamUrl, runType }>
  const streamMetaRef = useRef(new Map());

  /**
   * Call backend cancel API for a session if possible.
   */
  const cancelBackendRun = useCallback(async (sessionId, reason = 'disconnect') => {
    if (!sessionId) return { ok: true, cancelled: false };

    const meta = streamMetaRef.current.get(sessionId);
    if (!meta?.runId || !meta?.streamUrl) return { ok: true, cancelled: false };

    const cancelUrl = buildCancelUrl(meta.streamUrl, meta.runId);
    if (!cancelUrl) return { ok: false };

    try {
      const res = await authFetch(cancelUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      logger.debug('Cancel API response', { sessionId, reason, status: res.status });
      if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        if (res.status === 404) {
          const statusResponse = await authFetch(buildRunStatusUrl(meta.runId));
          const statusPayload = await statusResponse.json().catch(() => ({}));
          const terminalStatus = String(statusPayload.status || '').trim();
          if (statusResponse.ok && isTerminalRunStatus(terminalStatus)) {
            return { ok: true, cancelled: false, terminalStatus };
          }
        }
        logger.warn('Backend rejected run cancellation', {
          sessionId,
          reason,
          status: res.status,
          error: payload.error || payload.detail || '',
        });
        return { ok: false };
      }
      return { ok: true, cancelled: true };
    } catch (error) {
      logger.warn('Failed to cancel backend run', { sessionId, reason, error: error.message });
      return { ok: false };
    }
  }, []);

  /**
   * Get stream state for a session.
   */
  const getStreamState = useCallback((sessionId) => {
    if (!sessionId) {
      return { isLoading: false, status: 'idle', runId: null };
    }
    const stream = streams.get(sessionId);
    if (!stream) {
      return { isLoading: false, status: 'idle', runId: null };
    }
    return stream;
  }, [streams]);

  /**
   * Get list of session IDs with active streams.
   */
  const getActiveSessionIds = useCallback(() => {
    const activeIds = [];
    streams.forEach((stream, sessionId) => {
      if (stream.isLoading) {
        activeIds.push(sessionId);
      }
    });
    return activeIds;
  }, [streams]);

  /**
   * Update stream state for a session.
   */
  const updateStreamState = useCallback((sessionId, updates) => {
    setStreams((prev) => {
      const next = new Map(prev);
      const current = next.get(sessionId) || {
        sessionId,
        runId: null,
        isLoading: false,
        status: 'idle',
      };
      next.set(sessionId, { ...current, ...updates });
      return next;
    });
  }, []);

  /**
   * Get messages for a session.
   */
  const getMessagesForSession = useCallback((sessionId) => {
    if (!sessionId) return [];
    return messagesCache.get(sessionId) || [];
  }, [messagesCache]);

  /**
   * Set messages for a session.
   */
  const setMessagesForSession = useCallback((sessionId, updater) => {
    if (!sessionId) return;

    setMessagesCache((prev) => {
      const next = new Map(prev);
      const currentMessages = prev.get(sessionId) || [];
      const newMessages = typeof updater === 'function' ? updater(currentMessages) : updater;
      next.set(sessionId, newMessages);
      return next;
    });
  }, []);

  /**
   * Clear messages cache for a session.
   */
  const clearCacheForSession = useCallback((sessionId) => {
    if (!sessionId) return;

    setMessagesCache((prev) => {
      const next = new Map(prev);
      next.delete(sessionId);
      return next;
    });

    setStreams((prev) => {
      const next = new Map(prev);
      next.delete(sessionId);
      return next;
    });
  }, []);

  const clearAllCaches = useCallback(() => {
    abortStreamControllers(abortControllersRef.current, { silent: true });
    setMessagesCache(new Map());
    setStreams(new Map());
    abortControllersRef.current = new Map();
    streamMetaRef.current = new Map();
  }, []);

  /**
   * Consume one SSE response. A clean EOF without turn.finished is resumable,
   * so lifecycle/error handling stays with the reconnect loop below.
   */
  const processStream = useCallback(async (sessionId, reader, streamToken, onEvent) => {
    const decoder = new TextDecoder();
    let buffer = '';
    let terminalStatus = '';
    let receivedEvent = false;

    while (true) {
      const { done, value } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: !done });
      if (done) buffer += decoder.decode();
      const { frames: parts, remainder } = takeSSEFrames(buffer, { done });
      buffer = remainder;

      let shouldStop = false;
      for (const part of parts) {
        const parsed = parseSSEMessage(part);
        if (!parsed) continue;
        receivedEvent = true;

        let { event, data } = parsed;
        if ((event === 'message' || !event) && data && typeof data === 'object') {
          if (data.event && data.data) {
            event = data.event;
            data = data.data;
          } else if (!event && (data.event || data.type)) {
            event = data.event || data.type;
          }
        }

        const activeMeta = streamMetaRef.current.get(sessionId);
        if (!activeMeta || activeMeta.streamToken !== streamToken) {
          await reader.cancel();
          return { terminalStatus: '', superseded: true };
        }

        if (event === AGENT_STREAM_EVENT && isAgentV2Envelope(data)) {
          const classification = classifyEnvelope(
            data,
            activeMeta.lastSeq,
            activeMeta.seenEventIds,
          );
          if (classification.duplicate) {
            logger.debug('Ignoring replayed SSE envelope', {
              sessionId,
              eventId: classification.identity,
              sequence: classification.sequence,
            });
            continue;
          }

          activeMeta.runId = data.run_id || activeMeta.runId;
          activeMeta.runType = data.type || activeMeta.runType;
          activeMeta.lastSeq = classification.nextCursor;
          if (classification.identity) activeMeta.seenEventIds.add(classification.identity);
          const progress = classifyRunProgressFromEnvelope(data);
          if (progress?.phase) {
            activeMeta.progressPhase = progress.phase;
          }
          streamMetaRef.current.set(sessionId, activeMeta);

          updateStreamState(sessionId, {
            runId: activeMeta.runId,
            cursor: activeMeta.lastSeq,
            status: data.type === 'turn.finished' ? 'finalizing' : 'streaming',
            startedAt: activeMeta.startedAt,
            progressPhase: activeMeta.progressPhase,
          });

          if (data.type === 'turn.finished') {
            terminalStatus = String(data.payload?.status || 'success');
          }
        }

        logger.debug('SSE event', { sessionId, event });
        if (onEvent?.(event, data)) {
          logger.debug('Stream handler requested break', { sessionId });
          shouldStop = true;
          break;
        }
      }

      if (shouldStop || terminalStatus) {
        if (!terminalStatus) terminalStatus = 'success';
        await reader.cancel();
        break;
      }
      if (done) {
        logger.debug('Stream response ended', { sessionId, terminal: Boolean(terminalStatus) });
        break;
      }
    }

    return { terminalStatus, superseded: false, receivedEvent };
  }, [updateStreamState]);

  const cleanupStream = useCallback((sessionId, streamToken, status) => {
    const activeMeta = streamMetaRef.current.get(sessionId);
    if (activeMeta?.streamToken !== streamToken) return;
    abortControllersRef.current.delete(sessionId);
    streamMetaRef.current.delete(sessionId);
    updateStreamState(sessionId, {
      isLoading: false,
      status,
      queue: null,
    });
  }, [updateStreamState]);

  const followRun = useCallback(async (sessionId, streamToken, onEvent) => {
    let reconnectAttempts = 0;

    while (true) {
      const meta = streamMetaRef.current.get(sessionId);
      const controller = abortControllersRef.current.get(sessionId);
      if (!meta || meta.streamToken !== streamToken) return { superseded: true, terminalStatus: '' };
      if (!controller || controller.signal.aborted) throw abortError();

      if (meta.runStatus === 'queued') {
        try {
          const statusResponse = await authFetch(buildRunStatusUrl(meta.runId), {
            signal: controller.signal,
          });
          const statusPayload = await statusResponse.json().catch(() => ({}));
          if (!statusResponse.ok) {
            throw new Error(statusPayload.error || `Run status failed with ${statusResponse.status}`);
          }

          meta.runStatus = String(statusPayload.status || 'queued');
          meta.queue = statusPayload.queue || null;
          streamMetaRef.current.set(sessionId, meta);
          if (meta.runStatus === 'queued') {
            meta.progressPhase = 'queued';
            streamMetaRef.current.set(sessionId, meta);
            updateStreamState(sessionId, {
              isLoading: true,
              status: 'queued',
              runId: meta.runId,
              queue: meta.queue,
              startedAt: meta.startedAt,
              progressPhase: meta.progressPhase,
            });
            await waitForDelay(QUEUE_POLL_INTERVAL_MS, controller.signal);
            continue;
          }
          reconnectAttempts = 0;
        } catch (error) {
          if (error.name === 'AbortError') throw error;
          reconnectAttempts += 1;
          if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) throw error;
          meta.progressPhase = 'reconnecting';
          streamMetaRef.current.set(sessionId, meta);
          updateStreamState(sessionId, {
            isLoading: true,
            status: 'reconnecting',
            runId: meta.runId,
            queue: meta.queue,
            startedAt: meta.startedAt,
            progressPhase: meta.progressPhase,
          });
          await waitForDelay(RECONNECT_DELAY_MS * reconnectAttempts, controller.signal);
          continue;
        }
      }

      meta.progressPhase = reconnectAttempts > 0
        ? 'reconnecting'
        : (['queued', 'reconnecting'].includes(meta.progressPhase) ? 'running' : (meta.progressPhase || 'running'));
      streamMetaRef.current.set(sessionId, meta);
      updateStreamState(sessionId, {
        isLoading: true,
        status: streamStatusForConnection(reconnectAttempts),
        runId: meta.runId,
        cursor: meta.lastSeq,
        queue: null,
        startedAt: meta.startedAt,
        progressPhase: meta.progressPhase,
      });

      try {
        const headers = meta.lastSeq ? { 'Last-Event-ID': String(meta.lastSeq) } : undefined;
        const response = await authFetch(buildRunEventsUrl(meta.runId, meta.lastSeq), {
          headers,
          signal: controller.signal,
        });
        if (response.status === 409) {
          const payload = await response.json().catch(() => ({}));
          meta.runStatus = 'queued';
          meta.queue = payload.queue || null;
          meta.progressPhase = 'queued';
          streamMetaRef.current.set(sessionId, meta);
          continue;
        }
        if (response.status === 410) {
          const payload = await response.json().catch(() => ({}));
          const goneStatus = String(payload.status || meta.runStatus || '').trim();
          if (isTerminalRunStatus(goneStatus)) {
            return { terminalStatus: goneStatus, superseded: false, receivedEvent: false };
          }
          throw new Error(payload.error || 'Run event stream is no longer available');
        }
        if (!response.ok || !response.body) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.error || `Run event stream failed with ${response.status}`);
        }

        const result = await processStream(
          sessionId,
          response.body.getReader(),
          streamToken,
          onEvent,
        );
        if (result.superseded || result.terminalStatus) return result;
        reconnectAttempts = result.receivedEvent ? 0 : reconnectAttempts + 1;
      } catch (error) {
        if (error.name === 'AbortError') throw error;
        reconnectAttempts += 1;
        logger.warn('Run event stream disconnected; retrying from cursor', {
          sessionId,
          runId: meta.runId,
          cursor: meta.lastSeq,
          attempt: reconnectAttempts,
          error: error.message,
        });
      }

      if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        throw new Error('Run event stream reconnect limit exceeded');
      }
      await waitForDelay(RECONNECT_DELAY_MS * reconnectAttempts, controller.signal);
    }
  }, [processStream, updateStreamState]);

  /**
   * Start a stream for a specific session.
   */
  const startStreamForSession = useCallback(async (sessionId, url, body, { onEvent, onComplete, onError }) => {
    if (!sessionId) {
      logger.warn('Cannot start stream without sessionId');
      return;
    }

    logger.info('Starting stream for session', { sessionId, url });

    const requestedRunId = typeof body?.run_id === 'string' && body.run_id.trim()
      ? body.run_id.trim()
      : null;
    const startedAt = Date.now();
    const streamToken = `${startedAt}-${Math.random().toString(16).slice(2)}`;
    const initialProgress = buildInitialProgressState(startedAt);

    const controller = new AbortController();
    abortStreamController(abortControllersRef.current.get(sessionId), { silent: true });
    abortControllersRef.current.set(sessionId, controller);
    streamMetaRef.current.set(sessionId, {
      streamUrl: url,
      runId: requestedRunId,
      runType: null,
      runStatus: 'running',
      streamToken,
      lastSeq: 0,
      seenEventIds: new Set(),
      queue: null,
      onComplete,
      ...initialProgress,
    });

    updateStreamState(sessionId, {
      sessionId,
      isLoading: true,
      status: 'streaming',
      runId: requestedRunId,
      ...initialProgress,
    });

    try {
      const res = await authFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      logger.debug('Stream response', { sessionId, status: res.status });

      let result;
      if (res.status === 202) {
        const payload = await res.json().catch(() => ({}));
        const meta = streamMetaRef.current.get(sessionId);
        if (!meta || meta.streamToken !== streamToken) return;
        meta.runId = String(payload.run_id || requestedRunId || '').trim();
        if (!meta.runId) throw new Error('Queued stream response did not include run_id');
        meta.runStatus = String(payload.status || 'queued');
        meta.queue = payload.queue || null;
        meta.progressPhase = 'queued';
        streamMetaRef.current.set(sessionId, meta);
        updateStreamState(sessionId, {
          isLoading: true,
          status: streamStatusForAdmission(meta.runStatus),
          runId: meta.runId,
          queue: meta.queue,
          startedAt: meta.startedAt,
          progressPhase: meta.progressPhase,
        });
        result = await followRun(sessionId, streamToken, onEvent);
      } else {
        if (!res.ok || !res.body) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data?.error || `Stream failed with status ${res.status}`);
        }
        const meta = streamMetaRef.current.get(sessionId);
        if (meta?.streamToken === streamToken) {
          meta.progressPhase = 'running';
          streamMetaRef.current.set(sessionId, meta);
          updateStreamState(sessionId, {
            isLoading: true,
            status: 'streaming',
            runId: meta.runId,
            startedAt: meta.startedAt,
            progressPhase: meta.progressPhase,
          });
        }
        result = await processStream(sessionId, res.body.getReader(), streamToken, onEvent);
        if (!result.terminalStatus && !result.superseded) {
          result = await followRun(sessionId, streamToken, onEvent);
        }
      }

      if (result.superseded) return;
      if (onComplete) onComplete(result.terminalStatus);
      cleanupStream(sessionId, streamToken, streamStatusForTerminal(result.terminalStatus));
    } catch (error) {
      if (error.name === 'AbortError') {
        logger.info('Stream aborted by user', { sessionId });
        if (!isSilentStreamAbort(controller) && onError) onError(error, true);
      } else {
        logger.error('Stream error', { sessionId, error: error.message });
        if (onError) onError(error, false);
      }
      cleanupStream(sessionId, streamToken, streamStatusForError(error));
    }
  }, [cleanupStream, followRun, processStream, updateStreamState]);

  /**
   * Reattach after a page reload or session switch. The durable session read
   * supplies run id and cursor; followRun handles queued admission and replay.
   */
  const resumeRunForSession = useCallback(async (
    sessionId,
    runId,
    afterSequence,
    { onEvent, onComplete, onError, status = 'running' },
  ) => {
    if (!sessionId || !runId) return false;
    const existing = streamMetaRef.current.get(sessionId);
    if (existing?.runId === runId && abortControllersRef.current.has(sessionId)) return true;

    const resumedAt = Date.now();
    const startedAt = resolveRunStartedAt({ runId }, resumedAt);
    const streamToken = `${resumedAt}-${Math.random().toString(16).slice(2)}`;
    const controller = new AbortController();
    const cursor = normalizeSequence(afterSequence);
    const initialProgress = buildInitialProgressState(
      startedAt,
      status === 'queued' ? 'queued' : 'reconnecting',
    );
    abortStreamController(abortControllersRef.current.get(sessionId), { silent: true });
    abortControllersRef.current.set(sessionId, controller);
    streamMetaRef.current.set(sessionId, {
      streamUrl: '/agentic/runs/stream',
      runId,
      runType: null,
      runStatus: status === 'queued' ? 'queued' : 'running',
      streamToken,
      lastSeq: cursor,
      seenEventIds: new Set(),
      queue: null,
      onComplete,
      ...initialProgress,
    });
    updateStreamState(sessionId, {
      sessionId,
      runId,
      cursor,
      isLoading: true,
      status: streamStatusForAdmission(status),
      ...initialProgress,
    });

    try {
      const result = await followRun(sessionId, streamToken, onEvent);
      if (result.superseded) return true;
      if (onComplete) onComplete(result.terminalStatus);
      cleanupStream(sessionId, streamToken, streamStatusForTerminal(result.terminalStatus));
      return true;
    } catch (error) {
      if (error.name === 'AbortError') {
        if (!isSilentStreamAbort(controller) && onError) onError(error, true);
      } else {
        logger.error('Failed to resume run event stream', { sessionId, runId, error: error.message });
        if (onError) onError(error, false);
      }
      cleanupStream(sessionId, streamToken, streamStatusForError(error));
      return false;
    }
  }, [cleanupStream, followRun, updateStreamState]);

  /**
   * Stop stream for a specific session.
   */
  const stopStreamForSession = useCallback(async (sessionId, { silent = false } = {}) => {
    if (!sessionId) return true;

    const controller = abortControllersRef.current.get(sessionId);
    const meta = streamMetaRef.current.get(sessionId);

    if (controller) {
      logger.info('Stopping stream for session', { sessionId });

      // Call backend cancel API if we have runId
      const streamToken = meta?.streamToken;
      const result = await cancelBackendRun(sessionId, 'user_stop');
      const activeMeta = streamMetaRef.current.get(sessionId);
      const activeController = abortControllersRef.current.get(sessionId);
      if (activeController !== controller || activeMeta?.streamToken !== streamToken) return true;
      if (!result.ok) {
        updateStreamState(sessionId, {
          isLoading: true,
          status: 'cancel_error',
          error: '后端未确认取消，运行仍在继续',
        });
        return false;
      }

      if (result.terminalStatus) {
        activeMeta.onComplete?.(result.terminalStatus);
        abortStreamController(controller, { silent: true });
        cleanupStream(sessionId, streamToken, streamStatusForTerminal(result.terminalStatus));
        return true;
      }

      // Abort the fetch
      abortStreamController(controller, { silent });
      if (silent) cleanupStream(sessionId, streamToken, 'cancelled');
      return true;
    }

    if (meta?.runId && meta?.streamUrl) {
      logger.info('Stopping pending run for session', { sessionId, runId: meta.runId });
      const result = await cancelBackendRun(sessionId, 'user_stop');
      if (!result.ok) {
        updateStreamState(sessionId, {
          isLoading: true,
          status: 'cancel_error',
          error: '后端未确认取消，运行仍在继续',
        });
        return false;
      }
      if (result.terminalStatus) meta.onComplete?.(result.terminalStatus);
      streamMetaRef.current.delete(sessionId);
      updateStreamState(sessionId, {
        isLoading: false,
        status: result.terminalStatus
          ? streamStatusForTerminal(result.terminalStatus)
          : 'cancelled',
        runId: null,
        queue: null,
      });
      return true;
    }
    return true;
  }, [cancelBackendRun, cleanupStream, updateStreamState]);

  /**
   * Stop all active streams (e.g., on page refresh/unload).
   */
  const stopAllStreams = useCallback(async () => {
    const sessionIds = Array.from(abortControllersRef.current.keys());
    return Promise.all(sessionIds.map((sessionId) => stopStreamForSession(sessionId)));
  }, [stopStreamForSession]);

  /**
   * Check if a session has an active stream.
   */
  const isSessionLoading = useCallback((sessionId) => {
    if (!sessionId) return false;
    const stream = streams.get(sessionId);
    return stream?.isLoading || false;
  }, [streams]);

  // Memoized active session IDs for sidebar indicators
  const activeSessionIds = useMemo(() => {
    return getActiveSessionIds();
  }, [getActiveSessionIds]);

  return {
    // Stream management
    startStreamForSession,
    resumeRunForSession,
    stopStreamForSession,
    getStreamState,
    isSessionLoading,
    activeSessionIds,
    streamStates: streams,

    // Message cache management
    getMessagesForSession,
    setMessagesForSession,
    clearCacheForSession,
    clearAllCaches,
    messagesCache,

    // Bulk stream control
    stopAllStreams,
  };
};

export default useStreamManager;
