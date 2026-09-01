import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  ArrowLeft,
  Boxes,
  Check,
  FileCode2,
  Gauge,
  Network,
  Plus,
  RefreshCw,
  Save,
  Scale,
  Search,
  Table2,
  Trash2,
  Workflow,
  X,
} from 'lucide-react';

import { store } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import EvidenceLinks from '../center/EvidenceLinks.jsx';
import { getProjectSession } from '../../authoring-api.js';
import { protectUnsavedBeforeUnload } from '../../flowchartNavigationGuard.js';
import {
  batchStudioElements,
  applyStudioDeletions,
  createStudioElement,
  getStudioBootstrap,
  getStudioElement,
  getStudioElements,
  getStudioGraph,
  invalidateStudioBootstrap,
  previewStudioDeletions,
  requestStudioReview,
  studioError,
  updateStudioElement,
} from './studio-api.js';
import {
  applyStudioFocus,
  KIND_META,
  readStudioSession,
  restoreStudioHistory,
  clearStudioSearch,
  shouldSubmitStudioSearch,
  stageStudioSearch,
  STUDIO_TARGET,
  studioSearchDraft,
  studioTextMatchesQuery,
  submitStudioSearch,
  studioHistoryFromLocation,
  studioHistorySnapshot,
  studioHistoryUrl,
  studioVisibleElementId,
  writeStudioSession,
} from './studio-model.js';
import {
  isStudioNoChangesFailure,
  studioChangedFields,
  studioDeleteCopy,
  studioDraftEqual,
  studioDraftFieldValue,
  studioIncompleteDraftField,
  studioRelationElements,
  studioSaveStateAfterDraftChange,
  studioSyncedSaveState,
} from './studio-entry-state.js';
import { useProjectEvents } from '../../projectEventStream.jsx';
import { createProjectEventReplayGate } from '../../projectEventReplay.js';
import StudioGraph from './StudioGraph.jsx';
import {
  createStudioProjectEventBaseline,
  markStudioDraftsForRefresh,
  planStudioProjectEvent,
  planStudioWindowRefresh,
  studioHasPendingLocalEdits,
  studioPendingDraftRefresh,
  studioRefreshNoticeMessage,
} from './studioRefresh.js';
import './studio.css';

const selProjectId = (state) => state.projectId;
const selMaterials = (state) => state.materials;
const KIND_ICON = { object: Boxes, operation: Workflow, rule: Scale, metric: Gauge, script: FileCode2 };
const STUDIO_REFRESH_DEBOUNCE_MS = 200;

