import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { getPackage, getPackageCheck, runPackageCheck } from './api.js';
import { synchronizePackageCheck } from './packageCheckRefresh.js';
import {
  addFlowchartDiagram,
  advanceProjectSession,
  getFlowchartDraft,
  getProjectSession,
  getRevisionDiff,
  listAiConstructionMessages,
  listRevisions,
  previewMermaidFlowchartImport,
  respondToScenarioReview,
  restoreRevision,
  saveFlowchartDraft,
} from './authoring-api.js';
import { useProjectEvents } from './projectEventStream.jsx';
import DataSourcePanel from './DataSourcePanel.jsx';
import EvaluationLifecycleTabs, { evaluationLifecycleTarget } from './EvaluationLifecycleTabs.jsx';
import { rememberEvaluationView } from './evaluationLifecycleState.js';
import { ReviewPanel } from './M2Panels.jsx';
import { EvaluatePanel, TestcasesPanel } from './M3Panels.jsx';
import { FailureDiagnosisPanel, OptimizationPanel } from './M4Panel.jsx';
import PackageChat from './PackageChat.jsx';
import PendingInteraction from './PendingInteraction.jsx';
import SemanticBindingPanel from './SemanticBindingPanel.jsx';
import PaneResizeHandle from './PaneResizeHandle.jsx';
import { assistantPaneBounds, assistantPaneRatio, assistantPaneWidth, ASSISTANT_PANE_RATIO } from './paneLayout.js';
import { packageWorkspaceFocus } from './packageWorkspaceFocus.js';
import { isPackageChatRunning } from './packageChatRun.js';
import { activeOperationOf } from './activeRun.js';
import ReleasePanel from './ReleasePanel.jsx';
import VersionPanel from './VersionPanel.jsx';
import { ScenarioPanel } from './WorkflowExtraPanels.jsx';
import {
  constructionInteractionQueue,
  isConstructionInteraction,
} from './constructionInteraction.js';
import CandidateRevisionPanel, { CandidateRevisionCanvas } from './CandidateRevisionPanel.jsx';
import { createProjectEventReplayGate } from './projectEventReplay.js';
import {
  planPackageWorkspaceProjectEvent,
  planPackageWorkspaceWindowRefresh,
} from './packageWorkspaceRefresh.js';
import {
  isProgressOnlyMessageEvent,
  liveAiConstructionMessage,
  mergeAiConstructionSession,
  prependAiConstructionMessagePage,
  upsertPendingAuthoringInteraction,
  upsertAiConstructionMessage,
} from './aiConstructionLiveState.js';
import { FlowchartAddDialog, FlowchartEditor } from './flowchart-editor/index.js';
import {
  confirmUnsavedNavigation,
  protectUnsavedBeforeUnload,
} from './flowchartNavigationGuard.js';

const TABS = [
  ['edit', '编辑与检查'],
  ['scenario', '场景推演'],
  ['data', '数据源'],
  ['evaluate', '评测'],
  ['improve', '诊断与优化'],
  ['versions', '版本'],
  ['release', '发布'],
];

function checkState(pkg, check) {
  if (!pkg) return { tone: 'muted', text: '尚无知识包' };
  if (!check) return { tone: 'warn', text: '待检查' };
  if (check.revision && check.revision !== pkg.revision) return { tone: 'warn', text: '内容已更新，需重查' };
  const errors = check.findings?.filter((finding) => finding.severity === '错误').length ?? 0;
  return errors
    ? { tone: 'bad', text: `${errors} 个错误` }
    : { tone: 'good', text: '当前内容已通过检查' };
}

