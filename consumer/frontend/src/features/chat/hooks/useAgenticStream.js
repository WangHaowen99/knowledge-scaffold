/**
 * Hook for handling agent-stream.v2 SSE streams.
 */

import { useRef, useCallback, useLayoutEffect } from 'react';
import createLogger from '../../../shared/lib/logger';
import { authFetch } from '../../../shared/api/auth';
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

const normalizedTurnId = (turnId) => {
  if (turnId == null || String(turnId).trim() === '') return '';
  return String(turnId).trim();
};

const turnTrackingKey = (runId, turnId = null) => (
  `${runId}::${normalizedTurnId(turnId) || 'pending'}`
);

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
  endpoint: '/agentic/runs',
  buildPayload: (message, sessionId, options = {}) => ({
    request: message,
    session_id: sessionId,
    agent_mode: options.agentMode || 'quick',
    llm_request_mode: options.llmRequestMode || 'stream',
    model_profile_id: options.modelProfileId || '',
    arguments: options.arguments || null,
    include_trace: options.includeTrace === true,
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

  const clearRunTracking = useCallback((runId, turnId = null) => {
    if (!runId) return;
    const normalized = normalizedTurnId(turnId);
    if (normalized) {
      turnMessageIdsRef.current.delete(turnTrackingKey(runId, normalized));
      return;
    }
    const next = new Map(turnMessageIdsRef.current);
    for (const key of next.keys()) {
      if (key.startsWith(`${runId}::`)) next.delete(key);
    }
    turnMessageIdsRef.current = next;
  }, []);

  const rebindRunTracking = useCallback((fromRunId, toRunId, targetSessionId = null, options = {}) => {
    if (!fromRunId || !toRunId || fromRunId === toRunId) return;
    if (
      options.scopeEpoch !== undefined
      && !isMessageUpdateScopeCurrent(options.scopeEpoch, scopeEpochRef.current)
    ) return;

    const trackedEntries = Array.from(turnMessageIdsRef.current.entries())
      .filter(([key]) => key.startsWith(`${fromRunId}::`));
    const trackedId = trackedEntries[0]?.[1];
    if (trackedId) {
      const remappedId = trackedId === `${fromRunId}:turn` ? `${toRunId}:turn` : trackedId;
      trackedEntries.forEach(([key, messageId]) => {
        const turnId = key.slice(`${fromRunId}::`.length);
        turnMessageIdsRef.current.delete(key);
        turnMessageIdsRef.current.set(
          turnTrackingKey(toRunId, turnId === 'pending' ? null : turnId),
          messageId === `${fromRunId}:turn` ? remappedId : messageId,
        );
      });
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

      const currentTurnId = normalizedTurnId(current.turnId);
      turnMessageIdsRef.current.set(turnTrackingKey(toRunId, currentTurnId), nextId);
      return next;
    }, targetSessionId, options);
  }, [queueMessageUpdate]);

  const upsertTurnBubble = useCallback((
    runId,
    turnId,
    updater,
    targetSessionId = null,
    options = {},
  ) => {
    queueMessageUpdate((prev) => {
      const next = prev.slice();
      const eventTurnId = normalizedTurnId(turnId);
      const trackingKey = turnTrackingKey(runId, eventTurnId);
      const existingId = turnMessageIdsRef.current.get(trackingKey);

      let idx = existingId ? next.findIndex((msg) => msg.id === existingId) : -1;
      if (idx === -1) {
        idx = next.findIndex(
          (msg) => (
            msg.runId === runId
            && msg.kind === 'agent_step'
            && msg.stepLabel === TURN_STEP_LABEL
            && normalizedTurnId(msg.turnId) === eventTurnId
          )
        );
      }
      if (idx === -1 && eventTurnId) {
        idx = next.findIndex(
          (msg) => (
            msg.runId === runId
            && msg.kind === 'agent_step'
            && msg.stepLabel === TURN_STEP_LABEL
            && !normalizedTurnId(msg.turnId)
          )
        );
      }
      if (idx === -1 && !eventTurnId) {
        for (let index = next.length - 1; index >= 0; index -= 1) {
          const msg = next[index];
          if (msg.runId === runId && msg.kind === 'agent_step' && msg.stepLabel === TURN_STEP_LABEL) {
            idx = index;
            break;
          }
        }
      }

      if (idx === -1) {
        const id = existingId || (
          eventTurnId ? `${runId}:turn:${eventTurnId}` : `${runId}:turn`
        );
        const base = {
          id,
          runId,
          ...(eventTurnId ? { turnId: eventTurnId } : {}),
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
        turnMessageIdsRef.current.set(trackingKey, id);
        return next;
      }

      const current = next[idx];
      const patch = typeof updater === 'function' ? updater(current) : updater;
      next[idx] = {
        ...current,
        ...(eventTurnId ? { turnId: eventTurnId } : {}),
        ...patch,
        ts: Date.now(),
      };
      turnMessageIdsRef.current.set(trackingKey, next[idx].id);
      return next;
    }, targetSessionId, options);
  }, [queueMessageUpdate]);

  const finalizeErroredRun = useCallback((runId, errorText, targetSessionId = null, scopeEpoch = null, turnId = null) => {
    upsertTurnBubble(runId, turnId, (current) => {
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
    clearRunTracking(runId, turnId);
  }, [clearRunTracking, upsertTurnBubble]);

  const finalizeCancelledRun = useCallback((runId, errorText, targetSessionId = null, scopeEpoch = null, turnId = null) => {
    upsertTurnBubble(runId, turnId, (current) => {
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
    clearRunTracking(runId, turnId);
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

      // run.started is consumed by StreamManager for lifecycle state, but it
      // is not a turn event and must not create a visible assistant bubble.
      if (envelope.type === 'run.started') return false;

      const activeRunId = runLifecycle?.activeRunId || currentRunIdRef.current || runId;
      const eventRunId = envelope.run_id || activeRunId;
      const eventTurnId = normalizedTurnId(envelope.turn_id);
      const isTerminalEnvelope = envelope.type === 'turn.finished';
      if (envelope.run_id && envelope.run_id !== activeRunId) {
        rebindRunTracking(activeRunId, envelope.run_id, targetSessionId, {
          immediate: isTerminalEnvelope,
          scopeEpoch: runLifecycle?.scopeEpoch,
        });
      }

      if (runLifecycle) {
        runLifecycle.activeRunId = eventRunId;
        if (eventTurnId) runLifecycle.activeTurnId = eventTurnId;
      }
      currentRunIdRef.current = eventRunId;
      upsertTurnBubble(
        eventRunId,
        eventTurnId,
        (current) => applyAgentV2Event(current, envelope),
        targetSessionId,
        { immediate: isTerminalEnvelope, scopeEpoch: runLifecycle?.scopeEpoch },
      );

      if (isTerminalEnvelope) {
        clearRunTracking(eventRunId, eventTurnId);
      }

      return false;
    };
  }, [clearRunTracking, rebindRunTracking, upsertTurnBubble]);

  const createStreamCallbacks = useCallback((runId, targetSessionId, runLifecycle, onSessionUpdated = null) => ({
    onEvent: createEventDispatcher(runId, targetSessionId, runLifecycle),
    onComplete: (terminalStatus = 'success') => {
      if (!isMessageUpdateScopeCurrent(runLifecycle.scopeEpoch, scopeEpochRef.current)) return;
      const trackedRunId = runLifecycle.activeRunId || runId;
      const trackedTurnId = runLifecycle.activeTurnId || null;
      upsertTurnBubble(trackedRunId, trackedTurnId, (current) => ({
        ...current,
        status: current.status === 'running' || current.status === 'queued'
          ? messageStatusForTerminalRun(terminalStatus)
          : (current.status || 'finished'),
        isLoading: false,
        streamSegments: closeOpenSegment(current.streamSegments || []),
        currentSegmentType: null,
      }), targetSessionId, { immediate: true, scopeEpoch: runLifecycle.scopeEpoch });
      clearRunTracking(trackedRunId, trackedTurnId);
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
          runLifecycle.activeTurnId || null,
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
        runLifecycle.activeTurnId || null,
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
    const streamPayload = AGENTIC_CONFIG.buildPayload(
      message.trim(),
      activeSessionId,
      {
        agentMode,
        llmRequestMode: 'stream',
        modelProfileId,
        arguments: selectedOntologyId ? { selected_ontology_id: selectedOntologyId } : null,
        includeTrace: true,
      },
    );

    if (!streamManager || !activeSessionId) {
      logger.error('StreamManager unavailable or missing sessionId');
      return;
    }

    // HITL resumes the existing run and subscribes from its last cursor.
    if (interactionResponse) {
      const currentState = streamManager.getStreamState(activeSessionId);
      const interactionRunId = String(
        options?.interactionRunId || currentState?.runId || ''
      ).trim();
      if (!interactionRunId) {
        finalizeErroredRun(runId, 'Missing run id for interaction resume', activeSessionId);
        return;
      }
      try {
        const interactionResult = await authFetch(
          `/agentic/runs/${encodeURIComponent(interactionRunId)}/interact`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(interactionResponse),
          },
        );
        const interactionPayload = await interactionResult.json().catch(() => ({}));
        if (!interactionResult.ok) {
          const detail = interactionPayload?.error?.detail
            || interactionPayload?.error
            || 'Interaction resume failed';
          throw new Error(detail);
        }
        const interactionLifecycle = {
          activeRunId: interactionRunId,
          scopeEpoch: scopeEpochRef.current,
        };
        await streamManager.resumeRunForSession(
          activeSessionId,
          interactionRunId,
          currentState?.cursor || 0,
          {
            ...createStreamCallbacks(
              interactionRunId,
              activeSessionId,
              interactionLifecycle,
              onSessionUpdated,
            ),
            status: interactionPayload.status || 'running',
          },
        );
      } catch (error) {
        finalizeErroredRun(
          interactionRunId,
          error?.message || 'Interaction resume failed',
          activeSessionId,
        );
      }
      return;
    }

    upsertTurnBubble(runId, null, { status: 'running', isLoading: true }, activeSessionId, { immediate: true });

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
    finalizeErroredRun,
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
    upsertTurnBubble(normalizedRunId, null, (current) => ({
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