export default function StudioView({ focus }) {
  const projectId = useStore(store, selProjectId);
  const materials = useStore(store, selMaterials);
  const [session, setSession] = useState(() => initialStudioSession(projectId));
  const [createKind, setCreateKind] = useState('');
  const [deleteIds, setDeleteIds] = useState([]);
  const [externalRefreshNotice, setExternalRefreshNotice] = useState({ pending: false, message: '', revisionId: '' });
  const [reviewSubmitState, setReviewSubmitState] = useState({ phase: 'idle', message: '' });
  const [projectEventBaseline, setProjectEventBaseline] = useState({ projectId: '', cursor: 0, ready: false });
  const sessionRef = useRef(session);
  const requestRef = useRef(0);
  const loadAbortRef = useRef(null);
  const refreshStateRef = useRef({ gate: null, loadTimer: null });
  const deferredRefreshRef = useRef(null);
  const localEditsRef = useRef(studioHasPendingLocalEdits(session));
  const externalRefreshNoticeRef = useRef(externalRefreshNotice);

  useEffect(() => { externalRefreshNoticeRef.current = externalRefreshNotice; }, [externalRefreshNotice]);

  const update = useCallback((patch, historyMode = 'replace') => {
    const previous = sessionRef.current;
    const next = typeof patch === 'function' ? patch(previous) : { ...previous, ...patch };
    sessionRef.current = next;
    writeStudioSession(projectId, next);
    setSession(next);
    if (historyMode) writeBrowserHistory(next, historyMode, projectId);
    return next;
  }, [projectId]);
  const setExternalNotice = useCallback((next) => {
    externalRefreshNoticeRef.current = next;
    setExternalRefreshNotice(next);
  }, []);
  const clearExternalNotice = useCallback(() => {
    if (!externalRefreshNoticeRef.current.pending) return;
    setExternalNotice({ pending: false, message: '', revisionId: '' });
  }, [setExternalNotice]);

  const markRequestFailure = useCallback((error) => {
    const failure = studioError(error);
    update({ phase: failure.conflict ? 'conflict' : 'error', error: failure, detailLoading: false, loadingMore: false });
  }, [update]);

  const loadDetail = useCallback(async (elementId, revision = sessionRef.current.workingRevision) => {
    if (!projectId || !elementId || !revision) return;
    update({ selectedElementId: elementId, detailLoading: true });
    try {
      const response = await getStudioElement(projectId, elementId, revision);
      if (response.workingRevision !== sessionRef.current.workingRevision) return;
      update((current) => ({
        ...current,
        detailLoading: false,
        details: { ...current.details, [elementId]: response.element },
        elements: current.elements.some((item) => item.id === response.element.id)
          ? current.elements
          : [...current.elements, elementSummary(response.element)],
      }));
    } catch (error) {
      markRequestFailure(error);
    }
  }, [markRequestFailure, projectId, update]);

  const loadStudio = useCallback(async (options = {}) => {
    if (!projectId) return;
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    const hasContent = Boolean(sessionRef.current.schema && sessionRef.current.graph);
    update({ phase: hasContent ? 'refreshing' : 'loading', error: null });
    try {
      const bootstrap = await getStudioBootstrap(projectId, { limit: 200 }, {
        signal: controller.signal,
        cache: options.cache,
      });
      const { schema, graph } = bootstrap;
      const list = bootstrap.elements;
      if (requestRef.current !== requestId) return;
      const previous = sessionRef.current;
      const selectedExists = list.items.some((item) => item.id === previous.selectedElementId)
        || graph.nodes.some((node) => node.id === previous.selectedElementId);
      const selectedElementId = selectedExists ? previous.selectedElementId : '';
      const revisionChanged = previous.workingRevision !== list.workingRevision;
      const pendingDraftRefresh = studioPendingDraftRefresh(previous.entryDrafts, list.workingRevision);
      update({
        phase: 'ready',
        error: null,
        schema,
        elements: list.items,
        total: list.total,
        nextCursor: list.nextCursor,
        graph,
        workingRevision: list.workingRevision,
        studioPendingReview: Boolean(list.hasStagedChanges && !list.candidateRevision && !list.reviewRequested),
        selectedElementId,
        view: previous.view === 'entry' && !selectedElementId ? previous.returnView : previous.view,
        details: revisionChanged ? {} : previous.details,
      });
      store.set({ studioPendingCount: list.hasStagedChanges ? 1 : 0 });
      if (pendingDraftRefresh.pending) {
        deferredRefreshRef.current = { revisionId: pendingDraftRefresh.revisionId };
        setExternalNotice({
          pending: true,
          message: studioRefreshNoticeMessage(pendingDraftRefresh.revisionId),
          revisionId: pendingDraftRefresh.revisionId,
        });
      } else {
        deferredRefreshRef.current = null;
        clearExternalNotice();
      }
      if (selectedElementId) await loadDetail(selectedElementId, list.workingRevision);
    } catch (error) {
      if (!controller.signal.aborted && requestRef.current === requestId) markRequestFailure(error);
    } finally {
      if (loadAbortRef.current === controller) loadAbortRef.current = null;
    }
  }, [clearExternalNotice, loadDetail, markRequestFailure, projectId, setExternalNotice, update]);
  const reloadStudio = useCallback(() => loadStudio({ cache: 'reload' }), [loadStudio]);

  const discardLocalDraftsAndLoadStudio = useCallback(async () => {
    localEditsRef.current = false;
    deferredRefreshRef.current = null;
    invalidateStudioBootstrap(projectId);
    update({ entryDrafts: {}, details: {} });
    await loadStudio();
  }, [loadStudio, projectId, update]);

  useEffect(() => {
    const dirty = studioHasPendingLocalEdits(session);
    if (!dirty) return undefined;
    const beforeUnload = (event) => { protectUnsavedBeforeUnload(event, true); };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [session.entryDrafts]);
  const scheduleStudioRefresh = useCallback(() => {
    const state = refreshStateRef.current;
    if (state.loadTimer) return;
    state.loadTimer = setTimeout(() => {
      state.loadTimer = null;
      void reloadStudio();
    }, STUDIO_REFRESH_DEBOUNCE_MS);
  }, [reloadStudio]);

  useEffect(() => {
    const cached = initialStudioSession(projectId);
    writeStudioSession(projectId, cached);
    sessionRef.current = cached;
    setSession(cached);
    localEditsRef.current = studioHasPendingLocalEdits(cached);
    deferredRefreshRef.current = null;
    setExternalNotice({ pending: false, message: '', revisionId: '' });
  }, [projectId, setExternalNotice]);

  useEffect(() => {
    let active = true;
    setProjectEventBaseline({ projectId: '', cursor: 0, ready: false });
    if (!projectId) {
      return () => { active = false; };
    }
    void getProjectSession(projectId)
      .then((snapshot) => {
        if (!active) return;
        setProjectEventBaseline(createStudioProjectEventBaseline({
          projectId,
          snapshot,
        }));
      })
      .catch(() => {
        if (!active) return;
        setProjectEventBaseline(createStudioProjectEventBaseline({
          projectId,
        }));
      });
    return () => { active = false; };
  }, [projectId]);

  useEffect(() => {
    const onPopState = (event) => {
      const snapshot = event.state?.studio;
      if (!snapshot || (snapshot.projectId && snapshot.projectId !== projectId)) return;
      const restored = restoreStudioHistory(sessionRef.current, snapshot);
      update(restored, '');
      store.set({
        focus: {
          kind: 'studio',
          target: STUDIO_TARGET,
          view: restored.view,
          ...(restored.selectedElementId ? { elementId: restored.selectedElementId } : {}),
        },
        follow: false,
        pendingFocus: null,
      });
      if (restored.selectedElementId && restored.workingRevision && !restored.details[restored.selectedElementId]) {
        void loadDetail(restored.selectedElementId, restored.workingRevision);
      }
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [loadDetail, projectId, update]);

  useEffect(() => {
    const next = applyStudioFocus(sessionRef.current, focus);
    if (next !== sessionRef.current) update(next);
    if (next.selectedElementId && next.workingRevision && !next.details[next.selectedElementId]) {
      void loadDetail(next.selectedElementId, next.workingRevision);
    }
  }, [focus, loadDetail, update]);

  useEffect(() => {
    const elementId = session.selectedElementId;
    if (!elementId || !session.workingRevision || session.details[elementId] || session.detailLoading) return;
    void loadDetail(elementId, session.workingRevision);
  }, [loadDetail, session.detailLoading, session.details, session.selectedElementId, session.workingRevision]);

  useEffect(() => {
    if (!projectEventBaseline.ready || projectEventBaseline.projectId !== projectId) return undefined;
    void loadStudio();
    return () => {
      loadAbortRef.current?.abort();
      loadAbortRef.current = null;
      requestRef.current += 1;
      if (refreshStateRef.current.loadTimer) clearTimeout(refreshStateRef.current.loadTimer);
      refreshStateRef.current.loadTimer = null;
    };
  }, [loadStudio, projectEventBaseline.projectId, projectEventBaseline.ready, projectId]);

  const entryDraftDirty = studioHasPendingLocalEdits(session);
  useEffect(() => {
    localEditsRef.current = entryDraftDirty;
    if (!entryDraftDirty && deferredRefreshRef.current) {
      scheduleStudioRefresh();
    }
  }, [entryDraftDirty, scheduleStudioRefresh]);

  useEffect(() => {
    if (!entryDraftDirty || !externalRefreshNotice.pending) return;
    update((current) => ({
      ...current,
      entryDrafts: markStudioDraftsForRefresh(
        current.entryDrafts,
        externalRefreshNotice.revisionId,
      ),
    }));
  }, [entryDraftDirty, externalRefreshNotice.pending, externalRefreshNotice.revisionId, update]);

  useEffect(() => {
    if (!projectEventBaseline.ready || projectEventBaseline.projectId !== projectId) {
      refreshStateRef.current.gate = null;
      return undefined;
    }
    refreshStateRef.current.gate = createProjectEventReplayGate(projectEventBaseline.cursor);
    return () => {
      refreshStateRef.current.gate = null;
    };
  }, [projectEventBaseline.cursor, projectEventBaseline.projectId, projectEventBaseline.ready, projectId]);

  const handleProjectEvent = useCallback((event) => {
    const state = refreshStateRef.current;
    state.gate ??= createProjectEventReplayGate(projectEventBaseline.cursor);
    const plan = planStudioProjectEvent({
      event,
      gate: state.gate,
      localEdits: localEditsRef.current,
      currentRevision: sessionRef.current.workingRevision,
    });
    state.gate = plan.gate;
    if (plan.deferLoad) {
      update((current) => ({
        ...current,
        entryDrafts: markStudioDraftsForRefresh(current.entryDrafts, plan.revisionId),
      }));
      deferredRefreshRef.current = { revisionId: plan.revisionId || '' };
      setExternalNotice({
        pending: true,
        message: studioRefreshNoticeMessage(plan.revisionId),
        revisionId: plan.revisionId || '',
      });
      return;
    }
    if (plan.scheduleLoad) {
      deferredRefreshRef.current = null;
      scheduleStudioRefresh();
    }
  }, [projectEventBaseline.cursor, scheduleStudioRefresh, setExternalNotice, update]);

  useEffect(() => {
    const refresh = () => {
      const plan = planStudioWindowRefresh({ localEdits: localEditsRef.current });
      if (plan.deferLoad) {
        update((current) => ({
          ...current,
          entryDrafts: markStudioDraftsForRefresh(current.entryDrafts),
        }));
        deferredRefreshRef.current = { revisionId: '' };
        setExternalNotice({
          pending: true,
          message: studioRefreshNoticeMessage(''),
          revisionId: '',
        });
        return;
      }
      deferredRefreshRef.current = null;
      scheduleStudioRefresh();
    };
    window.addEventListener('mw-package-updated', refresh);
    const refreshOnVisible = () => {
      if (document.visibilityState === 'visible') refresh();
    };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refreshOnVisible);
    return () => {
      window.removeEventListener('mw-package-updated', refresh);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refreshOnVisible);
    };
  }, [scheduleStudioRefresh, setExternalNotice, update]);

  const navigate = useCallback((view, elementId = sessionRef.current.selectedElementId) => {
    const next = {
      ...sessionRef.current,
      view,
      selectedElementId: elementId || '',
      returnView: view === 'entry'
        ? sessionRef.current.returnView
        : view,
    };
    update(next, 'push');
    store.set({
      focus: { kind: 'studio', target: STUDIO_TARGET, view, ...(elementId ? { elementId } : {}) },
      follow: false,
      pendingFocus: null,
    });
  }, [update]);

  const openDetail = useCallback((elementId, returnView = sessionRef.current.view) => {
    update({ selectedElementId: elementId, returnView: returnView === 'table' ? 'table' : 'graph' });
    navigate('entry', elementId);
    if (!sessionRef.current.details[elementId]) void loadDetail(elementId);
  }, [loadDetail, navigate, update]);

  const submitPendingReview = useCallback(async () => {
    if (!sessionRef.current.studioPendingReview || reviewSubmitState.phase === 'saving') return;
    if (studioHasPendingLocalEdits(sessionRef.current)) {
      setReviewSubmitState({
        phase: 'error',
        message: '当前条目仍有未保存或不合法的输入，请先修正后再检查并应用',
      });
      return;
    }
    setReviewSubmitState({ phase: 'saving', message: '正在执行确定性检查并应用修改' });
    try {
      const response = await requestStudioReview(projectId, {
        expectedRevision: sessionRef.current.workingRevision,
        clientMutationId: mutationId('studio-review-recovery'),
      });
      update({ studioPendingReview: false });
      if (response.applied) {
        store.set({ studioPendingCount: 0 });
        setReviewSubmitState({ phase: 'saved', message: response.display?.text || '确定性检查通过，修改已应用' });
        await loadStudio();
        window.dispatchEvent(new Event('mw-package-updated'));
      } else {
        setReviewSubmitState({ phase: 'saved', message: response.display?.text || '已提交复核' });
      }
    } catch (error) {
      setReviewSubmitState({ phase: 'error', message: studioError(error).message });
    }
  }, [loadStudio, projectId, reviewSubmitState.phase, update]);

  const loadMore = useCallback(async () => {
    const current = sessionRef.current;
    if (!current.nextCursor || current.loadingMore) return;
    update({ loadingMore: true });
    try {
      const page = await getStudioElements(projectId, {
        cursor: current.nextCursor,
        limit: 200,
        workingRevision: current.workingRevision,
      });
      update((latest) => ({
        ...latest,
        loadingMore: false,
        elements: [...new Map([...latest.elements, ...page.items].map((item) => [item.id, item])).values()],
        total: page.total,
        nextCursor: page.nextCursor,
      }));
    } catch (error) {
      markRequestFailure(error);
    }
  }, [markRequestFailure, projectId, update]);

  const applyMutation = useCallback(async (response) => {
    const changed = response.elements || (response.element ? [response.element] : []);
    const deleted = new Set(response.deletedElementIds || []);
    update((current) => {
      const details = { ...current.details };
      const elements = current.elements.filter((item) => !deleted.has(item.id));
      for (const id of deleted) delete details[id];
      for (const element of changed) {
        details[element.id] = element;
        const index = elements.findIndex((item) => item.id === element.id);
        if (index >= 0) elements[index] = elementSummary(element);
        else elements.push(elementSummary(element));
      }
      return {
        ...current,
        phase: 'ready',
        error: null,
        workingRevision: response.workingRevision,
        studioPendingReview: true,
        studioWarnings: Array.isArray(response.warnings) ? response.warnings : current.studioWarnings,
        studioWarningCount: Number.isInteger(response.historicalProblemCount)
          ? response.historicalProblemCount
          : current.studioWarningCount,
        details,
        elements,
        selectedElementId: deleted.has(current.selectedElementId) ? '' : current.selectedElementId,
        selectedIds: current.selectedIds.filter((id) => !deleted.has(id)),
        total: Math.max(0, current.total - deleted.size, elements.length),
      };
    });
    store.set({ studioPendingCount: 1 });
    try {
      const [list, graph] = await Promise.all([
        getStudioElements(projectId, { limit: 200, workingRevision: response.workingRevision }),
        getStudioGraph(projectId, { workingRevision: response.workingRevision }),
      ]);
      update((current) => ({
        ...current,
        elements: list.items,
        total: list.total,
        nextCursor: list.nextCursor,
        graph,
      }));
    } catch (error) {
      markRequestFailure(error);
    }
  }, [markRequestFailure, projectId, update]);

  const projectEventsReady = projectEventBaseline.ready && projectEventBaseline.projectId === projectId;
  const projectEvents = projectEventsReady ? (
    <StudioProjectEvents
      key={projectId}
      projectId={projectId}
      initialCursor={projectEventBaseline.cursor}
      onEvent={handleProjectEvent}
    />
  ) : null;

  if (session.phase === 'loading' || session.phase === 'idle') {
    return <>
      {projectEvents}
      <StudioLoading />
    </>;
  }
  if (session.phase === 'error' && !session.schema) {
    return <>
      {projectEvents}
      <StudioFailure failure={session.error} onRetry={reloadStudio} />
    </>;
  }
  if (session.phase === 'conflict' && !session.schema) {
    return <>
      {projectEvents}
      <StudioConflict failure={session.error} onReload={reloadStudio} />
    </>;
  }

  return (
    <>
      {projectEvents}
      <div className="studio-root">
        {externalRefreshNotice.pending && (
          <StudioExternalRefreshBanner message={externalRefreshNotice.message}
            onReload={discardLocalDraftsAndLoadStudio} />
        )}
        {(session.phase === 'conflict' || session.phase === 'error') && (
          <StudioBanner failure={session.error} conflict={session.phase === 'conflict'} onRetry={reloadStudio} />
        )}
        {session.studioWarnings?.length > 0 && (
          <StudioWarningBanner warnings={session.studioWarnings} count={session.studioWarningCount}
            onDismiss={() => update({ studioWarnings: [], studioWarningCount: 0 })} />
        )}
        <StudioAccessBanner access={session.schema?.access} />
        {session.studioPendingReview && (
          <StudioPendingReviewBanner state={reviewSubmitState} onSubmit={submitPendingReview}
            blocked={entryDraftDirty} />
        )}
        <StudioToolbar session={session} update={update} navigate={navigate} onRefresh={reloadStudio} />
        {session.elements.length === 0 ? (
          <StudioEmpty schema={session.schema} onCreate={setCreateKind} onRefresh={reloadStudio} />
        ) : session.view === 'graph' ? (
          <StudioGraph projectId={projectId} session={session} update={update} onOpen={openDetail} focus={focus} />
        ) : session.view === 'table' ? (
          <StudioTable session={session} update={update} onOpen={openDetail} onLoadMore={loadMore}
            onCreate={setCreateKind} onDelete={setDeleteIds} onMutation={applyMutation} projectId={projectId} focus={focus} />
        ) : (
          <StudioEntry session={session} update={update} onBack={() => navigate(session.returnView || 'graph')}
            onOpenRelation={(elementId) => openDetail(elementId, session.returnView)}
            onDelete={(elementId) => setDeleteIds([elementId])}
            onMutation={applyMutation} projectId={projectId} />
        )}
        {createKind && (
          <StudioCreateDialog kind={createKind} session={session} projectId={projectId}
            materials={materials}
            onClose={() => setCreateKind('')}
            onCreated={async (response) => {
              await applyMutation(response);
              setCreateKind('');
              openDetail(response.element.id, 'table');
            }} />
        )}
        {deleteIds.length > 0 && (
          <StudioDeleteDialog projectId={projectId} session={session} elementIds={deleteIds}
            onClose={() => setDeleteIds([])}
            onMutation={applyMutation}
            onCompleted={() => {
              setDeleteIds([]);
            navigate('table', '');
          }} />
        )}
      </div>
    </>
  );
}

function StudioProjectEvents({ projectId, initialCursor, onEvent }) {
  useProjectEvents(projectId, onEvent, {
    enabled: Boolean(projectId),
    initialCursor,
  });
  return null;
}

function initialStudioSession(projectId) {
  const cached = readStudioSession(projectId);
  if (typeof window === 'undefined') return cached;
  return restoreStudioHistory(
    cached,
    window.history?.state?.studio?.projectId === projectId
      ? window.history.state.studio
      : studioHistoryFromLocation(window.location),
  );
}

function writeBrowserHistory(session, mode, projectId) {
  if (typeof window === 'undefined' || !window.history || !window.location) return;
  const state = { ...(window.history.state || {}), studio: studioHistorySnapshot(session, projectId) };
  const url = studioHistoryUrl(window.location, session);
  if (mode === 'push') window.history.pushState(state, '', url);
  else window.history.replaceState(state, '', url);
}

function StudioToolbar({ session, update, navigate, onRefresh }) {
  const inEntry = session.view === 'entry';
  const searchDraft = studioSearchDraft(session);
  const submitSearch = () => update(submitStudioSearch);
  const clearSearch = () => update(clearStudioSearch);
  return (
    <div className="studio-toolbar">
      <div className="seg" role="tablist" aria-label="Studio 视图">
        <button role="tab" aria-selected={session.view === 'graph'} className={session.view === 'graph' ? 'on' : ''}
          disabled={inEntry} title={inEntry ? '先从条目返回，修改会自动保存' : undefined}
          onClick={() => navigate('graph')}><Network size={13} />图</button>
        <button role="tab" aria-selected={session.view === 'table'} className={session.view === 'table' ? 'on' : ''}
          disabled={inEntry} title={inEntry ? '先从条目返回，修改会自动保存' : undefined}
          onClick={() => navigate('table')}><Table2 size={13} />表</button>
        <button role="tab" aria-selected={inEntry} className={inEntry ? 'on' : ''} disabled={!session.selectedElementId}
          onClick={() => session.selectedElementId && navigate('entry')}><Boxes size={13} />条目</button>
      </div>
      {!inEntry && <>
        <div className="studio-search" role="search">
          <input value={searchDraft} aria-label="搜索知识条目" placeholder="搜索名称、Key 或说明"
            onChange={(event) => update((current) => stageStudioSearch(current, event.target.value), '')}
            onKeyDown={(event) => {
              if (!shouldSubmitStudioSearch(event)) return;
              event.preventDefault();
              submitSearch();
            }} />
          <button type="button" title="搜索" aria-label="搜索" onClick={submitSearch}><Search size={13} /></button>
          {(searchDraft || session.query) && (
            <button type="button" title="清除搜索" aria-label="清除搜索" onClick={clearSearch}><X size={13} /></button>
          )}
        </div>
        <StudioKindTabs kinds={session.schema?.kinds} elements={session.elements} value={session.kind}
          onChange={(kind) => update((current) => {
            const visibleNodes = (current.graph?.nodes || []).filter((node) => !kind || node.kind === kind);
            return {
              ...current,
              kind,
              selectedIds: [],
              selectedElementId: studioVisibleElementId(current.selectedElementId, visibleNodes),
            };
          })} />
      </>}
      <span className="spacer" />
      <span className="studio-count">{session.elements.length}/{session.total || session.elements.length} 项</span>
      <button className="btn btn-ghost studio-refresh" title="刷新 Studio" aria-label="刷新 Studio"
        onClick={onRefresh} disabled={session.phase === 'refreshing'}>
        <RefreshCw size={14} className={session.phase === 'refreshing' ? 'spin' : ''} />
      </button>
    </div>
  );
}

export function StudioKindTabs({ kinds = [], elements = [], value = '', onChange = () => {} }) {
  const counts = elements.reduce((result, element) => {
    result[element.kind] = (result[element.kind] || 0) + 1;
    return result;
  }, {});
  const tabs = [{ kind: '', label: '全部' }, ...kinds];
  return (
    <div className="seg studio-kind-tabs" role="tablist" aria-label="按知识类型筛选">
      {tabs.map((kind) => {
        const KindIcon = KIND_ICON[kind.kind];
        const count = kind.kind ? counts[kind.kind] || 0 : elements.length;
        const selected = value === kind.kind;
        return (
          <button type="button" role="tab" key={kind.kind || 'all'} className={selected ? 'on' : ''}
            aria-selected={selected} onClick={() => onChange(kind.kind)}>
            {KindIcon && <KindIcon size={13} />}
            <span>{kind.label}</span><small>{count}</small>
          </button>
        );
      })}
    </div>
  );
}

function StudioTable({
  session,
  update,
  onOpen,
  onLoadMore,
  onCreate,
  onDelete,
  onMutation,
  projectId,
  focus,
}) {
  const scrollRef = useRef(null);
  const [bulkDescription, setBulkDescription] = useState('');
  const [bulkState, setBulkState] = useState({ phase: 'idle', message: '' });
  const query = session.query.trim();
  const groups = (session.schema?.kinds || []).map((kind) => ({
    ...kind,
    items: session.elements.filter((element) => (
      element.kind === kind.kind
      && (!session.kind || element.kind === session.kind)
      && studioTextMatchesQuery(`${element.key} ${element.title} ${element.description}`, query)
    )),
  })).filter((group) => !session.kind || group.kind === session.kind);

  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTop = session.tableScrollTop || 0;
  }, []);

  useEffect(() => {
    if (!focus?.elementId || focus.view !== 'table') return;
    scrollRef.current?.querySelector(`[data-element-id="${CSS.escape(focus.elementId)}"]`)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [focus]);

  const visible = groups.reduce((sum, group) => sum + group.items.length, 0);
  if (!visible) return <FilteredEmpty />;
  const selected = new Set(session.selectedIds || []);
  const selectedItems = session.elements.filter((element) => selected.has(element.id));
  const selectedKind = selectedItems[0]?.kind || '';
  const mixedKinds = new Set(selectedItems.map((item) => item.kind)).size > 1;
  const toggleSelected = (element) => {
    const next = new Set(session.selectedIds || []);
    if (next.has(element.id)) next.delete(element.id);
    else next.add(element.id);
    update({ selectedIds: [...next] });
    setBulkState({ phase: 'idle', message: '' });
  };
  const applyBulk = async () => {
    const value = bulkDescription.trim();
    if (!selectedItems.length || mixedKinds || !value || bulkState.phase === 'saving') return;
    setBulkState({ phase: 'saving', message: '正在保存为一组工作草稿' });
    try {
      const response = await batchStudioElements(projectId, {
        expectedRevision: session.workingRevision,
        clientMutationId: mutationId('studio-bulk'),
        editSessionId: mutationId('studio-bulk-session'),
        patches: selectedItems.map((element) => ({
          elementId: element.id,
          changes: { description: value },
        })),
      });
      await onMutation(response);
      update({ selectedIds: [] });
      setBulkDescription('');
      setBulkState({ phase: 'saved', message: '修改已保存，请执行确定性检查后应用' });
    } catch (error) {
      const failure = studioError(error);
      if (isStudioNoChangesFailure(failure)) {
        update({ selectedIds: [] });
        setBulkDescription('');
        setBulkState({ phase: 'saved', message: '内容已同步，无需重复保存' });
        return;
      }
      setBulkState({
        phase: failure.conflict ? 'conflict' : 'error',
        message: failure.message,
      });
    }
  };
  return (
    <>
      {selectedItems.length > 0 && (
        <div className="studio-bulk" role="region" aria-label="批量编辑">
          <b>已选 {selectedItems.length} 条{mixedKinds ? '知识' : KIND_META[selectedKind]?.label || '知识'}条目</b>
          {!mixedKinds && <>
            <label><span>统一说明</span><input className="input" value={bulkDescription}
              placeholder="输入这批条目共同的业务说明"
              onChange={(event) => setBulkDescription(event.target.value)} /></label>
            <button className="btn btn-p" disabled={!bulkDescription.trim() || bulkState.phase === 'saving'}
              onClick={applyBulk}>
              <Save size={13} />{bulkState.phase === 'saving' ? '保存中…' : '保存为待检查修改'}
            </button>
          </>}
          <button className="btn btn-danger" onClick={() => onDelete(selectedItems.map((item) => item.id))}>
            <Trash2 size={13} />删除所选
          </button>
          <button className="btn btn-ghost" aria-label="取消批量选择" title="取消选择"
            onClick={() => update({ selectedIds: [] })}><X size={14} /></button>
          {bulkState.message && <span className={`studio-save-note ${bulkState.phase}`}>{bulkState.message}</span>}
        </div>
      )}
      <div className="studio-table-scroll" ref={scrollRef}
        onScroll={(event) => update({ tableScrollTop: event.currentTarget.scrollTop })}>
        {groups.map((group) => (
          <section className="studio-table-group" key={group.kind} aria-labelledby={`studio-${group.kind}`}>
            <div className="studio-group-head">
              <KindLabel kind={group.kind} label={group.label} />
              <span>{group.items.length} 项</span>
              {group.capabilities?.create && (
                <button className="btn btn-sm" onClick={() => onCreate(group.kind)}>
                  <Plus size={13} />新建{group.label}
                </button>
              )}
            </div>
            {group.items.length ? (
              <table className="grid">
                <thead><tr><th aria-label="批量选择" /><th>名称</th><th>Key</th><th>说明</th><th>关系</th></tr></thead>
                <tbody>{group.items.map((element) => (
                  <tr key={element.id} data-element-id={element.id} tabIndex={0}
                    className={element.id === session.selectedElementId ? 'selected' : ''}
                    aria-label={`查看${group.label}${element.title}`}
                    onClick={() => onOpen(element.id, 'table')}
                    onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') onOpen(element.id, 'table'); }}>
                    <td onClick={(event) => event.stopPropagation()}>
                      {(group.capabilities?.batch || group.capabilities?.delete) && (
                        <input type="checkbox" aria-label={`选择${element.title}`}
                          checked={selected.has(element.id)}
                          onChange={() => toggleSelected(element)} />
                      )}
                    </td>
                    <td><b>{element.title}</b></td>
                    <td><code className="k">{element.key}</code></td>
                    <td className="muted">{element.description || '未填写说明'}</td>
                    <td>{element.relationCount}</td>
                  </tr>
                ))}</tbody>
              </table>
            ) : <div className="studio-group-empty">没有匹配项</div>}
          </section>
        ))}
        {session.nextCursor && (
          <div className="studio-load-more">
            <button className="btn" onClick={onLoadMore} disabled={session.loadingMore}>
              {session.loadingMore ? '正在加载…' : `继续加载其余 ${Math.max(0, session.total - session.elements.length)} 项`}
            </button>
          </div>
        )}
      </div>
    </>
  );
}

