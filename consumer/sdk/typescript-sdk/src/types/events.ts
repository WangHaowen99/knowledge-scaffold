// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import type { SseEventMessage } from '../streams/sse.js';

export interface AgenticEventSource {
  node?: string;
  component?: string;
}

export interface AgenticEnvelope {
  protocol: 'agent-stream.v2';
  event_id?: string;
  seq?: number;
  ts?: number;
  session_id?: string;
  run_id?: string;
  turn_id?: number;
  type: string;
  source?: AgenticEventSource;
  payload?: Record<string, unknown>;
}

export interface A2ATaskUpdateEvent {
  task_id?: string;
  session_id?: string;
  event_type?: string;
  timestamp?: number;
  source?: AgenticEventSource;
  payload?: Record<string, unknown>;
}

export type NormalizedStreamEventKind =
  | 'run.started'
  | 'run.delta'
  | 'reasoning.delta'
  | 'reasoning.summary'
  | 'tool.started'
  | 'tool.progress'
  | 'tool.finished'
  | 'solver.input.awaiting_confirmation'
  | 'solver.progress'
  | 'solver.verification.completed'
  | 'solver.verification.rejected'
  | 'solver.result.authoritative'
  | 'subagent.started'
  | 'subagent.delta'
  | 'subagent.progress'
  | 'subagent.finished'
  | 'subagent_group.started'
  | 'subagent_group.finished'
  | 'run.completed'
  | 'run.awaiting'
  | 'run.failed'
  | 'run.cancelled'
  | 'raw';

export interface NormalizedStreamEvent {
  kind: NormalizedStreamEventKind;
  protocol: 'agentic' | 'a2a';
  eventName: string;
  runId?: string;
  taskId?: string;
  sessionId?: string;
  turnId?: number;
  sequence?: number;
  eventId?: string;
  timestamp?: number;
  source?: AgenticEventSource;
  payload: Record<string, unknown>;
  raw: unknown;
}

const toRecord = (value: unknown): Record<string, unknown> => {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
};

const normalizeTurnFinishedKind = (payload: Record<string, unknown>): NormalizedStreamEventKind => {
  const status = typeof payload.status === 'string' ? payload.status : '';
  switch (status) {
    case 'awaiting_input':
      return 'run.awaiting';
    case 'error':
      return 'run.failed';
    case 'cancelled':
      return 'run.cancelled';
    default:
      return 'run.completed';
  }
};

const normalizeEventKind = (eventType: string, payload: Record<string, unknown>): NormalizedStreamEventKind => {
  switch (eventType) {
    case 'turn.started':
      return 'run.started';
    case 'turn.finished':
      return normalizeTurnFinishedKind(payload);
    case 'token.delta':
      return payload.channel === 'reasoning' ? 'reasoning.delta' : 'run.delta';
    case 'reasoning.summary':
      return 'reasoning.summary';
    case 'tool.started':
    case 'tool.progress':
    case 'tool.finished':
    case 'solver.input.awaiting_confirmation':
    case 'solver.progress':
    case 'solver.verification.completed':
    case 'solver.verification.rejected':
    case 'solver.result.authoritative':
    case 'subagent.started':
    case 'subagent.delta':
    case 'subagent.progress':
    case 'subagent.finished':
    case 'subagent_group.started':
    case 'subagent_group.finished':
      return eventType;
    case 'task.error':
      return 'run.failed';
    default:
      return 'raw';
  }
};

export const normalizeAgenticEnvelope = (envelope: AgenticEnvelope): NormalizedStreamEvent => {
  const payload = toRecord(envelope.payload);
  return {
    kind: normalizeEventKind(envelope.type, payload),
    protocol: 'agentic',
    eventName: envelope.type,
    runId: envelope.run_id,
    sessionId: envelope.session_id,
    turnId: envelope.turn_id,
    sequence: envelope.seq,
    eventId: envelope.event_id,
    timestamp: envelope.ts,
    source: envelope.source,
    payload,
    raw: envelope,
  };
};

export const normalizeA2AUpdate = (update: A2ATaskUpdateEvent): NormalizedStreamEvent => {
  const payload = toRecord(update.payload);
  const eventName = update.event_type || 'task.update';
  return {
    kind: normalizeEventKind(eventName, payload),
    protocol: 'a2a',
    eventName,
    runId: update.task_id,
    taskId: update.task_id,
    sessionId: update.session_id,
    timestamp: update.timestamp,
    source: update.source,
    payload,
    raw: update,
  };
};

export const normalizeAgenticSseMessage = (message: SseEventMessage): NormalizedStreamEvent => {
  const json = message.json;
  if (message.event === 'agent.v2' && json && typeof json === 'object' && (json as Record<string, unknown>).protocol === 'agent-stream.v2') {
    const normalized = normalizeAgenticEnvelope(json as AgenticEnvelope);
    return {
      ...normalized,
      eventId: normalized.eventId || message.id,
    };
  }

  return {
    kind: 'raw',
    protocol: 'agentic',
    eventName: message.event,
    eventId: message.id,
    payload: {},
    raw: message,
  };
};

export const normalizeA2ASseMessage = (message: SseEventMessage): NormalizedStreamEvent => {
  const json = message.json;
  if (message.event === 'task.update' && json && typeof json === 'object') {
    return normalizeA2AUpdate(json as A2ATaskUpdateEvent);
  }

  return {
    kind: 'raw',
    protocol: 'a2a',
    eventName: message.event,
    payload: {},
    raw: message,
  };
};
