import {
  isSessionResumableStatus,
  isSessionWaitingForUserStatus,
  normalizeSessionRunStatus,
} from './sessionModel.js';

export const createSessionScope = (workspaceId, epoch = 0) => ({
  workspaceId: String(workspaceId || ''),
  epoch: Number(epoch) || 0,
});

export const isSameSessionScope = (left, right) => (
  Boolean(left && right)
  && left.workspaceId === right.workspaceId
  && left.epoch === right.epoch
);

export const shouldCommitSessionSelection = ({
  requestScope,
  currentScope,
  requestSelectionEpoch,
  currentSelectionEpoch,
}) => (
  isSameSessionScope(requestScope, currentScope)
  && requestSelectionEpoch === currentSelectionEpoch
);

export const selectedSessionStorageKey = (workspaceId) => (
  `onto_agent_current_session_${String(workspaceId || 'default')}`
);

export const mergeSessionCollections = (
  serverSessions = [],
  localSessions = [],
  deletedSessionIds = [],
) => {
  const deleted = new Set(deletedSessionIds || []);
  const merged = new Map();
  for (const session of serverSessions || []) {
    if (session?.id && !deleted.has(session.id)) merged.set(session.id, session);
  }
  for (const session of localSessions || []) {
    if (!session?.id || deleted.has(session.id)) continue;
    const server = merged.get(session.id) || {};
    merged.set(session.id, {
      ...server,
      ...session,
      metadata: { ...(server.metadata || {}), ...(session.metadata || {}) },
    });
  }
  return [...merged.values()];
};

export const isCurrentSessionRead = ({
  requestScope,
  currentScope,
  requestSequence,
  currentSequence,
  requestMutationEpoch,
  currentMutationEpoch,
  deleted = false,
}) => (
  !deleted
  && isSameSessionScope(requestScope, currentScope)
  && requestSequence === currentSequence
  && requestMutationEpoch === currentMutationEpoch
);

export const listResumableSessions = (sessions = []) => (sessions || [])
  .filter((session) => (
    session?.id
    && session?.active_run_id
    && isSessionResumableStatus(session?.active_run_status)
  ))
  .map((session) => ({
    sessionId: session.id,
    runId: String(session.active_run_id),
    status: String(session.active_run_status),
    afterSequence: Number(session.last_seq) || 0,
  }));

export const resolveSessionRunIndicator = (session, streamState) => {
  if (streamState) {
    const streamStatus = normalizeSessionRunStatus(streamState.status);
    if (isSessionWaitingForUserStatus(streamStatus)) return 'awaiting_input';
    if (!streamState.isLoading) return '';
    return streamStatus === 'queued'
      ? 'queued'
      : 'running';
  }
  const status = normalizeSessionRunStatus(session?.active_run_status);
  if (isSessionWaitingForUserStatus(status)) return 'awaiting_input';
  return isSessionResumableStatus(status) ? status : '';
};
