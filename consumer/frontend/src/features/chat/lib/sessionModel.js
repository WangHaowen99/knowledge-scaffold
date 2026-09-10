import { normalizeAgentMode } from './workspacePreferences.js';

const RESUMABLE_STATUSES = new Set(['queued', 'running']);
const WAITING_STATUSES = new Set(['awaiting', 'awaiting_input']);

export const normalizeSessionRunStatus = (status) =>
  String(status || '')
    .trim()
    .toLowerCase();

export const isSessionResumableStatus = (status) =>
  RESUMABLE_STATUSES.has(normalizeSessionRunStatus(status));

export const isSessionWaitingForUserStatus = (status) =>
  WAITING_STATUSES.has(normalizeSessionRunStatus(status));

const sessionTimestamp = (session) => {
  const timestamp = Date.parse(
    session?.updated_at || session?.created_at || '',
  );
  return Number.isFinite(timestamp) ? timestamp : 0;
};

export const sortSessions = (sessions = []) =>
  [...sessions].sort(
    (left, right) =>
      Number(Boolean(right?.metadata?.pinned)) -
        Number(Boolean(left?.metadata?.pinned)) ||
      sessionTimestamp(right) - sessionTimestamp(left),
  );

const withPinnedState = (session, pinned, timestamp) => {
  const metadata = { ...(session?.metadata || {}), pinned };
  if (pinned) {
    metadata.pinned_at = timestamp;
  } else {
    delete metadata.pinned_at;
  }
  return { ...session, metadata };
};

export const beginSessionPinUpdate = (
  sessions,
  sessionId,
  pinned,
  timestamp = new Date().toISOString(),
) => {
  const previousSession =
    (sessions || []).find((session) => session.id === sessionId) || null;
  const optimistic = sortSessions(
    (sessions || []).map((session) =>
      session.id === sessionId ?
        withPinnedState(session, pinned, timestamp) :
        session,
    ),
  );
  return { sessionId, previousSession, optimistic };
};

export const commitSessionPinUpdate = (sessions, sessionId, serverSession) =>
  sortSessions(
    (sessions || []).map((session) => {
      if (session.id !== sessionId) {
        return session;
      }
      return {
        ...session,
        ...(serverSession || {}),
        metadata: {
          ...(session.metadata || {}),
          ...(serverSession?.metadata || {}),
        },
      };
    }),
  );

export const rollbackSessionPinUpdate = (sessions, transaction) => {
  if (!transaction?.previousSession) {
    return sortSessions(sessions || []);
  }
  return sortSessions(
    (sessions || []).map((session) =>
      session.id === transaction.sessionId ?
        transaction.previousSession :
        session,
    ),
  );
};

export const resolveSessionPreferences = ({
  metadata = {},
  storedAgentMode = '',
  storedModelProfileId = '',
  storedOntologyId = '',
  storedOntologyVersion = '',
  defaultModelProfileId = '',
} = {}) => ({
  agentMode: normalizeAgentMode(
    metadata.agent_mode || storedAgentMode || 'quick',
  ),
  modelProfileId: String(
    metadata.model_profile_id ||
      storedModelProfileId ||
      defaultModelProfileId ||
      '',
  ).trim(),
  selectedOntologyId: String(
    metadata.selected_ontology_id || storedOntologyId || '',
  ).trim(),
  selectedOntologyVersion: String(
    metadata.selected_ontology_version ||
      metadata.selected_version ||
      storedOntologyVersion ||
      '',
  ).trim(),
});
