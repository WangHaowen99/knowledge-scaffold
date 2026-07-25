/**
 * Hook for handling agent-stream.v2 SSE streams.
 */

import { useRef, useCallback, useLayoutEffect } from 'react';
import createLogger from '../../../shared/lib/logger';
import {
  AGENT_STREAM_EVENT,
  applyAgentV2Event,
  closeOpenSegment,
  isAgentV2Envelope,
} from '../lib/streamHelpers';
import { messageStatusForTerminalRun } from '../lib/streamLifecycle';
import { isMessageUpdateScopeCurrent } from '../lib/streamUpdateScope';

const logger = createLogger('AgenticStream');

const TURN_STEP_LABEL = 'turn_stream';
const TURN_DISPLAY_NAME = '本体消费智能体';
const FRAME_FLUSH_DELAY_MS = 16;

const scheduleFrame = (callback) => {
  if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
    return {
      id: window.requestAnimationFrame(callback),
      type: 'raf',
    };
  }
  return {
    id: setTimeout(callback, FRAME_FLUSH_DELAY_MS),
    type: 'timeout',
  };
};

const cancelScheduledFrame = (handle) => {
  if (!handle) return;
  if (handle.type === 'raf' && typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
    window.cancelAnimationFrame(handle.id);
    return;
  }
  clearTimeout(handle.id);
};

const applyMessageUpdaters = (messages, updaters = []) => {
  return updaters.reduce((current, updater) => {
    return typeof updater === 'function' ? updater(current) : updater;
  }, messages);
};

const AGENTIC_CONFIG = {
  endpoint: '/agentic/runs/stream',
  buildPayload: (message, sessionId, context = {}, runId = null) => ({
    request: message,
    session_id: sessionId,
    run_id: runId,
    context,
  }),
};