function StudioEntry({ session, update, onBack, onOpenRelation, onDelete, onMutation, projectId }) {
  const detail = session.details[session.selectedElementId];
  const kindSchema = session.schema?.kinds.find((kind) => kind.kind === detail?.kind);
  const canUpdate = Boolean(kindSchema?.capabilities?.update);
  const remembered = session.entryDrafts?.[session.selectedElementId]?.draft;
  const [draft, setDraft] = useState(() => remembered || editableDraft(detail, kindSchema));
  const [baseline, setBaseline] = useState(() => editableDraft(detail, kindSchema));
  const [saveState, setSaveState] = useState({ phase: 'idle', message: '', failure: null, fieldErrors: {} });
  const savingRef = useRef(false);
  const savingPromiseRef = useRef(null);
  const pausedRef = useRef(false);
  const draftRef = useRef(draft);
  const revisionRef = useRef(session.workingRevision);

  useEffect(() => { revisionRef.current = session.workingRevision; }, [session.workingRevision]);
  useEffect(() => {
    if (!detail) return;
    const nextBaseline = editableDraft(detail, kindSchema);
    const next = session.entryDrafts?.[detail.id]?.draft || nextBaseline;
    const dirty = !studioDraftEqual(next, nextBaseline);
    draftRef.current = next;
    setDraft(next);
    setBaseline(nextBaseline);
    pausedRef.current = dirty && !canUpdate;
    setSaveState(dirty && !canUpdate
      ? { phase: 'paused', message: '本地修改已保留，当前项目处于只读状态', failure: null, fieldErrors: {} }
      : { phase: dirty ? 'dirty' : 'idle', message: '', failure: null, fieldErrors: {} });
  }, [canUpdate, detail?.id]);

  const forgetDraft = useCallback((elementId) => {
    update((current) => {
      if (!Object.prototype.hasOwnProperty.call(current.entryDrafts || {}, elementId)) return current;
      const entryDrafts = { ...current.entryDrafts };
      delete entryDrafts[elementId];
      return { ...current, entryDrafts };
    });
  }, [update]);

  const remember = (next, field = '') => {
    const synced = !savingRef.current && studioDraftEqual(next, baseline);
    pausedRef.current = false;
    draftRef.current = next;
    setDraft(next);
    setSaveState((current) => studioSaveStateAfterDraftChange(current, {
      baseline,
      draft: next,
      field,
      saving: savingRef.current,
    }));
    update((current) => {
      const previousDraft = current.entryDrafts?.[session.selectedElementId];
      return {
        ...current,
        entryDrafts: synced
          ? Object.fromEntries(Object.entries(current.entryDrafts || {}).filter(([id]) => id !== session.selectedElementId))
          : {
            ...current.entryDrafts,
            [session.selectedElementId]: {
              ...(previousDraft || {}),
              draft: next,
              baseRevision: previousDraft?.baseRevision || revisionRef.current,
              updatedAt: Date.now(),
            },
          },
      };
    });
  };

  const saveDraft = useCallback(() => {
    if (savingPromiseRef.current) return savingPromiseRef.current;
    if (!detail || !kindSchema) return Promise.resolve(false);
    if (!canUpdate) {
      pausedRef.current = true;
      setSaveState({
        phase: 'paused',
        message: '本地修改已保留，当前项目处于只读状态',
        failure: null,
        fieldErrors: {},
      });
      return Promise.resolve(false);
    }
    if (pausedRef.current) return Promise.resolve(false);
    const snapshot = draftRef.current;
    const incompleteField = studioIncompleteDraftField(snapshot, kindSchema);
    if (incompleteField) {
      setSaveState({
        phase: 'error',
        message: '请先补完整必填内容',
        failure: null,
        fieldErrors: { [`changes.${incompleteField}`]: '必填内容不能为空' },
      });
      return Promise.resolve(false);
    }
    const changes = studioChangedFields(baseline, snapshot);
    if (!Object.keys(changes).length) {
      pausedRef.current = false;
      forgetDraft(detail.id);
      setSaveState(studioSyncedSaveState());
      return Promise.resolve(true);
    }
    savingRef.current = true;
    setSaveState({ phase: 'saving', message: '正在保存工作草稿', failure: null, fieldErrors: {} });
    const promise = (async () => {
      try {
        const response = await updateStudioElement(projectId, detail.id, {
          expectedRevision: revisionRef.current,
          clientMutationId: mutationId('studio-save'),
          editSessionId: `studio-entry-${detail.id.replace(/[^a-zA-Z0-9_.:-]/g, '_')}`,
          changes,
        });
        revisionRef.current = response.workingRevision;
        await onMutation(response);
        const saved = editableDraft(response.element, kindSchema);
        setBaseline(saved);
        const unchangedDuringSave = studioDraftEqual(draftRef.current, snapshot);
        if (unchangedDuringSave) {
          draftRef.current = saved;
          setDraft(saved);
          update((current) => {
            const entryDrafts = { ...current.entryDrafts };
            delete entryDrafts[detail.id];
            return { ...current, entryDrafts };
          });
        } else {
          update((current) => {
            const entry = current.entryDrafts?.[detail.id];
            if (!entry) return current;
            const { newerRevision: _newerRevision, refreshPending: _refreshPending, ...rest } = entry;
            return {
              ...current,
              entryDrafts: {
                ...current.entryDrafts,
                [detail.id]: { ...rest, baseRevision: response.workingRevision },
              },
            };
          });
        }
        setSaveState({
          phase: unchangedDuringSave ? 'saved' : 'dirty',
          message: unchangedDuringSave ? (response.display?.text || '工作草稿已保存') : '有新的修改等待保存',
          savedAt: unchangedDuringSave ? new Date().toLocaleTimeString('zh-CN', { hour12: false }) : undefined,
          failure: null,
          fieldErrors: {},
        });
        return true;
      } catch (error) {
        const failure = studioError(error);
        if (isStudioNoChangesFailure(failure)) {
          let synced = snapshot;
          try {
            const latest = await getStudioElement(projectId, detail.id, revisionRef.current);
            revisionRef.current = latest.workingRevision;
            synced = editableDraft(latest.element, kindSchema);
            update((current) => ({
              ...current,
              workingRevision: latest.workingRevision,
              details: { ...current.details, [detail.id]: latest.element },
              elements: current.elements.map((element) => (
                element.id === detail.id ? elementSummary(latest.element) : element
              )),
            }));
          } catch {
            // NO_CHANGES proves the working package is already equivalent to this submission.
          }
          draftRef.current = synced;
          setDraft(synced);
          setBaseline(synced);
          pausedRef.current = false;
          forgetDraft(detail.id);
          setSaveState(studioSyncedSaveState());
          return true;
        }
        if (failure.code === 'STUDIO_WRITE_LOCKED') {
          try {
            const schema = await getStudioSchema(projectId);
            update({ schema });
          } catch {
            // Keep the original lock failure visible if access refresh is unavailable.
          }
        }
        if (failure.actualRevision) {
          update((current) => ({
            ...current,
            entryDrafts: markStudioDraftsForRefresh(current.entryDrafts, failure.actualRevision),
          }));
        }
        pausedRef.current = true;
        setSaveState({
          phase: failure.conflict ? 'conflict' : 'error',
          message: failure.message,
          failure,
          fieldErrors: failure.fieldErrors,
        });
        return false;
      } finally {
        savingRef.current = false;
        savingPromiseRef.current = null;
      }
    })();
    savingPromiseRef.current = promise;
    return promise;
  }, [baseline, canUpdate, detail, forgetDraft, kindSchema, onMutation, projectId, update]);

  useEffect(() => {
    if (!detail) return undefined;
    if (studioDraftEqual(draft, baseline)) {
      if (!savingRef.current) {
        pausedRef.current = false;
        setSaveState((current) => ['idle', 'saved'].includes(current.phase)
          ? current
          : studioSyncedSaveState());
      }
      return undefined;
    }
    if (pausedRef.current || savingRef.current) return undefined;
    if (studioIncompleteDraftField(draft, kindSchema)) {
      setSaveState((current) => ({ ...current, phase: 'dirty', message: '请先补完整必填内容' }));
      return undefined;
    }
    setSaveState((current) => current.phase === 'dirty'
      ? current
      : { phase: 'dirty', message: '有未保存修改', failure: null, fieldErrors: {} });
    const timer = setTimeout(() => { void saveDraft(); }, 600);
    return () => clearTimeout(timer);
  }, [baseline, detail, draft, kindSchema, saveDraft]);

  const reloadAndRetry = async () => {
    const actualRevision = saveState.failure?.actualRevision;
    if (!detail || !actualRevision) return;
    setSaveState({ phase: 'saving', message: '正在载入最新内容', failure: null, fieldErrors: {} });
    try {
      const latest = await getStudioElement(projectId, detail.id, actualRevision);
      const latestBaseline = editableDraft(latest.element, kindSchema);
      revisionRef.current = latest.workingRevision;
      setBaseline(latestBaseline);
      await onMutation({ ...latest, elements: [latest.element] });
      update((current) => {
        const entry = current.entryDrafts?.[detail.id];
        if (!entry) return current;
        const { newerRevision: _newerRevision, refreshPending: _refreshPending, ...rest } = entry;
        return {
          ...current,
          entryDrafts: {
            ...current.entryDrafts,
            [detail.id]: { ...rest, baseRevision: latest.workingRevision },
          },
        };
      });
      pausedRef.current = false;
      setSaveState({ phase: 'dirty', message: '已载入新版，正在保留你的修改并重试', failure: null, fieldErrors: {} });
    } catch (error) {
      const failure = studioError(error);
      setSaveState({ phase: 'error', message: failure.message, failure, fieldErrors: failure.fieldErrors });
    }
  };

  const leave = async () => {
    if (!studioDraftEqual(draftRef.current, baseline) && !pausedRef.current) {
      const saved = await saveDraft();
      if (!saved) return;
    }
    onBack();
  };
  const openRelated = async (elementId) => {
    if (!studioDraftEqual(draftRef.current, baseline) && !pausedRef.current) {
      const saved = await saveDraft();
      if (!saved) return;
    }
    onOpenRelation(elementId);
  };

  if (session.detailLoading && !detail) return <StudioLoading label="正在读取条目" />;
  if (!detail) return <StudioFailure failure={{ message: '这个条目已不在当前工作修订中' }} onRetry={onBack} retryLabel="返回列表" />;
  return (
    <div className="studio-entry">
      <div className="studio-entry-head">
        <button className="btn btn-ghost" onClick={leave}><ArrowLeft size={14} />返回{session.returnView === 'table' ? '表视图' : '图视图'}</button>
        <span className="spacer" />
        <StudioSaveState state={saveState} onRetry={() => {
          pausedRef.current = false;
          setSaveState({ phase: 'dirty', message: '正在重试', failure: null, fieldErrors: {} });
          void saveDraft();
        }} onReload={reloadAndRetry} onPause={() => {
          pausedRef.current = true;
          setSaveState((current) => ({ ...current, phase: 'paused', message: '本地修改已保留，稍后再处理' }));
          }} />
          {kindSchema?.capabilities?.delete && (
            <button className="btn btn-danger" onClick={() => onDelete(detail.id)}>
              <Trash2 size={13} />{studioDeleteCopy(detail.kind, detail.title).button}
            </button>
        )}
        <code className="k">{detail.sourcePath}</code>
      </div>
      <div className="studio-entry-title">
        <KindLabel kind={detail.kind} label={KIND_META[detail.kind]?.label} />
        <h2>{draft.title || detail.title}</h2>
        <code>{detail.key}</code>
      </div>
      {(draft.description || detail.description) && <p className="studio-entry-description">{draft.description || detail.description}</p>}
      <div className="studio-fields">
        {(kindSchema?.fields || []).map((field) => (
          <section key={field.key}>
            <h3>{field.label}</h3>
            {!studioFieldEditable(kindSchema, field) ? (
              field.type === 'evidence_list'
                ? <EvidenceLinks references={detail.values[field.key]} />
                : <FieldValue value={studioDraftFieldValue(draft, detail.values, field.key)} />
            ) : (
              <StudioFieldEditor field={field} value={draft[field.key]}
                error={fieldErrorFor(saveState.fieldErrors, field.key)}
                onChange={(value) => remember({ ...draftRef.current, [field.key]: value }, field.key)} />
            )}
          </section>
        ))}
      </div>
      <section className={`studio-relations${kindSchema?.capabilities?.update ? ' studio-relations-edit' : ''}`}>
        <h3>关系 <span>{(draft.relations || []).length}</span></h3>
        {kindSchema?.capabilities?.update && (
          <RelationEditor key={detail.id} sourceId={detail.id} schema={kindSchema} value={draft.relations || []}
            elements={studioRelationElements(session.elements, session.graph)}
            onChange={(relations) => remember({ ...draftRef.current, relations }, 'relations')} />
        )}
        {detail.relations.length > 0 && (
          <div className="studio-relation-links">
            {detail.relations.map((relation) => (
              <button key={relation.id} disabled={!relation.targetExists}
                onClick={() => relation.targetExists && void openRelated(relation.targetId)}>
                <span>{relation.label}</span>
                <b>{session.elements.find((element) => element.id === relation.targetId)?.title || relation.targetId}</b>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function StudioSaveState({ state, onRetry, onReload, onPause }) {
  const label = {
    idle: '当前内容已同步',
    dirty: '有未保存修改',
    saving: '保存中',
    saved: `工作草稿已保存${state.savedAt ? ` · ${state.savedAt}` : ''}`,
    conflict: '知识包已有新修改',
    error: '保存失败',
    paused: '本地修改已保留',
  }[state.phase] || '';
  return (
    <div className={`studio-save-state ${state.phase}`} aria-live="polite">
      {['idle', 'saved'].includes(state.phase) ? <Check size={13} /> : state.phase === 'saving' ? <RefreshCw className="spin" size={13} /> : <Save size={13} />}
      <span>{label}</span>
      {state.message && ['error', 'conflict', 'paused'].includes(state.phase) && <small>{state.message}</small>}
      {state.phase === 'error' && <button className="btn btn-sm" onClick={onRetry}>重试</button>}
      {state.phase === 'conflict' && <>
        <button className="btn btn-sm btn-p" onClick={onReload}>载入新版并重试</button>
        <button className="btn btn-sm" onClick={onPause}>稍后处理</button>
      </>}
    </div>
  );
}

function StudioFieldEditor({ field, value, onChange, error }) {
  if (field.type === 'text') {
    return <><input className="input studio-field-input" value={value || ''} required={field.required}
      onChange={(event) => onChange(event.target.value)} />{error && <FieldError text={error} />}</>;
  }
  if (field.type === 'long_text') {
    return <><textarea className="input studio-field-input" rows={4} value={value || ''} required={field.required}
      onChange={(event) => onChange(event.target.value)} />{error && <FieldError text={error} />}</>;
  }
  if (['text_list', 'ordered_text_list'].includes(field.type)) {
    const items = Array.isArray(value) ? value : [];
    return <><div className="studio-list-editor">
      {items.map((item, index) => <div key={index}><input className="input" value={item}
        onChange={(event) => onChange(items.map((current, itemIndex) => itemIndex === index ? event.target.value : current))} />
        <button className="btn btn-ghost" aria-label={`删除${field.label}第${index + 1}项`}
          onClick={() => onChange(items.filter((_item, itemIndex) => itemIndex !== index))}><X size={13} /></button></div>)}
      <button className="btn btn-sm" onClick={() => onChange([...items, ''])}><Plus size={13} />添加一项</button>
    </div>{error && <FieldError text={error} />}</>;
  }
  const columns = structuredColumns(field);
  if (columns) {
    const rows = Array.isArray(value) ? value : [];
    return <><div className="studio-structured-editor"><table className="grid"><thead><tr>
      {columns.map((column) => <th key={column.key}>{column.label}</th>)}<th aria-label="操作" />
    </tr></thead><tbody>{rows.map((row, index) => <tr key={index}>
      {columns.map((column) => <td key={column.key}>{column.type === 'boolean'
        ? <input type="checkbox" checked={Boolean(row?.[column.key])}
          onChange={(event) => onChange(updateStructuredCell(rows, index, column.key, event.target.checked))} />
        : <input className="input" value={row?.[column.key] || ''}
          onChange={(event) => onChange(updateStructuredCell(rows, index, column.key, event.target.value))} />}</td>)}
      <td><button className="btn btn-ghost" aria-label={`删除${field.label}第${index + 1}行`}
        onClick={() => onChange(rows.filter((_row, rowIndex) => rowIndex !== index))}><X size={13} /></button></td>
    </tr>)}</tbody></table>
      <button className="btn btn-sm" onClick={() => onChange([...rows, Object.fromEntries(columns.map((column) => [column.key, column.type === 'boolean' ? false : '']))])}>
        <Plus size={13} />添加一行
      </button>
    </div>{error && <FieldError text={error} />}</>;
  }
  return <FieldValue value={value} />;
}

function RelationEditor({ sourceId, schema, value, elements, onChange }) {
  const [kind, setKind] = useState(schema?.legalRelations?.[0]?.kind || '');
  const [targetId, setTargetId] = useState('');
  const legal = schema?.legalRelations?.find((item) => item.kind === kind);
  const existing = new Set(value.map((relation) => relation.targetId));
  const targets = elements.filter((element) => (
    element.id !== sourceId
    && legal?.targetKinds?.includes(element.kind)
    && !existing.has(element.id)
  ));
  const add = () => {
    if (!kind || !targetId) return;
    onChange([...value, { kind, targetId, label: relationLabel(kind) }]);
    setTargetId('');
  };
  return (
    <div className="studio-relation-editor">
      {value.length > 0 && <div className="studio-relation-chips">{value.map((relation) => (
        <span key={`${relation.kind}:${relation.targetId}`}>
          <small>{relationLabel(relation.kind)}</small>
          {elements.find((element) => element.id === relation.targetId)?.title || relation.targetId}
          <button aria-label={`移除关系${relation.targetId}`}
            onClick={() => onChange(value.filter((item) => item !== relation))}><X size={12} /></button>
        </span>
      ))}</div>}
      {(schema?.legalRelations || []).length > 0 && <div className="studio-relation-add">
        <select className="input" aria-label="关系类型" value={kind} onChange={(event) => { setKind(event.target.value); setTargetId(''); }}>
          {(schema.legalRelations || []).map((item) => <option value={item.kind} key={item.kind}>{relationLabel(item.kind)}</option>)}
        </select>
        <select className="input" aria-label="关系目标" value={targetId} onChange={(event) => setTargetId(event.target.value)}>
          <option value="">选择知识条目</option>
          {targets.map((element) => <option value={element.id} key={element.id}>{element.title} · {element.key}</option>)}
        </select>
        <button className="btn btn-sm" disabled={!targetId} onClick={add}><Plus size={13} />添加关系</button>
      </div>}
    </div>
  );
}

function StudioDeleteDialog({ projectId, session, elementIds, onClose, onMutation, onCompleted }) {
  const [state, setState] = useState({ phase: 'loading', preview: null, message: '' });
  const selectedElement = elementIds.length === 1
    ? session.elements.find((element) => element.id === elementIds[0])
    : null;
  const deleteCopy = studioDeleteCopy(selectedElement?.kind, selectedElement?.title, elementIds.length);

  useEffect(() => {
    let active = true;
    setState({ phase: 'loading', preview: null, message: '' });
    void previewStudioDeletions(projectId, {
      expectedRevision: session.workingRevision,
      elementIds,
    }).then((preview) => {
      if (active) setState({ phase: 'ready', preview, message: '' });
    }).catch((error) => {
      if (active) setState({ phase: 'error', preview: null, message: studioError(error).message });
    });
    return () => { active = false; };
  }, [elementIds, projectId, session.workingRevision]);

  const apply = async () => {
    if (!state.preview || state.phase === 'saving') return;
    setState((current) => ({
      ...current,
      phase: 'saving',
      message: '正在删除并整理关系',
    }));
    try {
      const response = await applyStudioDeletions(projectId, {
        expectedRevision: state.preview.workingRevision,
        clientMutationId: mutationId('studio-delete'),
        editSessionId: mutationId('studio-delete-session'),
        elementIds,
        previewToken: state.preview.previewToken,
        cleanupReferences: true,
      });
      await onMutation(response);
      onCompleted();
    } catch (error) {
      const failure = studioError(error);
      setState((current) => ({
        ...current,
        phase: failure.conflict ? 'conflict' : 'error',
        message: failure.message,
      }));
    }
  };

  return (
    <div className="studio-dialog-backdrop" role="presentation">
      <section className="studio-dialog" role="dialog" aria-modal="true" aria-labelledby="studio-delete-title">
        <header>
          <div><h2 id="studio-delete-title">{deleteCopy.heading}</h2></div>
          <button type="button" className="btn btn-ghost" aria-label="关闭" onClick={onClose}><X size={15} /></button>
        </header>
        {state.phase === 'loading' && <div className="studio-dialog-message">正在核对关系影响…</div>}
        {state.preview && <>
          <p>{state.preview.display.text}。{deleteCopy.clarification}删除会先保存为工作草稿，确定性检查通过后才应用。</p>
          {state.preview.references.length > 0 && <div className="studio-delete-impact">
            <b>将同时清理的关系</b>
            <ul>{state.preview.references.map((reference) => (
              <li key={`${reference.sourceId}:${reference.field}:${reference.targetId}`}>
                《{reference.sourceTitle}》的“{reference.field}”
              </li>
            ))}</ul>
          </div>}
          {state.preview.textReferences?.length > 0 && <div className="studio-delete-impact error">
            <b>必须先修改的正文引用</b>
            <ul>{state.preview.textReferences.map((reference) => (
              <li key={`${reference.sourceId}:${reference.field}:${reference.targetId}`}>
                《{reference.sourceTitle}》的“{reference.field}”：{reference.excerpt}
              </li>
            ))}</ul>
          </div>}
        </>}
        {state.message && <div className={`studio-dialog-message ${state.phase}`} role="alert">{state.message}</div>}
        <footer>
          <button className="btn" onClick={onClose} disabled={state.phase === 'saving'}>取消</button>
          <button className="btn btn-danger" disabled={!state.preview || state.preview.blocked || state.phase === 'saving'} onClick={apply}>
            <Trash2 size={13} />{state.phase === 'saving'
              ? '正在删除…'
              : state.preview?.cleanupRequired
                ? `${deleteCopy.button}并清理关系`
                : `确认${deleteCopy.button}`}
          </button>
        </footer>
      </section>
    </div>
  );
}

function StudioCreateDialog({ kind, session, projectId, materials, onClose, onCreated }) {
  const meta = KIND_META[kind] || { label: '知识条目' };
  const kindSchema = session.schema?.kinds?.find((item) => item.kind === kind);
  const evidenceRequired = Boolean(kindSchema?.fields?.some((field) => (
    field.type === 'evidence_list' && field.required
  )));
  const [key, setKey] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [coreContent, setCoreContent] = useState('');
  const [materialId, setMaterialId] = useState(materials[0]?.id || '');
  const [anchor, setAnchor] = useState('');
  const [state, setState] = useState({ phase: 'idle', message: '', issues: [] });
  const selectedMaterial = materials.find((material) => material.id === materialId);
  const evidenceReady = !evidenceRequired || Boolean(selectedMaterial && anchor.trim());
  const coreContentRequired = ['operation', 'rule', 'metric'].includes(kind);
  const creationReady = Boolean(
    key.trim()
    && title.trim()
    && description.trim()
    && (!coreContentRequired || coreContent.trim())
    && evidenceReady,
  );
  const submit = async (event) => {
    event.preventDefault();
    if (!creationReady || state.phase === 'saving') return;
    setState({ phase: 'saving', message: '正在建立工作草稿', issues: [] });
    try {
      const evidence = evidenceRequired
        ? [`material:${selectedMaterial.sourceKey || selectedMaterial.id}#${anchor.trim()}`]
        : undefined;
      const values = {
        title: title.trim(),
        description: description.trim(),
        ...(kind === 'operation' ? { flow: [coreContent.trim()] } : {}),
        ...(kind === 'rule' ? { content: coreContent.trim() } : {}),
        ...(kind === 'metric' ? { caliber: coreContent.trim(), boundaries: [coreContent.trim()] } : {}),
        ...(evidence ? { evidence } : {}),
      };
      const response = await createStudioElement(projectId, {
        expectedRevision: session.workingRevision,
        clientMutationId: mutationId('studio-create'),
        editSessionId: mutationId('studio-create-session'),
        kind,
        key: key.trim(),
        values,
      });
      await onCreated(response);
    } catch (error) {
      const failure = studioError(error);
      setState({
        phase: failure.conflict ? 'conflict' : 'error',
        message: failure.message,
        issues: visibleStudioIssues(failure.fieldErrors),
      });
    }
  };
  return (
    <div className="studio-dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <form className="studio-dialog" role="dialog" aria-modal="true" aria-labelledby="studio-create-title" onSubmit={submit}>
        <header><div><KindLabel kind={kind} label={meta.label} /><h2 id="studio-create-title">新建{meta.label}</h2></div>
          <button type="button" className="btn btn-ghost" aria-label="关闭" onClick={onClose}><X size={15} /></button></header>
        <label><span>稳定 Key</span><input className="input" value={key} autoFocus
          placeholder="例如 safety_stock" onChange={(event) => setKey(event.target.value)} />
          <small>创建后不能修改；名称和业务内容可以继续编辑。</small></label>
        <label><span>名称</span><input className="input" value={title}
          placeholder={`输入${meta.label}名称`} onChange={(event) => setTitle(event.target.value)} /></label>
        <label><span>{kind === 'object' ? '业务定义' : kind === 'operation' ? '业务目标' : '说明'}</span>
          <textarea className="input" rows={3} value={description}
            placeholder={`说明这条${meta.label}解决什么问题`} onChange={(event) => setDescription(event.target.value)} required />
        </label>
        {coreContentRequired && <label>
          <span>{kind === 'operation' ? '首个流程步骤' : kind === 'rule' ? '规则内容' : '业务口径'}</span>
          <textarea className="input" rows={3} value={coreContent}
            placeholder={kind === 'operation' ? '输入一个可执行的流程步骤' : `输入${meta.label}的核心内容`}
            onChange={(event) => setCoreContent(event.target.value)} required />
        </label>}
        {evidenceRequired && <div className="studio-dialog-evidence">
          <label><span>依据资料</span><select className="input" aria-label="依据资料" value={materialId}
            onChange={(event) => setMaterialId(event.target.value)} required>
            <option value="">选择项目资料</option>
            {materials.map((material) => <option value={material.id} key={material.id}>{material.filename}</option>)}
          </select><small>选择能支持这条{meta.label}的项目资料。</small></label>
          <label><span>原文位置</span><input className="input" aria-label="原文位置" value={anchor}
            placeholder="例如：第三章 2.1 安全库存" onChange={(event) => setAnchor(event.target.value)} required />
          <small>填写章节、标题、表名或能在原文中定位的文字。</small></label>
          {!materials.length && <div className="studio-dialog-message error" role="alert">
            项目还没有可选资料，请先上传资料再新建{meta.label}。
          </div>}
        </div>}
        {state.message && <div className={`studio-dialog-message ${state.phase}`}>{state.message}</div>}
        {state.issues.length > 0 && <ul className="studio-dialog-issues">
          {state.issues.map((issue) => <li key={issue}>{issue}</li>)}
        </ul>}
        <footer><button type="button" className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-p" disabled={!creationReady || state.phase === 'saving'}>
            {state.phase === 'saving' ? '正在建立…' : '建立并继续编辑'}
          </button></footer>
      </form>
    </div>
  );
}

function editableDraft(detail, schema) {
  if (!detail) return {};
  const draft = {};
  for (const field of schema?.fields || []) {
    if (!field.readOnly) draft[field.key] = structuredClone(detail.values?.[field.key] ?? defaultFieldValue(field.type));
  }
  draft.relations = (detail.relations || []).filter((relation) => relation.targetExists).map((relation) => ({
    kind: relation.kind,
    targetId: relation.targetId,
    label: relation.label,
    ...(relation.description ? { description: relation.description } : {}),
  }));
  return draft;
}

function defaultFieldValue(type) {
  return ['text_list', 'ordered_text_list', 'parameter_table', 'output_table', 'condition_table', 'key_value_table', 'field_table'].includes(type) ? [] : '';
}

function mutationId(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${suffix}`.slice(0, 128);
}

function fieldErrorFor(errors, field) {
  const match = Object.entries(errors || {}).find(([path]) => path === `changes.${field}` || path.startsWith(`changes.${field}[`) || path.startsWith(`changes.${field}.`));
  return match?.[1] || '';
}

function visibleStudioIssues(fieldErrors) {
  const issues = Object.values(fieldErrors || {}).flatMap((value) => String(value)
    .split('\n')
    .map((line) => line.replace(/^\s*-\s*/u, '').trim())
    .filter((line) => line && !/知识包规格不完整/u.test(line)));
  return [...new Set(issues)].slice(0, 6);
}

function FieldError({ text }) {
  return <div className="studio-field-error" role="alert">{text}</div>;
}

function structuredColumns(field) {
  if (field.type === 'field_table') return [
    { key: 'key', label: 'Key' }, { key: 'cnName', label: '中文名' }, { key: 'type', label: '类型' },
    { key: 'required', label: '必填', type: 'boolean' }, { key: 'note', label: '说明' },
  ];
  if (field.type === 'parameter_table') return [
    { key: 'param', label: '参数' }, { key: 'cnName', label: '中文名' }, { key: 'type', label: '类型' },
    { key: 'required', label: '必填', type: 'boolean' }, { key: 'note', label: '说明' },
  ];
  if (field.type === 'output_table') return [
    { key: 'field', label: '字段' }, { key: 'cnName', label: '中文名' }, { key: 'type', label: '类型' }, { key: 'note', label: '说明' },
  ];
  if (field.type === 'condition_table') return [{ key: 'condition', label: '条件' }, { key: 'conclusion', label: '结论' }];
  if (field.type === 'key_value_table' && field.key === 'nullPolicy') return [{ key: 'field', label: '字段' }, { key: 'handling', label: '处理' }];
  if (field.type === 'key_value_table') return [{ key: 'key', label: 'Key' }, { key: 'note', label: '说明' }];
  return null;
}

function updateStructuredCell(rows, rowIndex, key, value) {
  return rows.map((row, index) => index === rowIndex ? { ...row, [key]: value } : row);
}

function relationLabel(kind) {
  return ({
    object_relation: '关联对象',
    uses_object: '使用对象',
    uses_rule: '使用规则',
    uses_metric: '使用指标',
    applies_to_object: '适用于对象',
  })[kind] || '关系';
}

function FieldValue({ value }) {
  if (value === undefined || value === null || value === '' || (Array.isArray(value) && value.length === 0)) {
    return <span className="faint">未声明</span>;
  }
  if (typeof value === 'boolean') return <span>{value ? '是' : '否'}</span>;
  if (typeof value !== 'object') return <span className="studio-long-text">{String(value)}</span>;
  if (Array.isArray(value)) {
    if (value.every((item) => typeof item !== 'object')) {
      return <ul>{value.map((item, index) => <li key={`${index}-${item}`}>{String(item)}</li>)}</ul>;
    }
    const columns = [...new Set(value.flatMap((row) => Object.keys(row || {})))];
    return (
      <div className="studio-value-table"><table className="grid"><thead><tr>
        {columns.map((column) => <th key={column}>{humanField(column)}</th>)}
      </tr></thead><tbody>{value.map((row, index) => <tr key={index}>
        {columns.map((column) => <td key={column}>{compactValue(row?.[column])}</td>)}
      </tr>)}</tbody></table></div>
    );
  }
  return <dl>{Object.entries(value).map(([key, item]) => (
    <div key={key}><dt>{humanField(key)}</dt><dd>{compactValue(item)}</dd></div>
  ))}</dl>;
}

function compactValue(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (Array.isArray(value)) return value.map(compactValue).join('、') || '—';
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${humanField(key)}：${compactValue(item)}`).join('；');
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

function elementSummary(element) {
  const { values: _values, relations: _relations, ...summary } = element;
  return summary;
}

function humanField(value) {
  return ({
    key: 'Key', cnName: '中文名', type: '类型', required: '必填', note: '说明', alias: '别名',
    kind: '类型', filename: '文件', anchors: '定位', condition: '条件', conclusion: '结论',
    field: '字段', handling: '处理', param: '参数', defaultValue: '默认值',
  })[value] || value;
}

function KindLabel({ kind, label }) {
  const Icon = KIND_ICON[kind] || Boxes;
  return <span className={`studio-kind-label ${KIND_META[kind]?.className || ''}`}><Icon size={13} />{label || kind}</span>;
}

function StudioLoading({ label = '正在读取知识包 Studio' }) {
  return <div className="studio-state" aria-busy="true"><RefreshCw className="spin" size={22} /><b>{label}</b><span>正在对齐当前工作修订…</span></div>;
}

function StudioFailure({ failure, onRetry, retryLabel = '重试' }) {
  return <div className="studio-state error"><AlertCircle size={24} /><b>Studio 暂时无法显示</b><span>{failure?.message}</span><button className="btn" onClick={onRetry}>{retryLabel}</button></div>;
}

function StudioConflict({ failure, onReload }) {
  return <div className="studio-state conflict"><RefreshCw size={24} /><b>知识包刚刚有了新版本</b><span>{failure?.message}</span><button className="btn btn-p" onClick={onReload}>载入当前版本</button></div>;
}

function StudioBanner({ failure, conflict, onRetry }) {
  return <div className={`studio-banner ${conflict ? 'conflict' : 'error'}`} role="alert">
    <AlertCircle size={14} /><span>{conflict ? '知识包已变化，当前画面保留供核对。' : failure?.message}</span>
    <button className="btn btn-sm" onClick={onRetry}>{conflict ? '载入当前版本' : '重试'}</button>
  </div>;
}

function StudioExternalRefreshBanner({ message, onReload }) {
  return <div className="studio-banner warning" role="status">
    <AlertCircle size={14} /><span>{message}</span>
    <button className="btn btn-sm" onClick={onReload}>放弃草稿并载入新版</button>
  </div>;
}

function StudioWarningBanner({ warnings, count, onDismiss }) {
  return <div className="studio-banner warning" role="status">
    <AlertCircle size={14} />
    <details><summary>本次编辑没有增加问题；知识包原有 {count || warnings.length} 项引用问题仍需后续处理</summary>
      <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
    </details>
    <button className="btn btn-ghost" aria-label="关闭历史问题提示" title="关闭提示" onClick={onDismiss}><X size={14} /></button>
  </div>;
}

export function StudioAccessBanner({ access }) {
  if (!access || (access.role !== 'viewer' && access.lease?.mode === 'write')) return null;
  if (access.role === 'viewer') {
    return <div className="studio-banner readonly" role="status">
      <AlertCircle size={14} /><span>你是只读成员，可以查看和整理个人视图；新建、编辑、批量和删除已关闭。</span>
    </div>;
  }
  if (access.lease?.mode === 'review') {
    return <div className="studio-banner readonly" role="status">
      <AlertCircle size={14} /><span>当前有待确认修改，确认或退回后才能继续编辑；输入框仍可提问。</span>
    </div>;
  }
  if (access.lease?.mode === 'readonly') {
    return <div className="studio-banner readonly" role="status">
      <AlertCircle size={14} /><span>{access.lease.owner} 正在建模；租约结束前以只读方式打开，输入框仍可提问。</span>
    </div>;
  }
  return <div className="studio-banner warning" role="status">
    <AlertCircle size={14} /><span>上一位建模者的租约已结束。首次编辑会接管现有候选，请先检查消息流中的待答问题。</span>
  </div>;
}

function StudioPendingReviewBanner({ state, onSubmit, blocked = false }) {
  return <div className={`studio-banner ${state.phase === 'error' ? 'error' : 'warning'}`} role="status">
    <Save size={14} />
    <span>{blocked
      ? '当前条目仍有未保存或不合法的输入，修正并保存后才能应用工作草稿。'
      : state.message || '已有 Studio 修改保存为工作草稿；应用前只执行确定性代码检查，不调用建模助手。'}</span>
    <button className="btn btn-sm btn-p" disabled={state.phase === 'saving' || blocked} onClick={onSubmit}
      title={blocked ? '请先修正并保存当前条目的本地输入' : '执行确定性检查并应用工作草稿'}>
      {state.phase === 'saving' ? '检查中…' : '检查并应用修改'}
    </button>
  </div>;
}

export function studioFieldEditable(kindSchema, field) {
  return Boolean(kindSchema?.capabilities?.update) && field?.readOnly !== true;
}

function StudioEmpty({ schema, onCreate, onRefresh }) {
  return <div className="studio-state"><Boxes size={26} /><b>还没有知识条目</b><span>可以从具体类型开始建立，也可以等待助手生成。</span>
    <div className="studio-empty-actions">{(schema?.kinds || []).filter((kind) => kind.capabilities?.create).map((kind) => (
      <button className="btn" key={kind.kind} onClick={() => onCreate(kind.kind)}><Plus size={13} />新建{kind.label}</button>
    ))}</div>
    <button className="btn btn-ghost" onClick={onRefresh}>重新检查</button>
  </div>;
}

function FilteredEmpty() {
  return <div className="studio-state"><Search size={24} /><b>没有匹配项</b><span>调整搜索词或类型筛选后再看。</span></div>;
}
