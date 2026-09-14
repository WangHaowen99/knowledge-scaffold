// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import {
  isSessionResumableStatus,
  isSessionWaitingForUserStatus,
  normalizeSessionRunStatus,
} from './sessionModel.js';

export const createSessionScope = (workspaceId, epoch = 0) => ({
  workspaceId: String(workspaceId || ''),
  epoch: Number(epoch) || 0,
});

export const isSameSessionScope = (left, right) =>
  Boolean(left && right) &&
  left.workspaceId === right.workspaceId &&
  left.epoch === right.epoch;

export const shouldCommitSessionSelection = ({
  requestScope,
  currentScope,
  requestSelectionEpoch,
  currentSelectionEpoch,
}) =>
  isSameSessionScope(requestScope, currentScope) &&
  requestSelectionEpoch === currentSelectionEpoch;

export const selectedSessionStorageKey = (workspaceId) =>
  `onto_agent_current_session_${String(workspaceId || 'default')}`;

export const resolveInitialSessionSelection = ({
  persistedId = '',
  currentId = '',
  sessions = [],
  selectLatest = false,
}) => {
  const available = Array.isArray(sessions) ? sessions : [];
  if (selectLatest && available.length > 0) {
    return { sessionId: available[0].id, requiresDirectHydration: false };
  }
  if (persistedId) {
    return {
      sessionId: persistedId,
      requiresDirectHydration: !available.some(
        (session) => session.id === persistedId,
      ),
    };
  }
  if (currentId && available.some((session) => session.id === currentId)) {
    return { sessionId: currentId, requiresDirectHydration: false };
  }
  return {
    sessionId: available[0]?.id || null,
    requiresDirectHydration: false,
  };
};

export const mergeSessionCollections = (
  serverSessions = [],
  localSessions = [],
  deletedSessionIds = [],
) => {
  const deleted = new Set(deletedSessionIds || []);
  const merged = new Map();
  for (const session of serverSessions || []) {
    if (session?.id && !deleted.has(session.id)) {
      merged.set(session.id, session);
    }
  }
  for (const session of localSessions || []) {
    if (!session?.id || deleted.has(session.id)) {
      continue;
    }
    const server = merged.get(session.id) || {};
    merged.set(session.id, {
      ...server,
      ...session,
      metadata: { ...(server.metadata || {}), ...(session.metadata || {}) },
    });
  }
  return [...merged.values()];
};

export const normalizeSessionPage = (payload) => {
  if (!payload || !Array.isArray(payload.sessions)) {
    throw new TypeError('session page sessions must be an array');
  }
  const hasMore = payload.has_more === true;
  const nextCursor =
    typeof payload.next_cursor === 'string' && payload.next_cursor ?
      payload.next_cursor :
      null;
  if (hasMore && !nextCursor) {
    throw new TypeError(
      'session page cursor is required when more sessions exist',
    );
  }
  const indexRevision =
    Number.isInteger(payload.index_revision) && payload.index_revision >= 0 ?
      payload.index_revision :
      null;
  return {
    sessions: payload.sessions,
    nextCursor,
    hasMore,
    indexRevision,
  };
};

export const mergeSessionPage = ({
  current = [],
  incoming = [],
  append = false,
  selectedId = '',
  deletedSessionIds = [],
}) => {
  const deleted = new Set(deletedSessionIds || []);
  const currentById = new Map(
    (current || [])
      .filter((session) => session?.id && !deleted.has(session.id))
      .map((session) => [session.id, session]),
  );
  const merge = (previous, next) => ({
    ...(previous || {}),
    ...(next || {}),
    metadata: { ...(previous?.metadata || {}), ...(next?.metadata || {}) },
  });

  if (append) {
    const result = [...currentById.values()];
    const positionById = new Map(
      result.map((session, index) => [session.id, index]),
    );
    for (const session of incoming || []) {
      if (!session?.id || deleted.has(session.id)) {
        continue;
      }
      const position = positionById.get(session.id);
      if (position === undefined) {
        positionById.set(session.id, result.length);
        result.push(session);
      } else {
        result[position] = merge(result[position], session);
      }
    }
    return result;
  }

  const result = (incoming || []).filter(
    (session) => session?.id && !deleted.has(session.id),
  );
  if (selectedId && !result.some((session) => session.id === selectedId)) {
    const selected = currentById.get(selectedId);
    if (selected) {
      result.push(selected);
    }
  }
  return result;
};

export const isCurrentSessionRead = ({
  requestScope,
  currentScope,
  requestSequence,
  currentSequence,
  requestMutationEpoch,
  currentMutationEpoch,
  deleted = false,
}) =>
  !deleted &&
  isSameSessionScope(requestScope, currentScope) &&
  requestSequence === currentSequence &&
  requestMutationEpoch === currentMutationEpoch;

export const listResumableSessions = (sessions = []) =>
  (sessions || [])
    .filter(
      (session) =>
        session?.id &&
        session?.active_run_id &&
        isSessionResumableStatus(session?.active_run_status),
    )
    .map((session) => ({
      sessionId: session.id,
      runId: String(session.active_run_id),
      status: String(session.active_run_status),
      afterSequence: Number(session.last_seq) || 0,
    }));

export const resolveSessionRunIndicator = (session, streamState) => {
  if (streamState) {
    const streamStatus = normalizeSessionRunStatus(streamState.status);
    if (isSessionWaitingForUserStatus(streamStatus)) {
      return 'awaiting_input';
    }
    if (!streamState.isLoading) {
      return '';
    }
    return streamStatus === 'queued' ? 'queued' : 'running';
  }
  const status = normalizeSessionRunStatus(session?.active_run_status);
  if (isSessionWaitingForUserStatus(status)) {
    return 'awaiting_input';
  }
  return isSessionResumableStatus(status) ? status : '';
};
