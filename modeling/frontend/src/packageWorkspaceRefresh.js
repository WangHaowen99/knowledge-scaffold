import { advanceProjectEventReplayGate } from './projectEventReplay.js';

export const PACKAGE_WORKSPACE_CANDIDATE_EVENT_TYPES = new Set([
  'package.candidate.created',
  'package.revision.created',
  'optimization.expert_review.resolved',
]);

export function shouldLoadPackageWorkspaceRevisions({
  activeTab,
  embedded = false,
  deliveryView = '',
} = {}) {
  return activeTab === 'versions' || (embedded && activeTab === 'release' && deliveryView === 'versions');
}

export function planPackageWorkspaceWindowRefresh({
  activeTab,
  embedded = false,
  deliveryView = '',
} = {}) {
  return {
    scheduleLoad: true,
    loadRevisions: shouldLoadPackageWorkspaceRevisions({ activeTab, embedded, deliveryView }),
  };
}

export function planPackageWorkspaceProjectEvent({
  event,
  gate,
  workspaceDirty = false,
  activeTab,
  embedded = false,
  deliveryView = '',
} = {}) {
  const replay = advanceProjectEventReplayGate(gate, event);
  const interactionOpened = event?.type === 'authoring.interaction.opened' && !replay.historical;
  const revisionCreated = event?.type === 'package.revision.created';
  return {
    gate: replay.gate,
    historical: replay.historical,
    candidateRefresh: PACKAGE_WORKSPACE_CANDIDATE_EVENT_TYPES.has(String(event?.type ?? '')),
    immediateLoad: Boolean(interactionOpened && !workspaceDirty),
    scheduleLoad: Boolean(replay.refresh && !workspaceDirty),
    deferLoad: Boolean(workspaceDirty && (interactionOpened || replay.refresh)),
    loadRevisions: Boolean(
      replay.refresh
      && revisionCreated
      && shouldLoadPackageWorkspaceRevisions({ activeTab, embedded, deliveryView })
    ),
  };
}