export default function PackageWorkspace({
  projectId,
  initialSession = null,
  embedded = false,
  focus = 'ai_construction',
  onNavigate,
  onUnsavedChange,
}) {
  const [tab, setTab] = useState('edit');
  const [evalView, setEvalView] = useState('cases');
  const [improveView, setImproveView] = useState('diagnose');
  const [mountedEvaluationViews, setMountedEvaluationViews] = useState(() => new Set(['cases']));
  const [deliveryView, setDeliveryView] = useState('release');
  const [session, setSession] = useState(initialSession);
  const [pkg, setPkg] = useState(null);
  const [check, setCheck] = useState(null);
  const [warningReview, setWarningReview] = useState(null);
  const [errorDeferral, setErrorDeferral] = useState(null);
  // 自动修复的可用性由后端在校验响应里声明，跟着体检快照一起更新
  const [repairAvailability, setRepairAvailability] = useState(null);
  const [rechecking, setRechecking] = useState(false);
  const [checkError, setCheckError] = useState('');
  const [error, setError] = useState('');
  const [revisions, setRevisions] = useState({ versions: [], currentRevision: '' });
  const [selectedRevision, setSelectedRevision] = useState('');
  const [diff, setDiff] = useState(undefined);
  const [diffLoading, setDiffLoading] = useState(false);
  const [versionLoading, setVersionLoading] = useState(false);
  const [versionError, setVersionError] = useState('');
  // 存比例而不是像素：窗口尺寸变化时版面观感保持一致
  const [assistantRatio, setAssistantRatio] = useState(ASSISTANT_PANE_RATIO.default);
  const [gridWidth, setGridWidth] = useState(0);
  const [interactionBusy, setInteractionBusy] = useState(false);
  const [olderMessagesBusy, setOlderMessagesBusy] = useState(false);
  const [olderMessagesError, setOlderMessagesError] = useState('');
  const [candidatePreview, setCandidatePreview] = useState(null);
  const [candidateRefreshKey, setCandidateRefreshKey] = useState(0);
  const [candidateAttentionKey, setCandidateAttentionKey] = useState('');
  const [pendingInteractionAttentionKey, setPendingInteractionAttentionKey] = useState('');
  const [selectedPendingInteractionId, setSelectedPendingInteractionId] = useState('');
  const activeCheckRunsRef = useRef(0);
  const sessionCursorRef = useRef(0);
  // 回放闸门与合并加载的定时器跨事件存活，因此放在 ref 而不是订阅副作用的闭包里
  const eventStateRef = useRef({ gate: null, loadTimer: null });
  const pendingFileSelectionRef = useRef(null);
  const [flowchartDraft, setFlowchartDraft] = useState(null);
  const [flowchartDraftError, setFlowchartDraftError] = useState('');
  const [flowchartDraftLoading, setFlowchartDraftLoading] = useState(false);
  const [flowchartEditorState, setFlowchartEditorState] = useState(null);
  const [flowchartAddOpen, setFlowchartAddOpen] = useState(false);
  const [flowchartAddMode, setFlowchartAddMode] = useState('assistant');
  const [flowchartAddBusy, setFlowchartAddBusy] = useState(false);
  const [flowchartSubmitBusy, setFlowchartSubmitBusy] = useState(false);
  const [flowchartAddError, setFlowchartAddError] = useState('');
  const [flowchartAddedDiagramId, setFlowchartAddedDiagramId] = useState('');
  const [scenarioReviewFocusRequested, setScenarioReviewFocusRequested] = useState(false);
  const [packageDraftDirty, setPackageDraftDirty] = useState(false);
  const workspaceDirtyRef = useRef(false);
  const deferredRefreshRef = useRef(false);
  const editGridRef = useRef(null);
  const scenarioReviewRef = useRef(null);
  const focusedView = packageWorkspaceFocus(focus);
  const activeTab = embedded ? focusedView.tab : tab;
  const sessionReady = Boolean(session);
  const refreshCandidateRevisions = useCallback(() => {
    setCandidateRefreshKey((value) => value + 1);
  }, []);
  const focusPendingInteraction = useCallback((interaction) => {
    if (!interaction?.id) return;
    setSelectedPendingInteractionId(interaction.id);
    setPendingInteractionAttentionKey(`${interaction.id}:${Date.now()}`);
  }, []);
  const openPendingInteraction = useCallback((result) => {
    if (!result?.interaction) return;
    const cursor = Number(result.cursor ?? 0) || 0;
    sessionCursorRef.current = Math.max(sessionCursorRef.current, cursor);
    setSession((current) => upsertPendingAuthoringInteraction(
      current,
      result.interaction,
      cursor,
    ));
    focusPendingInteraction(result.interaction);
  }, [focusPendingInteraction]);
  const applyPackageCheckResult = useCallback((result) => {
    setCheck({
      version: result.version,
      revision: result.revision,
      findings: result.findings,
      createdAt: result.createdAt ?? new Date().toISOString(),
    });
    setWarningReview(result.warningReview ?? null);
    setErrorDeferral(result.errorDeferral ?? null);
    setRepairAvailability(result.repair ?? null);
    if (result.packageRepair) {
      setSession((current) => current ? {
        ...current,
        packageRepair: result.packageRepair,
      } : current);
    }
  }, []);
  const startCheckRun = useCallback(() => {
    activeCheckRunsRef.current += 1;
    setRechecking(true);
    setCheckError('');
  }, []);
  const finishCheckRun = useCallback(() => {
    activeCheckRunsRef.current = Math.max(0, activeCheckRunsRef.current - 1);
    if (activeCheckRunsRef.current === 0) setRechecking(false);
  }, []);
  const recheckPackage = useCallback(async () => {
    startCheckRun();
    try {
      const result = await runPackageCheck(projectId);
      applyPackageCheckResult(result);
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (cause) {
      setCheckError(cause instanceof Error ? cause.message : '体检失败');
    } finally {
      finishCheckRun();
    }
  }, [applyPackageCheckResult, finishCheckRun, projectId, startCheckRun]);

  useEffect(() => {
    if (!embedded) return;
    if (focusedView.evalView) setEvalView(focusedView.evalView);
    if (focusedView.improveView) setImproveView(focusedView.improveView);
    const view = focusedView.improveView || focusedView.evalView
      || (focusedView.tab === 'evaluate' ? 'cases' : '');
    if (view) setMountedEvaluationViews((current) => rememberEvaluationView(current, view));
  }, [embedded, focus, focusedView.evalView, focusedView.improveView]);

  // 栅格要等 session/pkg 就绪后才渲染，所以依赖里必须带上就绪标志，
  // 否则首次 effect 拿到的是空 ref、之后再也不会重跑，宽度永远停在下限。
  const workspaceReady = Boolean(session && pkg);
  useEffect(() => {
    const element = editGridRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const update = () => { setGridWidth(element.clientWidth); };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [activeTab, workspaceReady]);

  // 助手在「流程图里还没有任何流程」时给出的出口，由工作台负责打开弹窗
  useEffect(() => {
    const open = (event) => {
      if (event.detail?.projectId !== projectId) return;
      setFlowchartAddError('');
      setFlowchartAddMode('assistant');
      setFlowchartAddOpen(true);
    };
    window.addEventListener('mw-flowchart-add-open', open);
    return () => window.removeEventListener('mw-flowchart-add-open', open);
  }, [projectId]);

  const load = useCallback(async (options) => {
    try {
      const readSession = options?.advance === true ? advanceProjectSession : getProjectSession;
      const [nextSession, nextPackage, nextCheck] = await Promise.all([
        readSession(projectId),
        getPackage(projectId),
        getPackageCheck(projectId),
      ]);
      sessionCursorRef.current = Number(nextSession?.cursor ?? 0) || 0;
      setSession((current) => mergeAiConstructionSession(current, nextSession));
      setPkg(nextPackage);
      setCheck(nextCheck.check);
      setWarningReview(nextCheck.warningReview ?? null);
      setErrorDeferral(nextCheck.errorDeferral ?? null);
      setError('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '知识包工作区加载失败');
    }
  }, [projectId]);

  // 本地写操作后发的 window 事件与 SSE 事件描述的是同一次变更，合流到一个防抖窗口，
  // 否则一次保存会触发两次 load，先后到达顺序还不确定。
  const scheduleLoad = useCallback(() => {
    const store = eventStateRef.current;
    if (store.loadTimer) return;
    store.loadTimer = setTimeout(() => {
      store.loadTimer = null;
      void load();
    }, 200);
  }, [load]);

  useEffect(() => {
    if (initialSession?.project?.id !== projectId) return;
    sessionCursorRef.current = Number(initialSession.cursor ?? 0) || 0;
    setSession((current) => mergeAiConstructionSession(current, initialSession));
  }, [initialSession, projectId]);

  const loadRevisions = useCallback(async () => {
    setVersionLoading(true); setVersionError('');
    try { setRevisions(await listRevisions(projectId)); }
    catch (cause) { setVersionError(cause instanceof Error ? cause.message : '版本读取失败'); }
    finally { setVersionLoading(false); }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  // 内容变更后自动重查，挂在工作区而不是体检面板上：面板只在「编辑与检查」页签渲染，
  // 而评测和诊断页签下同样会人工改包，重查必须与页签解耦。
  useEffect(() => {
    const sync = synchronizePackageCheck({
      projectId,
      eventTarget: window,
      loadCheck: getPackageCheck,
      runCheck: runPackageCheck,
      onSnapshot: (result) => {
        setCheck(result.check);
        setWarningReview(result.warningReview);
        setErrorDeferral(result.errorDeferral ?? null);
        setRepairAvailability(result.repair ?? null);
      },
      onChecked: (result) => {
        applyPackageCheckResult(result);
      },
      onPackageUpdated: () => {
        setCheck(null);
        setWarningReview(null);
        setErrorDeferral(null);
        setRepairAvailability(null);
      },
      onError: (cause) => setCheckError(cause instanceof Error ? cause.message : '体检失败'),
      onRecheckStart: startCheckRun,
      onRecheckFinish: finishCheckRun,
    });
    return () => sync.stop();
  }, [applyPackageCheckResult, finishCheckRun, projectId, startCheckRun]);
  useEffect(() => {
    setOlderMessagesBusy(false);
    setOlderMessagesError('');
  }, [projectId]);

  const loadOlderMessages = useCallback(async () => {
    const beforeSeq = session?.aiConstructionMessages?.[0]?.seq;
    if (olderMessagesBusy || !session?.aiConstructionMessagesPage?.hasMore || !beforeSeq) return 0;
    setOlderMessagesBusy(true);
    setOlderMessagesError('');
    try {
      const page = await listAiConstructionMessages(projectId, { beforeSeq, limit: 100 });
      setSession((current) => current?.project?.id === projectId
        ? prependAiConstructionMessagePage(current, page)
        : current);
      return page.messages?.length ?? 0;
    } catch (cause) {
      setOlderMessagesError(cause instanceof Error ? cause.message : '更早消息加载失败');
      return 0;
    } finally {
      setOlderMessagesBusy(false);
    }
  }, [olderMessagesBusy, projectId, session]);
  useEffect(() => {
    if (activeTab === 'versions' || (embedded && activeTab === 'release' && deliveryView === 'versions')) {
      void loadRevisions();
    }
  }, [activeTab, deliveryView, embedded, loadRevisions]);
  useEffect(() => {
    const refresh = () => {
      const plan = planPackageWorkspaceWindowRefresh({ activeTab, embedded, deliveryView });
      if (plan.scheduleLoad) scheduleLoad();
      if (plan.loadRevisions) void loadRevisions();
    };
    window.addEventListener('mw-package-updated', refresh);
    window.addEventListener('mw-package-check-updated', refresh);
    return () => {
      window.removeEventListener('mw-package-updated', refresh);
      window.removeEventListener('mw-package-check-updated', refresh);
    };
  }, [scheduleLoad, loadRevisions, activeTab, deliveryView, embedded]);
  const handleProjectEvent = (event) => {
    const store = eventStateRef.current;
    store.gate ??= createProjectEventReplayGate(sessionCursorRef.current);
    const plan = planPackageWorkspaceProjectEvent({
      event,
      gate: store.gate,
      workspaceDirty: workspaceDirtyRef.current,
      activeTab,
      embedded,
      deliveryView,
    });
    store.gate = plan.gate;
    if (plan.candidateRefresh) setCandidateRefreshKey((value) => value + 1);
    if (plan.deferLoad) deferredRefreshRef.current = true;
    if (plan.loadRevisions) void loadRevisions();
    // 题卡打开事件就是后端已切到 waiting 的信号。立即回源会话，不等普通事件的
    // 200ms 防抖，保证 waiting 状态与 pendingInteraction 在同一轮渲染中到达。
    if (plan.immediateLoad) void load({ advance: true });
    // 消息随事件内联送达，先贴上去让回显立即可见；任务图等其余状态仍按需回源。
    const liveMessage = liveAiConstructionMessage(event);
    if (liveMessage && !plan.historical) {
      setSession((current) => current ? {
        ...current,
        aiConstructionMessages: upsertAiConstructionMessage(
          current.aiConstructionMessages ?? [],
          liveMessage,
        ),
      } : current);
      if (isProgressOnlyMessageEvent(event)) return;
    }
    if (plan.scheduleLoad) scheduleLoad();
  };

  useProjectEvents(projectId, handleProjectEvent, { enabled: sessionReady });

  useEffect(() => {
    const store = eventStateRef.current;
    store.gate = null;
    return () => {
      if (store.loadTimer) clearTimeout(store.loadTimer);
      store.loadTimer = null;
    };
  }, [projectId]);

  const openRevision = async (id, item) => {
    setSelectedRevision(id);
    if (!item.parentRevision) {
      setDiff({ fromRevision: id, toRevision: id, files: [] });
      return;
    }
    setDiffLoading(true); setVersionError('');
    try { setDiff(await getRevisionDiff(projectId, item.parentRevision, id)); }
    catch (cause) { setVersionError(cause instanceof Error ? cause.message : '修改读取失败'); }
    finally { setDiffLoading(false); }
  };
  const restore = async (payload) => {
    await restoreRevision(projectId, payload);
    setDiff(undefined); setSelectedRevision('');
    await Promise.all([load(), loadRevisions()]);
    window.dispatchEvent(new CustomEvent('mw-package-updated'));
  };

  const status = useMemo(() => checkState(pkg, check), [pkg, check]);
  const steps = session?.workflow?.steps ?? [];
  const scenarioStep = steps.find((step) => step.id === 'scenario');
  const scenarioProgress = (session?.workflow?.progress ?? [])
    .find((item) => item.stepId === 'scenario');
  const pendingInteractions = session?.authoring?.pendingInteractions ?? [];
  const constructionQueue = constructionInteractionQueue(
    pendingInteractions,
    pkg?.revision,
  );
  const pendingQueueKey = constructionQueue.map((interaction) => interaction.id).join(':');
  const selectedQueueInteraction = constructionQueue.find((interaction) => (
    interaction.id === selectedPendingInteractionId
  )) ?? constructionQueue[0] ?? null;
  const constructionInteraction = isConstructionInteraction(selectedQueueInteraction)
    ? selectedQueueInteraction
    : null;
  const processReview = constructionInteraction?.type === 'flowchart_review'
    && constructionInteraction?.payload?.purpose === 'process_review'
    ? constructionInteraction
    : null;
  const scenarioInteraction = pendingInteractions.find((interaction) => (
    interaction.type === 'scenario_review'
  )) ?? null;
  const openScenarioCollaboration = useCallback(() => {
    setScenarioReviewFocusRequested(true);
  }, []);
  const selectPendingInteraction = useCallback((interaction) => {
    if (!interaction?.id) return;
    if (interaction.type === 'scenario_review') {
      setScenarioReviewFocusRequested(true);
      if (embedded) onNavigate?.('scenario');
      else setTab('scenario');
      return;
    }
    focusPendingInteraction(interaction);
  }, [embedded, focusPendingInteraction, onNavigate]);
  useEffect(() => {
    if (constructionQueue.some((interaction) => interaction.id === selectedPendingInteractionId)) return;
    setSelectedPendingInteractionId(constructionQueue[0]?.id ?? '');
  }, [pendingQueueKey, selectedPendingInteractionId]);
  useEffect(() => {
    if (!scenarioReviewFocusRequested || activeTab !== 'scenario' || !scenarioInteraction) return;
    scenarioReviewRef.current?.focus({ preventScroll: true });
    scenarioReviewRef.current?.scrollIntoView({ block: 'nearest' });
    setScenarioReviewFocusRequested(false);
  }, [activeTab, scenarioInteraction?.id, scenarioReviewFocusRequested]);
  const flowchartDraftDirty = Boolean(processReview && flowchartEditorState?.dirty);
  const workspaceDraftDirty = flowchartDraftDirty || packageDraftDirty;
  workspaceDirtyRef.current = workspaceDraftDirty;
  const confirmDiscardFlowchartChanges = useCallback(() => {
    if (!confirmUnsavedNavigation(workspaceDraftDirty)) return false;
    if (flowchartDraftDirty) setFlowchartEditorState(null);
    return true;
  }, [flowchartDraftDirty, workspaceDraftDirty]);

  useEffect(() => {
    onUnsavedChange?.(workspaceDraftDirty);
  }, [workspaceDraftDirty, onUnsavedChange]);

  useEffect(() => {
    if (workspaceDraftDirty || !deferredRefreshRef.current) return;
    deferredRefreshRef.current = false;
    void load();
  }, [workspaceDraftDirty, load]);

  useEffect(() => {
    if (!workspaceDraftDirty) return undefined;
    const beforeUnload = (event) => { protectUnsavedBeforeUnload(event, true); };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [workspaceDraftDirty]);

  useEffect(() => () => { onUnsavedChange?.(false); }, [onUnsavedChange]);

  useEffect(() => {
    let active = true;
    if (!processReview) {
      setFlowchartDraft(null);
      setFlowchartDraftError('');
      setFlowchartEditorState(null);
      setFlowchartAddOpen(false);
      setFlowchartAddedDiagramId('');
      setFlowchartSubmitBusy(false);
      return () => { active = false; };
    }
    setFlowchartDraftLoading(true);
    setFlowchartDraft(null);
    setFlowchartEditorState(null);
    setFlowchartAddOpen(false);
    setFlowchartAddedDiagramId('');
    setFlowchartSubmitBusy(false);
    setFlowchartAddError('');
    setFlowchartDraftError('');
    void getFlowchartDraft(projectId, processReview.id)
      .then((draft) => { if (active) setFlowchartDraft(draft); })
      .catch((cause) => {
        if (active) setFlowchartDraftError(cause instanceof Error ? cause.message : '流程图草稿加载失败');
      })
      .finally(() => { if (active) setFlowchartDraftLoading(false); });
    return () => { active = false; };
  }, [processReview?.id, projectId]);

  const saveCurrentFlowchartDraft = async (document, { expectedRevision }) => {
    if (!processReview) throw new Error('流程图确认已结束');
    const saved = await saveFlowchartDraft(projectId, processReview.id, expectedRevision, document);
    setFlowchartDraft(saved);
    return saved;
  };

  const addFlowchart = async (input) => {
    if (!processReview || !flowchartDraft || flowchartAddBusy) return;
    if (flowchartEditorState?.dirty) {
      setFlowchartAddError('请先保存当前流程图修改，再继续添加');
      return;
    }
    setFlowchartAddBusy(true);
    setFlowchartAddError('');
    try {
      const saved = await addFlowchartDiagram(projectId, processReview.id, {
        ...input,
        expectedRevision: flowchartDraft.draftRevision,
      });
      setFlowchartDraft(saved);
      setFlowchartAddedDiagramId(saved.document?.diagrams?.at(-1)?.id || '');
      setFlowchartAddOpen(false);
    } catch (cause) {
      setFlowchartAddError(cause instanceof Error ? cause.message : '流程图添加失败');
    } finally {
      setFlowchartAddBusy(false);
    }
  };

  const previewMermaidImport = async (input) => {
    if (!processReview || !flowchartDraft) throw new Error('流程图确认已结束');
    if (flowchartEditorState?.dirty) throw new Error('请先保存当前流程图修改，再导入 Mermaid');
    setFlowchartAddBusy(true);
    try {
      return await previewMermaidFlowchartImport(projectId, processReview.id, {
        ...input,
        expectedRevision: flowchartDraft.draftRevision,
      });
    } finally {
      setFlowchartAddBusy(false);
    }
  };

  const importMermaid = async (input) => {
    if (!processReview || !flowchartDraft) throw new Error('流程图确认已结束');
    if (flowchartEditorState?.dirty) throw new Error('请先保存当前流程图修改，再导入 Mermaid');
    setFlowchartAddBusy(true);
    try {
      const saved = await addFlowchartDiagram(projectId, processReview.id, {
        mode: 'mermaid',
        ...input,
        expectedRevision: flowchartDraft.draftRevision,
      });
      setFlowchartDraft(saved);
      setFlowchartAddedDiagramId(saved.document?.diagrams?.at(-1)?.id || '');
      setFlowchartAddOpen(false);
      return saved;
    } finally {
      setFlowchartAddBusy(false);
    }
  };

  const answerScenario = async (interactionId, response) => {
    if (!scenarioInteraction || interactionBusy) return;
    setInteractionBusy(true);
    try {
      const result = await respondToScenarioReview(projectId, interactionId, response);
      await load();
      if (result.remainingCount > 0) openScenarioCollaboration();
    } finally {
      setInteractionBusy(false);
    }
  };

  const evaluationLifecycleView = activeTab === 'improve' ? improveView : evalView;
  const selectEvaluationLifecycle = (view) => {
    const target = evaluationLifecycleTarget(view);
    setMountedEvaluationViews((current) => rememberEvaluationView(current, view));
    if (target.workspaceTab === 'evaluate') setEvalView(target.view);
    else setImproveView(target.view);
    if (embedded) onNavigate?.(target.workflowStepId);
    else setTab(target.workspaceTab);
  };
  // 助手栏里的「进入优化更新」把评测缺陷交给能反推资源改动的引擎。
  // 用 ref 承接最新的跳转闭包，订阅本身只依赖 projectId。
  const openEvaluationViewRef = useRef(null);
  openEvaluationViewRef.current = (view) => {
    if (!confirmDiscardFlowchartChanges()) return;
    selectEvaluationLifecycle(view);
  };
  useEffect(() => {
    const handler = (event) => {
      if (event?.detail?.projectId && event.detail.projectId !== projectId) return;
      const view = String(event?.detail?.view ?? '');
      if (view) openEvaluationViewRef.current?.(view);
    };
    window.addEventListener('mw-open-evaluation-view', handler);
    return () => window.removeEventListener('mw-open-evaluation-view', handler);
  }, [projectId]);

  // 评测/优化阶段不挂载 ReviewPanel，先保存助手发起的文件定位请求，回到编辑页后再交给文件面板。
  useEffect(() => {
    const selectFileFromStage = (event) => {
      const detail = event?.detail ?? {};
      if (detail.projectId !== projectId || typeof detail.path !== 'string') return;
      if (!['scenario', 'data', 'evaluate', 'improve'].includes(activeTab)) return;
      if (activeTab === 'edit') return;
      if (!confirmDiscardFlowchartChanges()) return;
      pendingFileSelectionRef.current = { projectId, path: detail.path };
      if (embedded) onNavigate?.('ai_construction');
      else setTab('edit');
    };
    window.addEventListener('mw-package-file-select', selectFileFromStage);
    return () => window.removeEventListener('mw-package-file-select', selectFileFromStage);
  }, [activeTab, confirmDiscardFlowchartChanges, embedded, onNavigate, projectId]);

  useEffect(() => {
    if (activeTab !== 'edit') return undefined;
    const pending = pendingFileSelectionRef.current;
    if (!pending || pending.projectId !== projectId) return undefined;
    pendingFileSelectionRef.current = null;
    const timer = window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent('mw-package-file-select', {
        detail: { projectId, path: pending.path, target: pending.path },
      }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, projectId]);

  const evaluationLifecycleTabs = (
    <EvaluationLifecycleTabs
      activeView={evaluationLifecycleView}
      onSelect={selectEvaluationLifecycle}
    />
  );
  const evaluationViewsToRender = rememberEvaluationView(
    mountedEvaluationViews,
    evaluationLifecycleView,
  );

  const evaluationPanel = (view) => {
    if (view === 'cases') {
      return <TestcasesPanel projectId={projectId} onAdvance={() => selectEvaluationLifecycle('run')} />;
    }
    if (view === 'run') {
      return <EvaluatePanel projectId={projectId} onProjectChanged={load}
        onRoundFinished={(_round, next) => {
          if (next !== 'diagnose') return;
          selectEvaluationLifecycle('diagnose');
        }} />;
    }
    if (view === 'diagnose') {
      return <FailureDiagnosisPanel projectId={projectId} currentRevisionOid={pkg?.revision} onChanged={load}
        onAdvance={() => selectEvaluationLifecycle('optimize')}
        onGoEvaluate={() => selectEvaluationLifecycle('run')} />;
    }
    return <OptimizationPanel projectId={projectId} onChanged={load}
      active={evaluationLifecycleView === 'optimize'}
      onCandidateCreated={refreshCandidateRevisions}
      onGoEvaluate={() => selectEvaluationLifecycle('run')}
      onGoDiagnose={() => selectEvaluationLifecycle('diagnose')} />;
  };

  const assistantBounds = assistantPaneBounds(gridWidth);
  const assistantWidth = assistantPaneWidth(gridWidth, assistantRatio);

  const stageContent = (() => {
    if (activeTab === 'edit') {
      if (candidatePreview) {
        return <CandidateRevisionCanvas preview={candidatePreview} onClose={() => setCandidatePreview(null)} onOpenFile={(path) => {
          setCandidatePreview(null);
          window.dispatchEvent(new CustomEvent('mw-package-file-select', {
            detail: { projectId, path: String(path).replace(/^package\//, '') },
          }));
        }} />;
      }
      if (processReview) {
        if (flowchartDraftLoading && !flowchartDraft) return <div className="empty">正在加载可编辑流程图…</div>;
        if (flowchartDraftError && !flowchartDraft) return <div className="empty">{flowchartDraftError}</div>;
        if (!flowchartDraft) return <div className="empty">流程图草稿尚未就绪</div>;
        return <>
          <div className="rv-flowchart">
            <FlowchartEditor
              key={`${projectId}:${processReview.id}`}
              document={flowchartDraft.document}
              savedRevision={flowchartDraft.draftRevision}
              initialDiagramId={flowchartAddedDiagramId}
              disabled={interactionBusy || flowchartAddBusy || flowchartSubmitBusy}
              onSave={saveCurrentFlowchartDraft}
              onStateChange={setFlowchartEditorState}
              onRecommendDiagram={() => {
                setFlowchartAddError('');
                setFlowchartAddMode('assistant');
                setFlowchartAddOpen(true);
              }}
              onImportMermaid={() => {
                setFlowchartAddError('');
                setFlowchartAddMode('mermaid');
                setFlowchartAddOpen(true);
              }}
              onCreateBlankDiagram={() => { void addFlowchart({ mode: 'blank' }); }}
            />
          </div>
          <FlowchartAddDialog
            open={flowchartAddOpen}
            initialMode={flowchartAddMode}
            candidates={flowchartDraft.availableCandidates}
            modelingGoal={flowchartDraft.modelingGoal}
            busy={flowchartAddBusy}
            error={flowchartAddError}
            onClose={() => setFlowchartAddOpen(false)}
            onAddCandidate={(candidate) => { void addFlowchart({
              mode: 'candidate',
              ...(candidate?.candidateId
                ? { candidateId: candidate.candidateId }
                : { candidateTitle: candidate?.title || '' }),
            }); }}
            onAddAi={(instruction) => { void addFlowchart({ mode: 'ai', instruction }); }}
            onPreviewMermaid={previewMermaidImport}
            onImportMermaid={importMermaid}
          />
        </>;
      }
      return null;
    }
    if (activeTab === 'scenario') {
      return (
        <div className={`pkw-scenario-grid${scenarioInteraction ? ' with-review' : ''}`}>
          <ScenarioPanel
            projectId={projectId}
            step={scenarioStep}
            progress={scenarioProgress}
            onChanged={load}
            onOpenCollaboration={openScenarioCollaboration}
          />
          {scenarioInteraction && (
            <aside ref={scenarioReviewRef} className="pkw-scenario-review" tabIndex={-1}
              aria-label="场景推演确认">
              <PendingInteraction
                interaction={scenarioInteraction}
                busy={interactionBusy}
                activeOperation={activeOperationOf(session)}
                onSubmit={answerScenario}
              />
            </aside>
          )}
        </div>
      );
    }
    if (activeTab === 'data') {
      return <DataSourcePanel projectId={projectId} onAdvance={() => {
        setEvalView('cases');
        if (embedded) onNavigate?.('evaluate');
        else setTab('evaluate');
      }} />;
    }
    if (activeTab === 'evaluate' || activeTab === 'improve') {
      return (
        <section>
          {evaluationLifecycleTabs}
          {[...evaluationViewsToRender].map((view) => (
            <div key={view} className="pkw-evaluation-panel"
              hidden={evaluationLifecycleView !== view}
              aria-hidden={evaluationLifecycleView !== view}>
              {evaluationPanel(view)}
            </div>
          ))}
        </section>
      );
    }
    if (activeTab === 'versions') {
      return <VersionPanel
        versions={revisions.versions}
        currentVersionId={revisions.currentRevision}
        selectedVersionId={selectedRevision}
        diff={diff}
        loading={versionLoading}
        diffLoading={diffLoading}
        error={versionError}
        onSelectVersion={openRevision}
        onRestore={restore}
      />;
    }
    if (activeTab === 'release') {
      return (
        <section>
          {embedded && (
            <div className="pkw-subtabs">
              <button className={deliveryView === 'release' ? 'on' : ''}
                onClick={() => setDeliveryView('release')}>发布检查</button>
              <button className={deliveryView === 'versions' ? 'on' : ''}
                onClick={() => setDeliveryView('versions')}>版本记录</button>
            </div>
          )}
          {embedded && deliveryView === 'versions'
            ? <VersionPanel
                versions={revisions.versions}
                currentVersionId={revisions.currentRevision}
                selectedVersionId={selectedRevision}
                diff={diff}
                loading={versionLoading}
                diffLoading={diffLoading}
                error={versionError}
                onSelectVersion={openRevision}
                onRestore={restore}
              />
            : <ReleasePanel projectId={projectId} release={session?.release} steps={steps}
                onReleased={async () => { await load(); await loadRevisions(); }} />}
        </section>
      );
    }
    return null;
  })();

  if (error && !session) return <div className={`package-workspace${embedded ? ' embedded' : ''}`}><div className="empty">{error}</div></div>;
  if (!session || !pkg) return <div className={`package-workspace${embedded ? ' embedded' : ''}`}><div className="empty">加载知识包工作区…</div></div>;
  const workflowStageOnly = ['scenario', 'data', 'evaluate', 'improve'].includes(activeTab);
  const taskStageLabel = ['evaluate', 'improve'].includes(activeTab) ? '评测阶段' : '';

  return (
    <div className={`package-workspace${embedded ? ' embedded' : ''}`}>
      {!embedded && <header className="pkw-head">
        <div className="pkw-title">
          <div className="pkw-kicker">知识包工作区</div>
          <h2>{session.project.name}</h2>
          <span>v{pkg.version}</span>
          <span className={`pkw-status ${status.tone}`}>{status.text}</span>
        </div>
      </header>}

      {error && <div className="err-banner">{error}</div>}
      {!embedded && <nav className="pkw-tabs" aria-label="知识包工作区">
        {TABS.map(([id, label]) => (
          <button key={id} type="button" className={activeTab === id ? 'on' : ''} onClick={() => {
            if (id === activeTab || confirmDiscardFlowchartChanges()) setTab(id);
          }}>{label}</button>
        ))}
      </nav>}

      <main className="pkw-body pkw-body-edit">
        <div ref={editGridRef} className="pkw-edit-grid" style={{ '--pkw-assistant-width': `${assistantWidth}px` }}>
          <section className="pkw-editor">
            {workflowStageOnly
              ? <div className="pkw-workflow-stage">{stageContent}</div>
              : <ReviewPanel
                  projectId={projectId}
                  stageContent={stageContent}
                  reviewBanner={!stageContent && (
                    <SemanticBindingPanel
                      projectId={projectId}
                      packageRevision={pkg?.revision ?? ''}
                      disabled={Boolean(pkg.draft)}
                      onApplied={async () => { await load(); }}
                    />
                  )}
                  packageDraft={pkg.draft ?? null}
                  packageRepairBusy={isPackageChatRunning(session.authoring) ? 'plan' : ''}
                  onDraftChange={(draft) => setPkg((current) => current ? { ...current, draft } : current)}
                  packageCheck={{
                    check,
                    warningReview,
                    errorDeferral,
                    repair: repairAvailability,
                    packageRepair: session.packageRepair ?? null,
                    rechecking,
                    error: checkError,
                    pendingInteractions,
                    packageRevision: pkg?.revision ?? '',
                  }}
                  onCheckRecheck={recheckPackage}
                  onCheckInteractionOpened={openPendingInteraction}
                  onCheckInteractionFocus={focusPendingInteraction}
                  wideStage={activeTab === 'edit' && Boolean(processReview)}
                  onDirtyChange={setPackageDraftDirty}
                  onFileSelected={() => {
                    if (activeTab === 'edit') return;
                    if (embedded) onNavigate?.('ai_construction');
                    else setTab('edit');
                  }}
                />}
          </section>
          <PaneResizeHandle label="调整知识包建模助手栏宽度"
            value={assistantWidth} min={assistantBounds.min} max={assistantBounds.max}
            direction={-1} defaultValue={assistantPaneWidth(gridWidth)}
            onChange={(next) => setAssistantRatio(assistantPaneRatio(gridWidth, next))} />
          <aside className="pkw-assistant">
            <PackageChat
              projectId={projectId}
              taskStageLabel={taskStageLabel}
              structuredMessages={session.aiConstructionMessages ?? []}
              hasOlderMessages={Boolean(session.aiConstructionMessagesPage?.hasMore)}
              olderMessagesBusy={olderMessagesBusy}
              olderMessagesError={olderMessagesError}
              onLoadOlderMessages={loadOlderMessages}
              aiConstruction={session.aiConstruction}
              authoring={session.authoring}
              packageRevision={pkg?.revision ?? ''}
              activeOperation={activeOperationOf(session)}
              pendingInteraction={constructionInteraction}
              pendingInteractions={constructionQueue}
              onPendingInteractionSelect={selectPendingInteraction}
              pendingInteractionAttentionKey={pendingInteractionAttentionKey}
              candidateRevisionAttentionKey={candidateAttentionKey}
              candidateRevisionPanel={<CandidateRevisionPanel projectId={projectId}
                refreshKey={candidateRefreshKey}
                onVisibilityChange={setCandidateAttentionKey}
                confirmAction={confirmDiscardFlowchartChanges}
                onPreview={(preview) => {
                  setCandidatePreview(preview);
                  if (activeTab !== 'edit') {
                    if (embedded) onNavigate?.('ai_construction');
                    else setTab('edit');
                  }
                }}
                onChanged={async () => {
                  setCandidatePreview(null);
                  await load();
                  window.dispatchEvent(new CustomEvent('mw-package-updated'));
                }} />}
              flowchartDraftState={processReview ? {
                ...(flowchartEditorState ?? {}),
                draftRevision: flowchartDraft?.draftRevision ?? null,
                diagramCount: flowchartEditorState?.document?.diagrams?.length
                  ?? flowchartDraft?.document?.diagrams?.length ?? 0,
                warnings: flowchartDraft?.warnings ?? [],
                loading: flowchartDraftLoading,
                error: flowchartDraftError,
              } : null}
              onPendingInteractionBusyChange={setFlowchartSubmitBusy}
              confirmFlowchartMutation={confirmDiscardFlowchartChanges}
              onProjectChanged={load}
              onNavigate={embedded ? onNavigate : undefined}
            />
          </aside>
        </div>
      </main>
    </div>
  );
}
