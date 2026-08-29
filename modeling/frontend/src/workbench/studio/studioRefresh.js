import { advanceProjectEventReplayGate } from '../../projectEventReplay.js';

export const STUDIO_PROJECT_REFRESH_EVENT_TYPES = new Set([
  'package.revision.created',
]);

export function createStudioProjectEventBaseline({
  projectId = '',
  snapshot = null,
} = {}) {
  const normalizedProjectId = String(projectId ?? '').trim();
  const snapshotCursor = Number(snapshot?.authoring?.lastEventSeq) || 0;
  return {
    projectId: normalizedProjectId,
    cursor: Math.max(0, snapshotCursor),
    ready: Boolean(normalizedProjectId),
  };
}

export function studioHasPendingLocalEdits(session = null) {
  const drafts = session?.entryDrafts;
  return Boolean(drafts && Object.keys(drafts).length > 0);
}

export function studioRefreshNoticeMessage(revisionId = '') {
  const revision = String(revisionId ?? '').trim();
  if (!revision) return '知识包已有新版本，当前保留你的本地输入。';
  return `知识包已有新版本（${revision.slice(0, 12)}），当前保留你的本地输入。`;
}

export function planStudioWindowRefresh({ localEdits = false } = {}) {
  return {
    scheduleLoad: !localEdits,
    deferLoad: localEdits,
  };
}

export function planStudioProjectEvent({
  event,
  gate,
  localEdits = false,
  currentRevision = '',
} = {}) {
  const replay = advanceProjectEventReplayGate(gate, event);
  const eventType = String(event?.type ?? '');
  const revisionId = String(
    event?.payload && typeof event.payload === 'object' && !Array.isArray(event.payload)
      ? event.payload.revisionId ?? ''
      : '',
  ).trim();
  const relevant = STUDIO_PROJECT_REFRESH_EVENT_TYPES.has(eventType);
  const sameRevision = Boolean(revisionId && revisionId === String(currentRevision ?? '').trim());
  const shouldRefresh = relevant && replay.refresh && !sameRevision;

  return {
    gate: replay.gate,
    historical: replay.historical,
    relevant,
    revisionId,
    scheduleLoad: Boolean(shouldRefresh && !localEdits),
    deferLoad: Boolean(shouldRefresh && localEdits),
  };
}
