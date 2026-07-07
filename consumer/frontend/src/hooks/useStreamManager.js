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
import createLogger from '../utils/logger';
import { authFetch } from '../utils/auth';
import { AGENT_STREAM_EVENT, isAgentV2Envelope } from '../utils/streamHelpers';

const logger = createLogger('StreamManager');

/**
 * Parse SSE message from raw text chunk.
 */
const parseSSEMessage = (chunk) => {
  let eventLine = '';
  const dataLines = [];

  for (const line of chunk.split(/\r?\n/)) {
    if (!line) continue;
    if (line.startsWith('event:')) {
      eventLine = line.replace(/^event:\s*/, '');
    } else if (line.startsWith('data:')) {
      dataLines.push(line.replace(/^data:\s*/, ''));
    }
  }

  if (dataLines.length === 0) return null;

  const dataLine = dataLines.join('\n');
  if (dataLine === '[DONE]') return null;

  try {
    return {
      event: eventLine || 'message',
      data: JSON.parse(dataLine),
    };
  } catch (err) {
    logger.warn('Failed to parse SSE data', { dataLine, error: err.message });
    return null;
  }
};

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
    if (!sessionId) return;

    const meta = streamMetaRef.current.get(sessionId);
    if (!meta?.runId || !meta?.streamUrl) return;

    const cancelUrl = buildCancelUrl(meta.streamUrl, meta.runId);
    if (!cancelUrl) return;

    try {
      const res = await authFetch(cancelUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      logger.debug('Cancel API response', { sessionId, reason, status: res.status });
    } catch (error) {
      logger.warn('Failed to cancel backend run', { sessionId, reason, error: error.message });
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
    setMessagesCache(new Map());
    setStreams(new Map());
    abortControllersRef.current = new Map();
    streamMetaRef.current = new Map();
  }, []);

  /**
   * Process SSE stream from a readable stream.
   */
  const processStream = useCallback(async (sessionId, reader, streamToken, { onEvent, onComplete, onError }) => {
    const decoder = new TextDecoder();
    let buffer = '';
    let sawTerminalEvent = false;

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          logger.debug('Stream ended', { sessionId });
          if (!sawTerminalEvent) {
            await cancelBackendRun(sessionId, 'disconnect');
          }
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split(/\r?\n\r?\n/);
        buffer = parts.pop() || '';

        let shouldStop = false;
        for (const part of parts) {
          const parsed = parseSSEMessage(part);
          if (parsed) {
            let { event, data } = parsed;
            if ((event === 'message' || !event) && data && typeof data === 'object') {
              if (data.event && data.data) {
                event = data.event;
                data = data.data;
              } else if (!event && (data.event || data.type)) {
                event = data.event || data.type;
              }
            }

            logger.debug('SSE event', { sessionId, event });

            // Track run metadata from v2 envelope
            if (event === AGENT_STREAM_EVENT && isAgentV2Envelope(data)) {
              const meta = streamMetaRef.current.get(sessionId) || {};
              meta.runId = data.run_id || meta.runId;
              meta.runType = data.type || meta.runType;
              streamMetaRef.current.set(sessionId, meta);

              updateStreamState(sessionId, { runId: data.run_id });
            }

            if (event === AGENT_STREAM_EVENT && isAgentV2Envelope(data) && data.type === 'turn.finished') {
              sawTerminalEvent = true;
              const activeMeta = streamMetaRef.current.get(sessionId);
              if (activeMeta?.streamToken === streamToken) {
                updateStreamState(sessionId, {
                  isLoading: false,
                  status: 'finalizing',
                  runId: data.run_id || activeMeta.runId || null,
                });
              }
            }

            if (onEvent(event, data)) {
              logger.debug('Stream handler requested break', { sessionId });
              sawTerminalEvent = true;
              shouldStop = true;
              break;
            }
          }
        }

        if (shouldStop) {
          await reader.cancel();
          break;
        }
      }

      if (onComplete) onComplete();
    } catch (error) {
      if (error.name === 'AbortError') {
        logger.info('Stream aborted by user', { sessionId });
        if (onError) onError(error, true);
      } else {
        logger.error('Stream error', { sessionId, error: error.message });
        if (onError) onError(error, false);
        await cancelBackendRun(sessionId, 'error');
      }
    } finally {
      // Clean up only if this stream is still the active one for the session.
      const activeMeta = streamMetaRef.current.get(sessionId);
      if (activeMeta?.streamToken === streamToken) {
        abortControllersRef.current.delete(sessionId);
        streamMetaRef.current.delete(sessionId);
        updateStreamState(sessionId, { isLoading: false, status: 'idle' });
      }
    }
  }, [cancelBackendRun, updateStreamState]);

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
    const streamToken = `${Date.now()}-${Math.random().toString(16).slice(2)}`;

    // Create new AbortController for this session
    const controller = new AbortController();
    abortControllersRef.current.set(sessionId, controller);

    // Store stream metadata
    streamMetaRef.current.set(sessionId, { streamUrl: url, runId: requestedRunId, runType: null, streamToken });

    // Update stream state
    updateStreamState(sessionId, {
      sessionId,
      isLoading: true,
      status: 'streaming',
      runId: requestedRunId,
    });

    try {
      const res = await authFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      logger.debug('Stream response', { sessionId, status: res.status });

      if (!res.ok || !res.body) {
        let detail = '';
        try {
          const data = await res.json();
          detail = data?.error || '';
        } catch {
          detail = '';
        }
        throw new Error(detail || `Stream failed with status ${res.status}`);
      }

      await processStream(sessionId, res.body.getReader(), streamToken, { onEvent, onComplete, onError });
    } catch (error) {
      if (error.name === 'AbortError') {
        logger.info('Stream aborted by user', { sessionId });
        if (onError) onError(error, true);
      } else {
        logger.error('Stream error', { sessionId, error: error.message });
        if (onError) onError(error, false);
      }

      // Clean up on error
      const activeMeta = streamMetaRef.current.get(sessionId);
      if (activeMeta?.streamToken === streamToken) {
        abortControllersRef.current.delete(sessionId);
        streamMetaRef.current.delete(sessionId);
        updateStreamState(sessionId, { isLoading: false, status: 'error' });
      }
    }
  }, [processStream, updateStreamState]);

  /**
   * Stop stream for a specific session.
   */
  const stopStreamForSession = useCallback(async (sessionId) => {
    if (!sessionId) return;

    const controller = abortControllersRef.current.get(sessionId);
    const meta = streamMetaRef.current.get(sessionId);

    if (controller) {
      logger.info('Stopping stream for session', { sessionId });

      // Call backend cancel API if we have runId
      await cancelBackendRun(sessionId, 'user_stop');

      // Abort the fetch
      controller.abort();
      abortControllersRef.current.delete(sessionId);
      streamMetaRef.current.delete(sessionId);
      updateStreamState(sessionId, { isLoading: false, status: 'idle', runId: null });
      return;
    }

    if (meta?.runId && meta?.streamUrl) {
      logger.info('Stopping pending run for session', { sessionId, runId: meta.runId });
      await cancelBackendRun(sessionId, 'user_stop');
      streamMetaRef.current.delete(sessionId);
      updateStreamState(sessionId, { isLoading: false, status: 'idle', runId: null });
    }
  }, [cancelBackendRun, updateStreamState]);

  /**
   * Stop all active streams (e.g., on page refresh/unload).
   */
  const stopAllStreams = useCallback(() => {
    const sessionIds = Array.from(abortControllersRef.current.keys());
    sessionIds.forEach((sessionId) => {
      stopStreamForSession(sessionId);
    });
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
    stopStreamForSession,
    getStreamState,
    isSessionLoading,
    activeSessionIds,

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