const useAgenticStream = ({
  sessionId,
  setMessages,
  streamManager,
  scopeKey = '',
}) => {
  const currentRunIdRef = useRef(null);
  const turnMessageIdsRef = useRef(new Map());
  const queuedMessageUpdatesRef = useRef(new Map());
  const scheduledFlushRef = useRef(null);
  const scopeEpochRef = useRef(0);

  const resetPendingUpdates = useCallback(() => {
    cancelScheduledFrame(scheduledFlushRef.current);
    scheduledFlushRef.current = null;
    queuedMessageUpdatesRef.current = new Map();
    turnMessageIdsRef.current = new Map();
    currentRunIdRef.current = null;
  }, []);

  useLayoutEffect(() => {
    scopeEpochRef.current += 1;
    resetPendingUpdates();
    return () => {
      scopeEpochRef.current += 1;
      resetPendingUpdates();
    };
  }, [resetPendingUpdates, scopeKey]);

  const resolveTargetSessionId = useCallback((targetSessionId = null) => {
    return targetSessionId || sessionId || null;
  }, [sessionId]);

  const cancelScheduledFlushIfIdle = useCallback(() => {
    if (queuedMessageUpdatesRef.current.size > 0 || !scheduledFlushRef.current) {
      return;
    }
    cancelScheduledFrame(scheduledFlushRef.current);
    scheduledFlushRef.current = null;
  }, []);

  const flushQueuedMessageUpdates = useCallback((targetSessionId = null, immediateUpdater = null) => {
    const resolvedSessionId = resolveTargetSessionId(targetSessionId);
    if (!resolvedSessionId) return;

    const entry = queuedMessageUpdatesRef.current.get(resolvedSessionId);
    const queued = entry && isMessageUpdateScopeCurrent(entry.epoch, scopeEpochRef.current)
      ? entry.updaters
      : [];
    if (queued.length === 0 && !immediateUpdater) {
      cancelScheduledFlushIfIdle();
      return;
    }

    const nextQueued = new Map(queuedMessageUpdatesRef.current);
    nextQueued.delete(resolvedSessionId);
    queuedMessageUpdatesRef.current = nextQueued;
    cancelScheduledFlushIfIdle();

    const updaters = immediateUpdater ? [...queued, immediateUpdater] : queued;
    setMessages((prev) => applyMessageUpdaters(prev, updaters), resolvedSessionId);
  }, [cancelScheduledFlushIfIdle, resolveTargetSessionId, setMessages]);

  const flushAllQueuedMessageUpdates = useCallback(() => {
    scheduledFlushRef.current = null;
    const entries = Array.from(queuedMessageUpdatesRef.current.entries());
    if (entries.length === 0) {
      return;
    }
    queuedMessageUpdatesRef.current = new Map();
    entries.forEach(([targetSessionId, entry]) => {
      if (
        !targetSessionId
        || !isMessageUpdateScopeCurrent(entry?.epoch, scopeEpochRef.current)
        || !Array.isArray(entry?.updaters)
        || entry.updaters.length === 0
      ) {
        return;
      }
      setMessages((prev) => applyMessageUpdaters(prev, entry.updaters), targetSessionId);
    });
  }, [setMessages]);

  const ensureScheduledFlush = useCallback(() => {
    if (scheduledFlushRef.current) return;
    scheduledFlushRef.current = scheduleFrame(() => {
      flushAllQueuedMessageUpdates();
    });
  }, [flushAllQueuedMessageUpdates]);

  const queueMessageUpdate = useCallback((updater, targetSessionId = null, options = {}) => {
    const resolvedSessionId = resolveTargetSessionId(targetSessionId);
    if (!resolvedSessionId) return;
    const updateEpoch = options.scopeEpoch ?? scopeEpochRef.current;
    if (!isMessageUpdateScopeCurrent(updateEpoch, scopeEpochRef.current)) return;

    if (options.immediate) {
      flushQueuedMessageUpdates(resolvedSessionId, updater);
      return;
    }

    const nextQueued = new Map(queuedMessageUpdatesRef.current);
    const existing = nextQueued.get(resolvedSessionId);
    const existingUpdaters = existing && isMessageUpdateScopeCurrent(existing.epoch, updateEpoch)
      ? existing.updaters
      : [];
    nextQueued.set(resolvedSessionId, {
      epoch: updateEpoch,
      updaters: [...existingUpdaters, updater],
    });
    queuedMessageUpdatesRef.current = nextQueued;
    ensureScheduledFlush();
  }, [ensureScheduledFlush, flushQueuedMessageUpdates, resolveTargetSessionId]);

  const clearRunTracking = useCallback((runId) => {
    if (!runId) return;
    turnMessageIdsRef.current.delete(runId);
  }, []);

  const rebindRunTracking = useCallback((fromRunId, toRunId, targetSessionId = null, options = {}) => {
    if (!fromRunId || !toRunId || fromRunId === toRunId) return;
    if (
      options.scopeEpoch !== undefined
      && !isMessageUpdateScopeCurrent(options.scopeEpoch, scopeEpochRef.current)
    ) return;

    const trackedId = turnMessageIdsRef.current.get(fromRunId);
    if (trackedId) {
      const remappedId = trackedId === `${fromRunId}:turn` ? `${toRunId}:turn` : trackedId;
      turnMessageIdsRef.current.delete(fromRunId);
      turnMessageIdsRef.current.set(toRunId, remappedId);
    }

    queueMessageUpdate((prev) => {
      const next = prev.slice();
      let idx = trackedId ? next.findIndex((msg) => msg.id === trackedId) : -1;

      if (idx === -1) {
        idx = next.findIndex(
          (msg) => msg.runId === fromRunId && msg.kind === 'agent_step' && msg.stepLabel === TURN_STEP_LABEL
        );
      }

      if (idx === -1) return prev;

      const current = next[idx];
      const nextId = current.id === `${fromRunId}:turn` ? `${toRunId}:turn` : current.id;
      next[idx] = {
        ...current,
        id: nextId,
        runId: toRunId,
        ts: Date.now(),
      };

      turnMessageIdsRef.current.set(toRunId, nextId);
      turnMessageIdsRef.current.delete(fromRunId);
      return next;
    }, targetSessionId, options);
  }, [queueMessageUpdate]);

  const upsertTurnBubble = useCallback((runId, updater, targetSessionId = null, options = {}) => {
    queueMessageUpdate((prev) => {
      const next = prev.slice();
      const existingId = turnMessageIdsRef.current.get(runId);

      let idx = existingId ? next.findIndex((msg) => msg.id === existingId) : -1;
      if (idx === -1) {
        idx = next.findIndex(
          (msg) => msg.runId === runId && msg.kind === 'agent_step' && msg.stepLabel === TURN_STEP_LABEL
        );
      }

      if (idx === -1) {
        const id = existingId || `${runId}:turn`;
        const base = {
          id,
          runId,
          role: 'assistant',
          kind: 'agent_step',
          stepLabel: TURN_STEP_LABEL,
          displayName: TURN_DISPLAY_NAME,
          status: 'running',
          content: '',
          reasoning: '',
          tools: '',
          streamSegments: [],
          iterationMetrics: [],
          currentSegmentType: null,
          options: null,
          messageType: 'text',
          isLoading: true,
          ts: Date.now(),
        };
        const patch = typeof updater === 'function' ? updater(base) : updater;
        next.push({ ...base, ...patch, ts: Date.now() });
        turnMessageIdsRef.current.set(runId, id);
        return next;
      }

      const current = next[idx];
      const patch = typeof updater === 'function' ? updater(current) : updater;
      next[idx] = { ...current, ...patch, ts: Date.now() };
      turnMessageIdsRef.current.set(runId, next[idx].id);
      return next;
    }, targetSessionId, options);
  }, [queueMessageUpdate]);

  const finalizeErroredRun = useCallback((runId, errorText, targetSessionId = null, scopeEpoch = null) => {
    upsertTurnBubble(runId, (current) => {
      const next = applyAgentV2Event(current, {
        protocol: 'agent-stream.v2',
        type: 'turn.finished',
        payload: {
          status: 'error',
          error: errorText,
        },
        source: { node: 'client', component: 'stream' },
      });
      return {
        ...next,
        streamSegments: closeOpenSegment(next.streamSegments || []),
      };
    }, targetSessionId, { immediate: true, scopeEpoch });
    clearRunTracking(runId);
  }, [clearRunTracking, upsertTurnBubble]);

  const finalizeCancelledRun = useCallback((runId, errorText, targetSessionId = null, scopeEpoch = null) => {
    upsertTurnBubble(runId, (current) => {
      const next = applyAgentV2Event(current, {
        protocol: 'agent-stream.v2',
        type: 'turn.finished',
        payload: {
          status: 'cancelled',
          error: errorText,
          cancel_reason: 'user_stop',
        },
        source: { node: 'client', component: 'stream' },
      });
      return {
        ...next,
        streamSegments: closeOpenSegment(next.streamSegments || []),
      };
    }, targetSessionId, { immediate: true, scopeEpoch });
    clearRunTracking(runId);
  }, [clearRunTracking, upsertTurnBubble]);

  const createEventDispatcher = useCallback((runId, targetSessionId = null, runLifecycle = null) => {
    return (eventLabel, data) => {
      if (
        runLifecycle?.scopeEpoch !== undefined
        && !isMessageUpdateScopeCurrent(runLifecycle.scopeEpoch, scopeEpochRef.current)
      ) return false;
      let envelope = null;
      if (eventLabel === AGENT_STREAM_EVENT && isAgentV2Envelope(data)) {
        envelope = data;
      } else if (eventLabel === 'message' && data?.event === AGENT_STREAM_EVENT && isAgentV2Envelope(data?.data)) {
        envelope = data.data;
      }

      if (!envelope) {
        logger.debug('Ignoring non-agent.v2 SSE event', { eventLabel });
        return false;
      }

      const activeRunId = runLifecycle?.activeRunId || currentRunIdRef.current || runId;
      const eventRunId = envelope.run_id || activeRunId;
      const isTerminalEnvelope = envelope.type === 'turn.finished';
      if (envelope.run_id && envelope.run_id !== activeRunId) {
        rebindRunTracking(activeRunId, envelope.run_id, targetSessionId, {
          immediate: isTerminalEnvelope,
          scopeEpoch: runLifecycle?.scopeEpoch,
        });
      }

      if (runLifecycle) {
        runLifecycle.activeRunId = eventRunId;
      }
      currentRunIdRef.current = eventRunId;
      upsertTurnBubble(
        eventRunId,
        (current) => applyAgentV2Event(current, envelope),
        targetSessionId,
        { immediate: isTerminalEnvelope, scopeEpoch: runLifecycle?.scopeEpoch },
      );

      if (isTerminalEnvelope) {
        clearRunTracking(eventRunId);
      }

      return false;
    };
  }, [clearRunTracking, rebindRunTracking, upsertTurnBubble]);

  const createStreamCallbacks = useCallback((runId, targetSessionId, runLifecycle, onSessionUpdated = null) => ({
    onEvent: createEventDispatcher(runId, targetSessionId, runLifecycle),
    onComplete: (terminalStatus = 'success') => {
      if (!isMessageUpdateScopeCurrent(runLifecycle.scopeEpoch, scopeEpochRef.current)) return;
      const trackedRunId = runLifecycle.activeRunId || runId;
      upsertTurnBubble(trackedRunId, (current) => ({
        ...current,
        status: current.status === 'running' || current.status === 'queued'
          ? messageStatusForTerminalRun(terminalStatus)
          : (current.status || 'finished'),
        isLoading: false,
        streamSegments: closeOpenSegment(current.streamSegments || []),
        currentSegmentType: null,
      }), targetSessionId, { immediate: true, scopeEpoch: runLifecycle.scopeEpoch });
      clearRunTracking(trackedRunId);
      onSessionUpdated?.();
    },
    onError: (error, isAbort) => {
      if (!isMessageUpdateScopeCurrent(runLifecycle.scopeEpoch, scopeEpochRef.current)) return;
      const trackedRunId = runLifecycle.activeRunId || runId;
      if (isAbort) {
        finalizeCancelledRun(
          trackedRunId,
          'Workflow cancelled by user',
          targetSessionId,
          runLifecycle.scopeEpoch,
        );
        onSessionUpdated?.();
        return;
      }
      logger.error('Agentic stream error', { error: error.message });
      finalizeErroredRun(
        trackedRunId,
        error.message || 'Unknown stream error',
        targetSessionId,
        runLifecycle.scopeEpoch,
      );
      onSessionUpdated?.();
    },
  }), [
    clearRunTracking,
    createEventDispatcher,
    finalizeCancelledRun,
    finalizeErroredRun,
    upsertTurnBubble,
  ]);

  const sendAgentic = useCallback(async (message, existingSessionId = null, options = {}) => {
    if (!message.trim()) return;

    const activeSessionId = existingSessionId || sessionId;
    const runId = `agentic-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const runLifecycle = { activeRunId: runId, scopeEpoch: scopeEpochRef.current };
    currentRunIdRef.current = runId;
    const agentMode = typeof options?.agentMode === 'string'
      ? options.agentMode.trim()
      : 'quick';
    const selectedOntologyId = typeof options?.selectedOntologyId === 'string'
      ? options.selectedOntologyId.trim()
      : '';
    const selectedOntologyVersion = typeof options?.selectedOntologyVersion === 'string'
      ? options.selectedOntologyVersion.trim()
      : '';
    const modelProfileId = typeof options?.modelProfileId === 'string'
      ? options.modelProfileId.trim()
      : '';
    const interactionResponse = options?.interactionResponse && typeof options.interactionResponse === 'object'
      ? options.interactionResponse
      : null;
    const onSessionUpdated = typeof options?.onSessionUpdated === 'function'
      ? options.onSessionUpdated
      : null;

    queueMessageUpdate((prev) => {
      const next = prev.slice();
      next.push({
        role: 'user',
        content: message.trim(),
      });
      return next;
    }, activeSessionId, { immediate: true });

    logger.info('Starting agentic workflow', { runId, sessionId: activeSessionId });

    const streamUrl = AGENTIC_CONFIG.endpoint;
    const contextPayload = {};
    if (selectedOntologyId) {
      contextPayload.selected_ontology_id = selectedOntologyId;
    }
    if (selectedOntologyVersion) {
      contextPayload.selected_ontology_version = selectedOntologyVersion;
    }
    contextPayload.agent_mode = agentMode;
    if (modelProfileId) {
      contextPayload.model_profile_id = modelProfileId;
    }
    if (interactionResponse) {
      contextPayload.interaction_response = interactionResponse;
    }
    const streamPayload = AGENTIC_CONFIG.buildPayload(
      message.trim(),
      activeSessionId,
      contextPayload,
      runId,
    );

    if (!streamManager || !activeSessionId) {
      logger.error('StreamManager unavailable or missing sessionId');
      return;
    }

    upsertTurnBubble(runId, { status: 'running', isLoading: true }, activeSessionId, { immediate: true });

    const streamCallbacks = createStreamCallbacks(
      runId,
      activeSessionId,
      runLifecycle,
      onSessionUpdated,
    );

    await streamManager.startStreamForSession(
      activeSessionId,
      streamUrl,
      streamPayload,
      streamCallbacks
    );
  }, [
    createStreamCallbacks,
    queueMessageUpdate,
    sessionId,
    streamManager,
    upsertTurnBubble,
  ]);

  const resumeAgentic = useCallback(async ({
    runId,
    targetSessionId = null,
    afterSequence = 0,
    status = 'running',
  }) => {
    const activeSessionId = targetSessionId || sessionId;
    const normalizedRunId = String(runId || '').trim();
    if (!activeSessionId || !normalizedRunId || !streamManager?.resumeRunForSession) return false;

    const runLifecycle = { activeRunId: normalizedRunId, scopeEpoch: scopeEpochRef.current };
    currentRunIdRef.current = normalizedRunId;
    upsertTurnBubble(normalizedRunId, (current) => ({
      ...current,
      status: status === 'queued' ? 'queued' : 'running',
      isLoading: true,
    }), activeSessionId, { immediate: true });
    return streamManager.resumeRunForSession(
      activeSessionId,
      normalizedRunId,
      afterSequence,
      {
        ...createStreamCallbacks(normalizedRunId, activeSessionId, runLifecycle),
        status,
      },
    );
  }, [createStreamCallbacks, sessionId, streamManager, upsertTurnBubble]);

  return {
    sendAgentic,
    resumeAgentic,
  };
};

export default useAgenticStream;
