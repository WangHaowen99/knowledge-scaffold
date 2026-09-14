// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
/**
 * Utility for building chat messages from stored events.
 */

import createLogger from '../../../shared/lib/logger.js';
import { applyPublicEvent, closeOpenSegment } from './streamHelpers.js';
import { storedAgentEventToPublicEvent } from './legacyAgentEvent.js';
import { publicEventTimestamp } from './publicEvent.js';
import {
  applyInteractionCancellation,
  INTERACTION_CANCELLED_TYPE,
} from './pendingInteraction.js';

const logger = createLogger('MessageBuilder');

const TURN_STEP_LABEL = 'turn_stream';
const TURN_DISPLAY_NAME = '本体消费智能体';

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export const buildMessagesFromEvents = (events = []) => {
  const messages = [];
  const turnMessageIndexByKey = new Map();

  const ensureTurnBubble = (runId, turnId = null) => {
    const normalizedTurnId = turnId == null ? '' : String(turnId).trim();
    const turnKey = `${runId}::${normalizedTurnId || 'pending'}`;
    if (turnMessageIndexByKey.has(turnKey)) {
      return turnMessageIndexByKey.get(turnKey);
    }

    const existingIdx = messages.findIndex(
      (msg) =>
        msg.runId === runId &&
        msg.kind === 'agent_step' &&
        msg.stepLabel === TURN_STEP_LABEL &&
        String(msg.turnId ?? '').trim() === normalizedTurnId,
    );
    if (existingIdx !== -1) {
      turnMessageIndexByKey.set(turnKey, existingIdx);
      return existingIdx;
    }

    const idx = messages.length;
    messages.push({
      id: normalizedTurnId ?
        `${runId}:turn:${normalizedTurnId}` :
        `${runId}:turn`,
      runId,
      ...(normalizedTurnId ? { turnId: normalizedTurnId } : {}),
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
    });

    turnMessageIndexByKey.set(turnKey, idx);
    return idx;
  };

  logger.debug(`Processing ${events.length} events`);

  events.forEach((event, eventIndex) => {
    const eventLabel = event?.event;
    const data = event?.data;

    if (eventLabel === 'chat_message') {
      const role = data?.role;
      const content = data?.content || '';
      const metadata =
        data?.metadata && typeof data.metadata === 'object' ?
          data.metadata :
          {};
      if (!role) {
        return;
      }

      if (metadata.type === INTERACTION_CANCELLED_TYPE) {
        const next = applyInteractionCancellation(messages, {
          interaction_id: metadata.interaction_id,
          cancelled_at: metadata.cancelled_at,
          display_message: content,
        });
        messages.splice(0, messages.length, ...next);
        return;
      }

      messages.push({
        id: `${role}-${messages.length}`,
        role,
        content,
        metadata,
      });
      return;
    }

    const publicEvent = storedAgentEventToPublicEvent(event, {
      index: eventIndex,
    });
    if (!publicEvent) {
      return;
    }

    // run.started is a run-level control event. It has no turn-scoped
    // rendering data and must not create a phantom assistant bubble during
    // durable session hydration.
    if (publicEvent.event_type === 'run.started') {
      return;
    }

    const runId = publicEvent.run_id || 'unknown-run';
    const idx = ensureTurnBubble(runId, publicEvent.turn_id);
    const current = messages[idx];

    const next = applyPublicEvent(current, publicEvent);
    messages[idx] = {
      ...next,
      ts: publicEventTimestamp(publicEvent),
    };
  });

  return messages.map((msg) => {
    if (msg?.kind !== 'agent_step') {
      return msg;
    }
    return {
      ...msg,
      streamSegments: closeOpenSegment(msg.streamSegments || []),
      isLoading: msg.status === 'running',
      currentSegmentType: null,
    };
  });
};

export const historyToMessages = (history = []) => {
  if (!history || history.length === 0) {
    return [];
  }
  // eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
  return history.map((item, idx) => {
    const metadata = item?.metadata || {};
    const runId =
      metadata.run_id || metadata.runId || item?.run_id || item?.runId || null;
    const status =
      item?.status ||
      metadata.status ||
      (item?.role === 'assistant' && runId ? 'completed' : undefined);

    return {
      id: item?.id || `hist-${idx}`,
      role: item?.role,
      content: item?.content,
      metadata,
      ...(runId ? { runId } : {}),
      ...(status ? { status } : {}),
    };
  });
};
