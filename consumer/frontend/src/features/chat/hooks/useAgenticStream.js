/**
 * Hook for projecting canonical PublicEvent streams into chat messages.
 */

import { useRef, useCallback, useLayoutEffect } from 'react';
import createLogger from '../../../shared/lib/logger';
import {
  applyLocalRunTermination,
  applyPublicEvent,
  closeOpenSegment,
} from '../lib/streamHelpers';
import { isPublicEvent } from '../lib/publicEvent';
import {
  buildRunRequestPayload,
  createIdempotencyKey,
} from '../lib/runAdmission';
import { messageStatusForTerminalRun } from '../lib/streamLifecycle';
import { isMessageUpdateScopeCurrent } from '../lib/streamUpdateScope';

const logger = createLogger('AgenticStream');

const TURN_STEP_LABEL = 'turn_stream';
const TURN_DISPLAY_NAME = '本体消费智能体';
const FRAME_FLUSH_DELAY_MS = 16;

const normalizedTurnId = (turnId) => {
  if (turnId == null || String(turnId).trim() === '') {
    return '';
  }
  return String(turnId).trim();
};

const turnTrackingKey = (runId, turnId = null) =>
  `${runId}::${normalizedTurnId(turnId) || 'pending'}`;

const scheduleFrame = (callback) => {
  if (
    typeof window !== 'undefined' &&
    typeof window.requestAnimationFrame === 'function'
  ) {
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
  if (!handle) {
    return;
  }
  if (
    handle.type === 'raf' &&
    typeof window !== 'undefined' &&
    typeof window.cancelAnimationFrame === 'function'
  ) {
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

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
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

  const resolveTargetSessionId = useCallback(
    (targetSessionId = null) => {
      return targetSessionId || sessionId || null;
    },
    [sessionId],
  );

  const cancelScheduledFlushIfIdle = useCallback(() => {
    if (
      queuedMessageUpdatesRef.current.size > 0 ||
      !scheduledFlushRef.current
    ) {
      return;
    }
    cancelScheduledFrame(scheduledFlushRef.current);
    scheduledFlushRef.current = null;
  }, []);

  const flushQueuedMessageUpdates = useCallback(
    (targetSessionId = null, immediateUpdater = null) => {
      const resolvedSessionId = resolveTargetSessionId(targetSessionId);
      if (!resolvedSessionId) {
        return;
      }

      const entry = queuedMessageUpdatesRef.current.get(resolvedSessionId);
      const queued =
        entry && isMessageUpdateScopeCurrent(entry.epoch, scopeEpochRef.current) ?
          entry.updaters :
          [];
      if (queued.length === 0 && !immediateUpdater) {
        cancelScheduledFlushIfIdle();
        return;
      }

      const nextQueued = new Map(queuedMessageUpdatesRef.current);
      nextQueued.delete(resolvedSessionId);
      queuedMessageUpdatesRef.current = nextQueued;
      cancelScheduledFlushIfIdle();

      const updaters = immediateUpdater ?
        [...queued, immediateUpdater] :
        queued;
      setMessages(
        (prev) => applyMessageUpdaters(prev, updaters),
        resolvedSessionId,
      );
    },
    [cancelScheduledFlushIfIdle, resolveTargetSessionId, setMessages],
  );

  const flushAllQueuedMessageUpdates = useCallback(() => {
    scheduledFlushRef.current = null;
    const entries = Array.from(queuedMessageUpdatesRef.current.entries());
    if (entries.length === 0) {
      return;
    }
    queuedMessageUpdatesRef.current = new Map();
    entries.forEach(([targetSessionId, entry]) => {
      const updateInvalid =
        !targetSessionId ||
        !isMessageUpdateScopeCurrent(entry?.epoch, scopeEpochRef.current) ||
        !Array.isArray(entry?.updaters) ||
        entry.updaters.length === 0;
      if (updateInvalid) {
        return;
      }
      setMessages(
        (prev) => applyMessageUpdaters(prev, entry.updaters),
        targetSessionId,
      );
    });
  }, [setMessages]);

  const ensureScheduledFlush = useCallback(() => {
    if (scheduledFlushRef.current) {
      return;
    }
    scheduledFlushRef.current = scheduleFrame(() => {
      flushAllQueuedMessageUpdates();
    });
  }, [flushAllQueuedMessageUpdates]);

  const queueMessageUpdate = useCallback(
    (updater, targetSessionId = null, options = {}) => {
      const resolvedSessionId = resolveTargetSessionId(targetSessionId);
      if (!resolvedSessionId) {
        return;
      }
      const updateEpoch = options.scopeEpoch ?? scopeEpochRef.current;
      if (!isMessageUpdateScopeCurrent(updateEpoch, scopeEpochRef.current)) {
        return;
      }

      if (options.immediate) {
        flushQueuedMessageUpdates(resolvedSessionId, updater);
        return;
      }

      const nextQueued = new Map(queuedMessageUpdatesRef.current);
      const existing = nextQueued.get(resolvedSessionId);
      const existingUpdaters =
        existing && isMessageUpdateScopeCurrent(existing.epoch, updateEpoch) ?
          existing.updaters :
          [];
      nextQueued.set(resolvedSessionId, {
        epoch: updateEpoch,
        updaters: [...existingUpdaters, updater],
      });
      queuedMessageUpdatesRef.current = nextQueued;
      ensureScheduledFlush();
    },
    [ensureScheduledFlush, flushQueuedMessageUpdates, resolveTargetSessionId],
  );

  const clearRunTracking = useCallback((runId, turnId = null) => {
    if (!runId) {
      return;
    }
    const normalized = normalizedTurnId(turnId);
    if (normalized) {
      turnMessageIdsRef.current.delete(turnTrackingKey(runId, normalized));
      return;
    }
    const next = new Map(turnMessageIdsRef.current);
    for (const key of next.keys()) {
      if (key.startsWith(`${runId}::`)) {
        next.delete(key);
      }
    }
    turnMessageIdsRef.current = next;
  }, []);

  const upsertTurnBubble = useCallback(
    (runId, turnId, updater, targetSessionId = null, options = {}) => {
      queueMessageUpdate(
        (prev) => {
          const next = prev.slice();
          const eventTurnId = normalizedTurnId(turnId);
          const trackingKey = turnTrackingKey(runId, eventTurnId);
          const existingId = turnMessageIdsRef.current.get(trackingKey);

          let idx = existingId ?
            next.findIndex((msg) => msg.id === existingId) :
            -1;
          if (idx === -1) {
            idx = next.findIndex(
              (msg) =>
                msg.runId === runId &&
                msg.kind === 'agent_step' &&
                msg.stepLabel === TURN_STEP_LABEL &&
                normalizedTurnId(msg.turnId) === eventTurnId,
            );
          }
          if (idx === -1 && eventTurnId) {
            idx = next.findIndex(
              (msg) =>
                msg.runId === runId &&
                msg.kind === 'agent_step' &&
                msg.stepLabel === TURN_STEP_LABEL &&
                !normalizedTurnId(msg.turnId),
            );
          }
          if (idx === -1 && !eventTurnId) {
            for (let index = next.length - 1; index >= 0; index -= 1) {
              const msg = next[index];
              if (
                msg.runId === runId &&
                msg.kind === 'agent_step' &&
                msg.stepLabel === TURN_STEP_LABEL
              ) {
                idx = index;
                break;
              }
            }
          }

          if (idx === -1) {
            const id =
              existingId ||
              (eventTurnId ? `${runId}:turn:${eventTurnId}` : `${runId}:turn`);
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
            const patch =
              typeof updater === 'function' ? updater(base) : updater;
            next.push({ ...base, ...patch, ts: Date.now() });
            turnMessageIdsRef.current.set(trackingKey, id);
            return next;
          }

          const current = next[idx];
          const patch =
            typeof updater === 'function' ? updater(current) : updater;
          next[idx] = {
            ...current,
            ...(eventTurnId ? { turnId: eventTurnId } : {}),
            ...patch,
            ts: Date.now(),
          };
          turnMessageIdsRef.current.set(trackingKey, next[idx].id);
          return next;
        },
        targetSessionId,
        options,
      );
    },
    [queueMessageUpdate],
  );

  const finalizeErroredRun = useCallback(
    (
      runId,
      errorText,
      targetSessionId = null,
      scopeEpoch = null,
      turnId = null,
    ) => {
      upsertTurnBubble(
        runId,
        turnId,
        (current) => {
          const next = applyLocalRunTermination(current, {
            status: 'failed',
            error: errorText,
          });
          return {
            ...next,
            streamSegments: closeOpenSegment(next.streamSegments || []),
          };
        },
        targetSessionId,
        { immediate: true, scopeEpoch },
      );
      clearRunTracking(runId, turnId);
    },
    [clearRunTracking, upsertTurnBubble],
  );

  const finalizeCancelledRun = useCallback(
    (
      runId,
      errorText,
      targetSessionId = null,
      scopeEpoch = null,
      turnId = null,
    ) => {
      upsertTurnBubble(
        runId,
        turnId,
        (current) => {
          const next = applyLocalRunTermination(current, {
            status: 'cancelled',
            error: errorText,
            cancelReason: 'user_stop',
          });
          return {
            ...next,
            streamSegments: closeOpenSegment(next.streamSegments || []),
          };
        },
        targetSessionId,
        { immediate: true, scopeEpoch },
      );
      clearRunTracking(runId, turnId);
    },
    [clearRunTracking, upsertTurnBubble],
  );

  const createEventDispatcher = useCallback(
    (runId, targetSessionId = null, runLifecycle = null) => {
      return (publicEvent) => {
        if (
          runLifecycle?.scopeEpoch !== undefined &&
          !isMessageUpdateScopeCurrent(
            runLifecycle.scopeEpoch,
            scopeEpochRef.current,
          )
        ) {
          return false;
        }
        if (!isPublicEvent(publicEvent)) {
          logger.debug('Ignoring invalid PublicEvent');
          return false;
        }

        // run.started is consumed by StreamManager for lifecycle state, but it
        // is not a turn event and must not create a visible assistant bubble.
        if (publicEvent.event_type === 'run.started') {
          return false;
        }

        const activeRunId =
          runLifecycle?.activeRunId || currentRunIdRef.current || runId;
        if (activeRunId && publicEvent.run_id !== activeRunId) {
          logger.warn('Ignoring PublicEvent for another run', {
            activeRunId,
            eventRunId: publicEvent.run_id,
          });
          return false;
        }
        const eventRunId = publicEvent.run_id;
        const eventTurnId = normalizedTurnId(publicEvent.turn_id);
        const isTerminalEvent = publicEvent.event_type === 'turn.finished';

        if (runLifecycle) {
          runLifecycle.activeRunId = eventRunId;
          if (eventTurnId) {
            runLifecycle.activeTurnId = eventTurnId;
          }
        }
        currentRunIdRef.current = eventRunId;
        upsertTurnBubble(
          eventRunId,
          eventTurnId,
          (current) => applyPublicEvent(current, publicEvent),
          targetSessionId,
          { immediate: isTerminalEvent, scopeEpoch: runLifecycle?.scopeEpoch },
        );

        if (isTerminalEvent) {
          clearRunTracking(eventRunId, eventTurnId);
        }

        return false;
      };
    },
    [clearRunTracking, upsertTurnBubble],
  );

  const createStreamCallbacks = useCallback(
    (runId, targetSessionId, runLifecycle, onSessionUpdated = null) => ({
      onEvent: createEventDispatcher(runId, targetSessionId, runLifecycle),
      onComplete: (terminalStatus = 'completed') => {
        if (
          !isMessageUpdateScopeCurrent(
            runLifecycle.scopeEpoch,
            scopeEpochRef.current,
          )
        ) {
          return;
        }
        const trackedRunId = runLifecycle.activeRunId || runId;
        if (!trackedRunId) {
          return;
        }
        const trackedTurnId = runLifecycle.activeTurnId || null;
        upsertTurnBubble(
          trackedRunId,
          trackedTurnId,
          (current) => ({
            ...current,
            status:
              current.status === 'running' || current.status === 'queued' ?
                messageStatusForTerminalRun(terminalStatus) :
                current.status || 'finished',
            isLoading: false,
            streamSegments: closeOpenSegment(current.streamSegments || []),
            currentSegmentType: null,
          }),
          targetSessionId,
          { immediate: true, scopeEpoch: runLifecycle.scopeEpoch },
        );
        clearRunTracking(trackedRunId, trackedTurnId);
        onSessionUpdated?.();
      },
      onError: (error, isAbort) => {
        if (
          !isMessageUpdateScopeCurrent(
            runLifecycle.scopeEpoch,
            scopeEpochRef.current,
          )
        ) {
          return;
        }
        const trackedRunId = runLifecycle.activeRunId || runId;
        if (!trackedRunId) {
          onSessionUpdated?.();
          return;
        }
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
    }),
    [
      clearRunTracking,
      createEventDispatcher,
      finalizeCancelledRun,
      finalizeErroredRun,
      upsertTurnBubble,
    ],
  );

  const sendAgentic = useCallback(
    // eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
    async (message, existingSessionId = null, options = {}) => {
      if (!message.trim()) {
        return;
      }

      const activeSessionId = existingSessionId || sessionId;
      const runLifecycle = {
        activeRunId: '',
        scopeEpoch: scopeEpochRef.current,
      };
      const agentMode =
        typeof options?.agentMode === 'string' ?
          options.agentMode.trim() :
          'quick';
      const selectedOntologyId =
        typeof options?.selectedOntologyId === 'string' ?
          options.selectedOntologyId.trim() :
          '';
      const modelProfileId =
        typeof options?.modelProfileId === 'string' ?
          options.modelProfileId.trim() :
          '';
      const interactionResponse =
        options?.interactionResponse &&
        typeof options.interactionResponse === 'object' ?
          options.interactionResponse :
          null;
      const onSessionUpdated =
        typeof options?.onSessionUpdated === 'function' ?
          options.onSessionUpdated :
          null;

      if (!streamManager?.startRunForSession || !activeSessionId) {
        logger.error('StreamManager unavailable or missing sessionId');
        return;
      }

      queueMessageUpdate(
        (prev) => {
          const next = prev.slice();
          next.push({
            role: 'user',
            content: message.trim(),
          });
          return next;
        },
        activeSessionId,
        { immediate: true },
      );

      logger.info('Starting agentic workflow', { sessionId: activeSessionId });

      const streamPayload = buildRunRequestPayload(
        message.trim(),
        activeSessionId,
        {
          agentMode,
          modelProfileId,
          arguments: selectedOntologyId ?
            { selected_ontology_id: selectedOntologyId } :
            null,
          includeTrace: true,
          interactionResponse,
        },
      );

      const streamCallbacks = createStreamCallbacks(
        '',
        activeSessionId,
        runLifecycle,
        onSessionUpdated,
      );

      await streamManager.startRunForSession(activeSessionId, streamPayload, {
        ...streamCallbacks,
        idempotencyKey:
          String(options?.idempotencyKey || '').trim() ||
          createIdempotencyKey(),
        onAdmission: (admission) => {
          runLifecycle.activeRunId = admission.runId;
          runLifecycle.activeTurnId = admission.turnId;
          currentRunIdRef.current = admission.runId;
          upsertTurnBubble(
            admission.runId,
            admission.turnId,
            {
              status: admission.status === 'queued' ? 'queued' : 'running',
              isLoading: true,
            },
            activeSessionId,
            { immediate: true, scopeEpoch: runLifecycle.scopeEpoch },
          );
        },
      });
    },
    [
      createStreamCallbacks,
      queueMessageUpdate,
      sessionId,
      streamManager,
      upsertTurnBubble,
    ],
  );

  const resumeAgentic = useCallback(
    async ({
      runId,
      targetSessionId = null,
      afterSequence = 0,
      status = 'running',
    }) => {
      const activeSessionId = targetSessionId || sessionId;
      const normalizedRunId = String(runId || '').trim();
      if (
        !activeSessionId ||
        !normalizedRunId ||
        !streamManager?.resumeRunForSession
      ) {
        return false;
      }

      const runLifecycle = {
        activeRunId: normalizedRunId,
        scopeEpoch: scopeEpochRef.current,
      };
      currentRunIdRef.current = normalizedRunId;
      upsertTurnBubble(
        normalizedRunId,
        null,
        (current) => ({
          ...current,
          status: status === 'queued' ? 'queued' : 'running',
          isLoading: true,
        }),
        activeSessionId,
        { immediate: true },
      );
      return streamManager.resumeRunForSession(
        activeSessionId,
        normalizedRunId,
        afterSequence,
        {
          ...createStreamCallbacks(
            normalizedRunId,
            activeSessionId,
            runLifecycle,
          ),
          status,
        },
      );
    },
    [createStreamCallbacks, sessionId, streamManager, upsertTurnBubble],
  );

  return {
    sendAgentic,
    resumeAgentic,
  };
};

export default useAgenticStream;
