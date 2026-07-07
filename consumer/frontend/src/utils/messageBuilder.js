/**
 * Utility for building chat messages from stored events.
 */

import createLogger from './logger';
import {
  AGENT_STREAM_EVENT,
  applyAgentV2Event,
  closeOpenSegment,
  isAgentV2Envelope,
} from './streamHelpers';

const logger = createLogger('MessageBuilder');

const TURN_STEP_LABEL = 'turn_stream';
const TURN_DISPLAY_NAME = '本体消费智能体';

export const buildMessagesFromEvents = (events = []) => {
  const messages = [];
  const turnMessageIndexByRun = new Map();

  const ensureTurnBubble = (runId) => {
    if (turnMessageIndexByRun.has(runId)) {
      return turnMessageIndexByRun.get(runId);
    }

    const existingIdx = messages.findIndex(
      (msg) => msg.runId === runId && msg.kind === 'agent_step' && msg.stepLabel === TURN_STEP_LABEL
    );
    if (existingIdx !== -1) {
      turnMessageIndexByRun.set(runId, existingIdx);
      return existingIdx;
    }

    const idx = messages.length;
    messages.push({
      id: `${runId}:turn`,
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
    });

    turnMessageIndexByRun.set(runId, idx);
    return idx;
  };

  logger.debug(`Processing ${events.length} events`);

  events.forEach((event) => {
    const eventLabel = event?.event;
    const data = event?.data;

    if (eventLabel === 'chat_message') {
      const role = data?.role;
      const content = data?.content || '';
      if (!role) return;

      messages.push({
        id: `${role}-${messages.length}`,
        role,
        content,
      });
      return;
    }

    if (eventLabel !== AGENT_STREAM_EVENT || !isAgentV2Envelope(data)) {
      return;
    }

    const runId = data.run_id || 'unknown-run';
    const idx = ensureTurnBubble(runId);
    const current = messages[idx];

    const next = applyAgentV2Event(current, data);
    messages[idx] = {
      ...next,
      ts: data.ts || Date.now(),
    };
  });

  return messages.map((msg) => {
    if (msg?.kind !== 'agent_step') return msg;
    return {
      ...msg,
      streamSegments: closeOpenSegment(msg.streamSegments || []),
      isLoading: msg.status === 'running',
      currentSegmentType: null,
    };
  });
};

export const historyToMessages = (history = []) => {
  if (!history || history.length === 0) return [];
  return history.map((item, idx) => {
    const metadata = item?.metadata || {};
    const runId = metadata.run_id || metadata.runId || item?.run_id || item?.runId || null;
    const status = item?.status || metadata.status || (item?.role === 'assistant' && runId ? 'completed' : undefined);

    return {
      id: item?.id || `hist-${idx}`,
      role: item?.role,
      content: item?.content,
      ...(runId ? { runId } : {}),
      ...(status ? { status } : {}),
    };
  });
};
