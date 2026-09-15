/* 工作台共享状态。字段刻意保持扁平，方便 useStore 用稳定引用做选择器订阅。 */
import { createStore } from './store.js';
import * as F from './mock/fixtures.js';
import { refreshWorkbenchAfterConfirmation } from '../interaction-settlement.js';
import {
  answerAgent,
  cancelAgent,
  confirmMaterialPurposeReview as confirmMaterialPurposeReviewRequest,
  confirmMaterialPurposeReviews as confirmMaterialPurposeReviewsRequest,
  getAgentState,
  getCandidateDiff,
  getMaterial,
  getMaterialReferrers,
  getMaterialRevisions,
  getPackageFile,
  locateEvidence as getEvidenceLocation,
  getSnapshot,
  steerAgent,
  resumeAgentIssue,
  resumeAgentPlan,
  updateConfirmPolicy,
  updateMaterialDirectives,
  uploadMaterial,
  uploadPackageSource,
  savePackageDataSource,
  startAgentIncrementRound,
  respondChangePlan as respondChangePlanRequest,
  getProgress,
  safeTerminateChangePlan as safeTerminateChangePlanRequest,
  terminateStaleRun as terminateStaleRunRequest,
} from './api.js';
import { prefetchStudioBootstrap } from '../studio/studio-api.js';

/** demo 场景：对应设计草图的各屏状态。接后端后由真实事件驱动，这里只是入口。 */
export const SCENES = [
  { id: 'modeling',  label: '建模进行中',  focus: { kind: 'flow' },    leftTab: 'pkg' },
  { id: 'confirm',   label: '变更确认',    focus: { kind: 'diff' },    leftTab: 'pkg' },
  { id: 'materials', label: '资料与反查',  focus: { kind: 'doc' },     leftTab: 'mat' },
  { id: 'increment', label: '增量建模',    focus: { kind: 'impact' },  leftTab: 'pkg' },
  { id: 'merge',     label: '知识包融合',  focus: { kind: 'conflict' },leftTab: 'pkg' },
  { id: 'gates',     label: '门禁与发布',  focus: { kind: 'gates' },   leftTab: 'pkg' },
];

/** 支持 ?scene=confirm 直接定位到某个场景：便于评审、截图和 bug 复现。 */
function sceneFromUrl() {
  if (typeof window === 'undefined') return null;
  const q = new URLSearchParams(window.location.search).get('scene');
  return SCENES.some((s) => s.id === q) ? q : null;
}
const DEFAULT_STUDIO_FOCUS = { kind: 'studio', target: '知识包 Studio', view: 'graph' };
const boot = SCENES.find((s) => s.id === sceneFromUrl()) || SCENES[0];
const initialProjectId = projectFromUrl();
const initialStudioFocus = initialProjectId ? studioFocusFromLocation() || { ...DEFAULT_STUDIO_FOCUS } : null;
if (initialProjectId && initialStudioFocus) {
  void prefetchStudioBootstrap(initialProjectId, { limit: 200 }).catch(() => undefined);
}

const initial = {
  demo: !initialProjectId,
  projectId: initialProjectId,
  project: null,
  revision: null,
  packageVersion: null,
  packageStatus: null,
  workflow: null,
  phases: [],
  progress: null,
  progressError: '',
  changePlan: null,
  pausedPlan: null,
  files: [],
  fileContents: {},
  fileErrors: {},
  candidateDiff: [],
  candidateRevisionRefreshKey: 0,
  materials: [],
  materialContents: {},
  materialReferrers: {},
  messages: [],
  interaction: null,
  reviewedChangeGroups: [],
  studioPendingCount: 0,
  run: null,
  recovery: null,
  statusText: '',
  loading: Boolean(initialProjectId),
  error: '',
  uploads: [],
  trace: [],
  terminalRunIds: [],
  lastSeq: 0,
  replayUntilSeq: 0,
  autoStartSent: false,
  scene: boot.id,
  leftTab: boot.leftTab,                // pkg | mat
  focus: initialProjectId
    ? initialStudioFocus
    : { ...boot.focus, target: focusTargetFor(boot.focus.kind), node: boot.id === 'modeling' ? 'D2' : undefined },
  follow: !initialProjectId,            // 项目首页停留在 Studio；可主动开启跟随助手
  pendingFocus: null,                   // follow=false 时助手想去的地方，攒着等用户「跟上」
  selectedPath: initialProjectId ? '' : 'diagrams/采购申请.mmd',
  selectedMaterial: initialProjectId ? '' : 'm1',
  activeAnchor: 0,                      // 资料原文里选中的高亮段
  userMessages: [],
  activity: null,                       // { text, progress, sub, startedAt } | null
  cancellation: null,                   // { count, tools, at } | null
  activityOpen: false,
  approved: Object.fromEntries(F.changeGroups.map((g) => [g.id, true])),
  openTiers: { caliber: true, structural: false, index: false },
  policy: F.policy,
  confirmPolicy: [],
  packageDataSource: { dataSourceType: 'neo4j' },
  reviewMode: null,                     // full | sequential | sampled
  widths: { left: 232, right: 384 },
  drawerOpen: false,
};

export const store = createStore(initial);

const UPLOAD_POLL_INTERVAL_MS = 800;
const UPLOAD_POLL_TIMEOUT_MS = 11 * 60_000;
export const ACTIVE_RUN_STATUSES = new Set(['queued', 'running', 'stopping']);
const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const PROGRESS_RUN_STATUSES = new Set(['queued', 'running']);
const TERMINAL_TOOL_PHASES = new Set(['completed', 'failed', 'blocked', 'rejected', 'waiting', 'cancelled', 'cancel_pending']);
const ACTIVE_TOOL_PHASES = new Set(['started', 'repairing', 'cancel_pending']);
const LATE_TOOL_PHASES_AFTER_TERMINAL = new Set([
  'started', 'repairing', 'blocked', 'failed', 'rejected', 'waiting', 'cancel_pending',
]);
// 这条续跑文案由前端代发，却会被服务端当成用户意图做正则分类。原文里的
// 「需要全文精读的资料继续按已保存设置执行」既会命中全文精读意图、强行拉起
// read_documents_fully，又因为措辞含糊让模型反复推敲用户到底要什么（见 #1）。
// 改成不含「全文精读」且语义唯一的说法。
const PURPOSE_REVIEW_CONTINUATION = '我已确认本批全部资料的明确用途和表格 Sheet 处理方式。现在开始按确认结果读取资料、形成建模目标；每份资料的阅读方式一律以已保存设置为准。';
// 只读工具清单，用于决定工具完成后是否重新拉取快照。
// 刻意采用「默认刷新、只列出只读工具」而不是「只列出写包工具」：服务端新增写包工具时，
// 漏在这里最多多刷一次快照（无害）；反过来漏在白名单里会让文件树和产物计数长期停在旧值，
// 用户只能靠手动刷新页面恢复。
export const SNAPSHOT_NEUTRAL_TOOLS = new Set([
  'focus', 'notify', 'ask',
  'list_documents', 'read_document', 'read_documents_fully', 'search_documents',
  'list_tables', 'describe_table', 'read_table_rows',
  'list_dir', 'read_file', 'grep_package', 'diff',
  'get_project_state', 'gate_status', 'who_references',
  'list_domain_resources', 'get_domain_resource', 'preview_domain_delete',
  'suggest_object_relations', 'get_schema_semantics', 'get_schema_mapping_plan',
  'get_change_plan', 'get_change_set_receipt',
  'analyze_deterministic_capabilities', 'analyze_material_impact', 'analyze_package_merge',
  'list_package_sources', 'read_package_source_file',
  'get_evaluation_results', 'query_eval_db', 'release_preflight',
]);
const SNAPSHOT_REFRESH_DEBOUNCE_MS = 300;
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
let optimisticSequence = 0;
let snapshotRefreshTimer = null;
let snapshotRefreshOpenPurposeReview = false;
const optimisticId = (kind) => `optimistic-${kind}-${Date.now()}-${optimisticSequence += 1}`;
const uploadStatus = (document) => document?.parseStatus === 'pending'
  ? 'parsing'
  : document?.parseStatus || 'failed';
const mergeUploadMaterials = (materials, uploads) => [
  ...materials.filter((material) => !uploads.some((item) => item.documentId === material.id)),
  ...uploads.filter((item) => item.documentId).map((item) => ({
    id: item.documentId,
    filename: item.name,
    kind: item.kind || 'unknown',
    parseStatus: item.status === 'parsing' ? 'pending' : item.status,
    textLength: 0,
    tableCount: 0,
    purpose: item.purpose || 'auto',
    readingMode: item.readingMode || 'on_demand',
  })),
];

export function pendingMaterialPurposeReviews(materials) {
  return (materials || []).filter((item) => item.parseStatus === 'parsed'
    && item.purposeReview?.status === 'pending');
}

export function nextReadyMaterialPurposeReview(materials, currentId = '') {
  const pending = pendingMaterialPurposeReviews(materials)
    .filter((item) => item.purposeReview?.suggestedBy === 'agent');
  if (!pending.length) return null;
  const currentIndex = pending.findIndex((item) => item.id === currentId);
  return pending[currentIndex >= 0 ? (currentIndex + 1) % pending.length : 0];
}

export function packageUploadFollowUp(sources) {
  if (!sources?.length) return '';
  const names = sources.map((source) => `“${source.name}”${source.version ? ` ${source.version}` : ''}`).join('、');
  return `我一次导入了 ${sources.length} 份来源知识包：${names}。请先让我选择各来源的融合范围，识别重复身份和跨包业务概念，再主动推进一次性融合。`;
}

function progressActivity(event, previous = null) {
  const counters = event?.counters && typeof event.counters === 'object' ? event.counters : {};
  const activityId = String(event?.activityId || `${event?.phase || 'activity'}:${event?.text || ''}`);
  const elapsedSeconds = counters.elapsedSeconds === null || counters.elapsedSeconds === undefined
    ? undefined
    : Number(counters.elapsedSeconds);
  const eventTime = Date.parse(String(event?.createdAt || ''));
  const inferredStartedAt = (() => { if (Number.isFinite(elapsedSeconds)) return Date.now() - Math.max(0, elapsedSeconds) * 1000; return Number.isFinite(eventTime) ? eventTime : Date.now(); })();
  const sameActivity = previous?.id === activityId;
  return {
    id: activityId,
    text: event?.text || '助手正在处理',
    detail: event?.detail || '',
    phase: event?.phase || '',
    progress: {
      percent: counters.progress === null || counters.progress === undefined || !Number.isFinite(Number(counters.progress))
        ? undefined : Number(counters.progress),
      current: counters.current === null || counters.current === undefined || !Number.isFinite(Number(counters.current))
        ? undefined : Number(counters.current),
      total: counters.total === null || counters.total === undefined || !Number.isFinite(Number(counters.total))
        ? undefined : Number(counters.total),
      elapsedSeconds: Number.isFinite(elapsedSeconds) ? Math.max(0, elapsedSeconds) : undefined,
      unit: counters.unit || '',
    },
    sub: sameActivity ? previous.sub || [] : [],
    startedAt: sameActivity ? Math.min(previous.startedAt || inferredStartedAt, inferredStartedAt) : inferredStartedAt,
    observedAt: Number.isFinite(eventTime) ? eventTime : Date.now(),
  };
}

function rejectedMessage(result, fallback) {
  if (result.status !== 'rejected') return '';
  return result.reason instanceof Error && result.reason.message
    ? result.reason.message
    : fallback;
}

function planContinuationPrompt(approved) {
  return approved
    ? '我已在页面批准当前建模计划。请继续执行已批准的建模计划：完成候选预检，候选修订写入后执行完整校验，校验通过后生成统一确认卡；不要重复已经完成的任务。'
    : '我已在页面拒绝当前建模计划。请读取计划的最终状态并停止这份计划，不要产生任何候选修改。';
}

function planCardBlocks(plan) {
  return [{
    type: 'plan',
    plan,
    items: (plan.tasks || []).map((task) => ({
      t: task.title,
      st: ['accepted', 'skipped'].includes(task.status)
        ? 'done'
        : ['staged', 'validated', 'waiting_user'].includes(task.status) ? 'now' : '',
    })),
  }];
}

async function waitForPlanContinuationBoundary(projectId, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const agent = await getAgentState(projectId);
    const status = String(agent?.run?.status || '');
    if (!['queued', 'running', 'stopping'].includes(status)) {
      store.set({ run: agent?.run || null, statusText: agent?.statusText || '' });
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const error = new Error('建模计划已经记录，但助手仍在收尾；开工指令还没有发出。');
  // 调用方据此区分“决定没生效”和“决定已生效但没开工”，两者的出口完全不同。
  error.planSettled = true;
  throw error;
}

export function resolveWorkbenchBootstrapResults(results) {
  const [snapshotResult, agentResult, candidateResult] = results;
  if (snapshotResult.status === 'rejected') throw snapshotResult.reason;

  const warnings = [
    rejectedMessage(agentResult, '建模助手暂时不可用'),
    rejectedMessage(candidateResult, '候选修改暂时不可用'),
  ].filter(Boolean);
  return {
    snapshot: snapshotResult.value,
    agent: agentResult.status === 'fulfilled' ? agentResult.value : {},
    candidate: candidateResult.status === 'fulfilled' ? candidateResult.value : { files: [] },
    agentAvailable: agentResult.status === 'fulfilled',
    warning: warnings.join('；'),
  };
}

export function shouldUseStaleRunTermination(recovery) {
  return Boolean(recovery && ['stale_run', 'plan_unreadable'].includes(recovery.status));
}

/* ---- actions ---- */
export const actions = {
  /** 门禁是按需拉的：computeGates 要叠加整个候选工作区，不适合跟着事件流反复算。 */
  async loadProgress() {
    const { projectId, demo, progress } = store.get();
    if (demo || !projectId || progress) return progress;
    try {
      const next = await getProgress(projectId);
      store.set({ progress: next, progressError: '' });
      return next;
    } catch (error) {
      store.set({ progressError: error instanceof Error ? error.message : '门禁结果暂时读不到' });
      return null;
    }
  },

  async bootstrap(projectId) {
    store.set({ demo: false, projectId, loading: true, error: '' });
    try {
      const settled = await Promise.allSettled([
        getSnapshot(projectId),
        getAgentState(projectId),
        getCandidateDiff(projectId),
      ]);
      const { snapshot, agent, candidate, agentAvailable, warning } = resolveWorkbenchBootstrapResults(settled);
      const initialPath = snapshot.files.find((path) => path.endsWith('.mmd'))
        || snapshot.files.find((path) => path.endsWith('.md'))
        || snapshot.files[0]
        || '';
      const activity = agent.activity ? progressActivity(agent.activity) : null;
      const pendingChangePlan = snapshot.changePlan && snapshot.changePlan.approval_status === 'pending'
        ? snapshot.changePlan
        : null;
      const restoredAgentFocus = agent.focus && availableFocusTarget(agent.focus, {
        files: snapshot.files,
        fileErrors: {},
        materials: snapshot.materials,
        candidateDiff: candidate.files || [],
      }) ? agent.focus : null;
      const focus = resolveBootstrapFocus({
        current: store.get().focus,
        candidateFiles: candidate.files,
        interaction: agent.interaction,
        agentFocus: restoredAgentFocus,
        initialPath,
        routeFocus: studioFocusFromLocation(),
        workflowPhase: agent.state?.phase,
      });
      store.set({
        project: snapshot.project,
        revision: snapshot.revision,
        packageVersion: snapshot.packageVersion || null,
        packageStatus: snapshot.packageStatus || null,
        workflow: agent.state || null,
        phases: Array.isArray(agent.phases) ? agent.phases : [],
        progress: null,
        progressError: '',
        changePlan: pendingChangePlan,
        pausedPlan: snapshot.pausedPlan || null,
        files: snapshot.files,
        materials: mergeUploadMaterials(snapshot.materials, store.get().uploads),
        candidateDiff: candidate.files || [],
        studioPendingCount: Number(snapshot.studioPendingCount) || 0,
        confirmPolicy: snapshot.confirmPolicy || [],
        packageDataSource: snapshot.packageDataSource || { dataSourceType: 'neo4j' },
        selectedMaterial: snapshot.materials[0]?.id || '',
        run: agent.run,
        terminalRunIds: agent.run && TERMINAL_RUN_STATUSES.has(String(agent.run.status || ''))
          ? [String(agent.run.id || '')].filter(Boolean)
          : [],
        recovery: agent.state?.recovery || null,
        interaction: agent.interaction,
        reviewedChangeGroups: [],
        activity,
        statusText: agent.statusText || '',
        cancellation: agent.cancellation || null,
        loading: false,
        error: warning,
        selectedPath: ['flow', 'file', 'diff'].includes(focus.kind) ? focus.target : initialPath,
        focus,
        replayUntilSeq: Number(agent.lastSeq) || 0,
      });
      if (pendingChangePlan) {
        const planMessageId = `change-plan-${pendingChangePlan.id}`;
        const existingPlanMessage = store.get().messages.find((message) => message.id === planMessageId);
        const existingPlan = existingPlanMessage?.blocks?.[0]?.plan;
        if (!existingPlan
          || existingPlan.version !== pendingChangePlan.version
          || existingPlan.updated_at !== pendingChangePlan.updated_at) {
          actions.appendMessage({
            id: planMessageId,
            role: 'assistant',
            createdAt: pendingChangePlan.updated_at,
            blocks: planCardBlocks(pendingChangePlan),
          });
        }
      }
      if (initialPath) void actions.loadFile(initialPath);
      if (snapshot.materials[0]?.id) void actions.loadMaterial(snapshot.materials[0].id);
      return { snapshot, agent, agentAvailable };
    } catch (error) {
      store.set({ loading: false, error: error instanceof Error ? error.message : '工作台加载失败' });
      throw error;
    }
  },
  async refreshSnapshot(options = {}) {
    const { projectId } = store.get();
    if (!projectId) return;
    const [snapshot, candidate] = await Promise.all([
      getSnapshot(projectId),
      getCandidateDiff(projectId),
    ]);
    const materialContents = { ...store.get().materialContents };
    for (const material of snapshot.materials) {
      if (!materialContents[material.id]) continue;
      materialContents[material.id] = {
        ...materialContents[material.id],
        purpose: material.purpose,
        readingMode: material.readingMode,
        directiveRevision: material.directiveRevision,
        purposeReview: material.purposeReview,
      };
    }
    store.set({
      project: snapshot.project,
      revision: snapshot.revision,
      packageVersion: snapshot.packageVersion || null,
      packageStatus: snapshot.packageStatus || null,
      pausedPlan: snapshot.pausedPlan || null,
      files: snapshot.files,
      materials: snapshot.materials,
      materialContents,
      candidateDiff: candidate.files || [],
      studioPendingCount: Number(snapshot.studioPendingCount) || 0,
      confirmPolicy: snapshot.confirmPolicy || [],
      packageDataSource: snapshot.packageDataSource || { dataSourceType: 'neo4j' },
    });
    if (options.openPurposeReview && store.get().follow) {
      const material = nextReadyMaterialPurposeReview(snapshot.materials, store.get().selectedMaterial);
      if (material) {
        store.set({
          leftTab: 'mat',
          selectedMaterial: material.id,
          focus: { kind: 'doc', target: material.id },
          follow: false,
          pendingFocus: null,
        });
        void actions.loadMaterial(material.id);
      }
    }
  },
  // 工具完成事件可能成串到达，这里合并成一次快照拉取，并兜住失败——
  // 直接 void refreshSnapshot() 会让拒绝变成无人处理的 promise rejection，界面停在旧值且毫无提示。
  scheduleSnapshotRefresh(options = {}) {
    if (options.openPurposeReview) snapshotRefreshOpenPurposeReview = true;
    if (snapshotRefreshTimer) return;
    snapshotRefreshTimer = setTimeout(() => {
      snapshotRefreshTimer = null;
      const openPurposeReview = snapshotRefreshOpenPurposeReview;
      snapshotRefreshOpenPurposeReview = false;
      actions.refreshSnapshot({ openPurposeReview }).catch((error) => {
        store.set({ error: error instanceof Error ? error.message : '知识包内容暂时读不到，稍后会自动重试' });
      });
    }, SNAPSHOT_REFRESH_DEBOUNCE_MS);
  },
  refreshCandidateRevisions() {
    store.set((state) => ({
      candidateRevisionRefreshKey: state.candidateRevisionRefreshKey + 1,
    }));
  },
  openCandidateRevisionPreview(preview) {
    const files = Array.isArray(preview?.files) ? preview.files : [];
    const first = files[0];
    store.set({
      candidateDiff: files,
      ...(first ? {
        selectedPath: first.path,
        focus: { kind: 'diff', target: first.path },
        follow: false,
        pendingFocus: null,
      } : {}),
    });
  },
  async loadFile(path) {
    const { projectId } = store.get();
    if (!projectId || !path || path.endsWith('/')) return;
    store.set((state) => {
      if (!state.fileErrors[path]) return state;
      const fileErrors = { ...state.fileErrors };
      delete fileErrors[path];
      return { fileErrors };
    });
    try {
      const result = await getPackageFile(projectId, path);
      store.set((state) => ({
        fileContents: { ...state.fileContents, [path]: result.content },
        fileErrors: Object.fromEntries(Object.entries(state.fileErrors).filter(([key]) => key !== path)),
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : `无法读取 ${path}`;
      store.set((state) => ({ fileErrors: { ...state.fileErrors, [path]: message } }));
    }
  },
  async loadMaterial(id) {
    const { projectId } = store.get();
    if (!projectId || !id) return;
    try {
      const [material, references] = await Promise.all([
        getMaterial(projectId, id),
        getMaterialReferrers(projectId, id),
      ]);
      store.set((state) => ({
        materialContents: { ...state.materialContents, [id]: material },
        materialReferrers: { ...state.materialReferrers, [id]: references.referrers || [] },
      }));
    } catch (error) {
      store.set({ error: error instanceof Error ? error.message : '资料读取失败' });
    }
  },
  async locateEvidence(reference) {
    const { projectId } = store.get();
    if (!projectId || !reference) return false;
    try {
      const location = await getEvidenceLocation(projectId, reference);
      const material = store.get().materials.find((item) => item.id === location.documentId);
      if (!material) throw new Error('这处依据对应的资料已不在当前项目中');
      const [document, references] = await Promise.all([
        getMaterial(projectId, material.id),
        getMaterialReferrers(projectId, material.id),
      ]);
      store.set((state) => ({
        leftTab: 'mat',
        selectedMaterial: material.id,
        activeAnchor: {
          anchor: location.anchor || '',
          offset: Number(location.offset) || 0,
          excerpt: location.excerpt || '',
        },
        focus: { kind: 'doc', target: material.filename },
        follow: false,
        pendingFocus: null,
        error: '',
        materialContents: { ...state.materialContents, [material.id]: document },
        materialReferrers: { ...state.materialReferrers, [material.id]: references.referrers || [] },
      }));
      return true;
    } catch (error) {
      store.set({ error: error instanceof Error ? error.message : '暂时无法找到这处原文' });
      return false;
    }
  },
  handleEvent(event) {
    if (!event || typeof event !== 'object') return;
    const eventType = event.eventType || event.kind;
    const seq = Number(event.seq) || 0;
    if (seq && seq <= store.get().lastSeq) return;
    const replaying = Boolean(seq && seq <= store.get().replayUntilSeq);
    if (eventType === 'focus' && event.target && !replaying) {
      actions.agentFocus(event.target);
      if (event.target.kind === 'diff') actions.scheduleSnapshotRefresh();
    }
    if (eventType === 'state' && !replaying) {
      store.set((state) => ({
        workflow: event,
        progress: null,
        recovery: event.recovery
          ? { ...(state.recovery || {}), ...event.recovery }
          : state.recovery,
      }));
    }
    if (eventType === 'change_plan' && event.plan) {
      store.set((state) => replaying ? state : ({
        changePlan: event.plan.status === 'active' ? event.plan : state.changePlan,
        pausedPlan: event.plan.status === 'paused' ? event.plan : (
          state.pausedPlan?.id === event.plan.id ? null : state.pausedPlan
        ),
      }));
      actions.appendMessage({
        id: `change-plan-${event.plan.id}`,
        role: 'assistant',
        createdAt: event.createdAt,
        blocks: planCardBlocks(event.plan),
      });
    }
    if (eventType === 'change_set' && !replaying) {
      store.set({ progress: null });
      actions.scheduleSnapshotRefresh();
    }
    if (eventType === 'progress' && !replaying) {
      store.set((state) => {
        const runStatus = String(state.run?.status || '');
        const eventRunId = String(event.runId || '');
        const currentRunId = String(state.run?.id || '');
        if ((runStatus && !PROGRESS_RUN_STATUSES.has(runStatus))
          || (eventRunId && currentRunId && eventRunId !== currentRunId)) return state;
        return { activity: progressActivity(event, state.activity) };
      });
    }
    if (eventType === 'tool') {
      const createdAt = event.createdAt || new Date().toISOString();
      const line = `${timeOf(createdAt)} · ${event.summary || '处理完成'}`;
      const terminal = TERMINAL_TOOL_PHASES.has(event.phase);
      const active = ACTIVE_TOOL_PHASES.has(event.phase);
      const eventRunId = String(event.runId || '');
      const currentRunId = String(store.get().run?.id || '');
      const currentRunStatus = String(store.get().run?.status || '');
      // Historical terminal events remain auditable, but a late active event
      // must never resurrect a stopped run or overwrite a newer run's view.
      const terminalRunIds = store.get().terminalRunIds || [];
      const staleTerminalEvent = !replaying
        && eventRunId && terminalRunIds.includes(eventRunId)
        && LATE_TOOL_PHASES_AFTER_TERMINAL.has(String(event.phase || ''));
      const sameTerminalRun = !replaying
        && eventRunId && eventRunId === currentRunId
        && (currentRunStatus === 'stopping' || TERMINAL_RUN_STATUSES.has(currentRunStatus))
        && LATE_TOOL_PHASES_AFTER_TERMINAL.has(String(event.phase || ''));
      if (staleTerminalEvent || sameTerminalRun) {
        if (seq) store.set({ lastSeq: seq, ...(seq >= store.get().replayUntilSeq ? { replayUntilSeq: 0 } : {}) });
        return;
      }
      if (!replaying && active && (
        (eventRunId && terminalRunIds.includes(eventRunId))
        || (eventRunId && currentRunId && eventRunId !== currentRunId
          && ACTIVE_RUN_STATUSES.has(currentRunStatus))
        || (eventRunId && eventRunId === currentRunId && currentRunStatus === 'stopping')
        || (eventRunId && eventRunId === currentRunId && TERMINAL_RUN_STATUSES.has(currentRunStatus))
      )) {
        if (seq) store.set({ lastSeq: seq, ...(seq >= store.get().replayUntilSeq ? { replayUntilSeq: 0 } : {}) });
        return;
      }
      store.set((state) => ({
        // 刷新后的历史回放只重建轨迹，不能清空 state 接口恢复的最新活动。
        activity: (() => { if (replaying) return state.activity; return terminal
            ? null
            : (() => { if (state.activity) return { ...state.activity, sub: [line, ...(state.activity.sub || [])].slice(0, 8) }; return state.activity; })(); })(),
        trace: mergeToolTrace(state.trace, event, seq),
      }));
      const resolvedIssueIds = new Set((event.resolvedIssueIds || []).map(String).filter(Boolean));
      if (resolvedIssueIds.size) {
        store.set((state) => ({
          messages: state.messages.filter((message) => !message.blocks?.some((block) => (
            ['error', 'recovery'].includes(block.type)
              && resolvedIssueIds.has(String(block.issueId || ''))
          ))),
        }));
      }
      if (event.phase === 'repairing' && event.issueId) {
        store.set((state) => ({
          messages: state.messages.filter((message) => !message.blocks?.some((block) => (
            ['error', 'recovery'].includes(block.type)
              && String(block.issueId || '') === String(event.issueId)
          ))),
        }));
      }
      if (!replaying && event.phase === 'completed' && !SNAPSHOT_NEUTRAL_TOOLS.has(event.name)) {
        actions.scheduleSnapshotRefresh({ openPurposeReview: event.name === 'set_material_directive' });
      }
      const automaticRecovery = event.phase === 'repairing' || event.problemVisibility === 'activity';
      if (!automaticRecovery && event.code && (!replaying || event.issueId)
        && ['failed', 'blocked', 'rejected', 'waiting'].includes(event.phase)) {
        const issueId = String(event.issueId || '');
        const recoveryActions = (() => { if (Array.isArray(event.recoveryActions)) return event.recoveryActions.filter((item) => item && typeof item === 'object'); return event.recoveryAction && typeof event.recoveryAction === 'object'
            ? [event.recoveryAction]
            : (() => { if (event.repairStopped) return [{
                id: 'review_validation_findings',
                label: '重新检查并继续',
                description: '从已保存进展继续，只处理仍未解决的内容。',
              }, {
                id: 'keep_current_progress',
                label: '保留当前成果，结束本轮',
                description: '保留已经完成的候选修改，暂不继续自动修正。',
              }]; return []; })(); })();
        const recoveryProblem = event.problemVisibility === 'recovery'
          || event.phase === 'waiting'
          || event.repairStopped
          || recoveryActions.length > 0;
        if (recoveryProblem) {
          actions.appendMessage({
            id: issueId
              ? `tool-issue-${event.runId || 'run'}-${issueId}`
              : `tool-recovery-${event.runId || 'run'}-${event.toolCallId || event.name || seq}`,
            role: 'assistant',
            createdAt,
            blocks: [{
              type: 'recovery',
              issueId,
              status: event.issueStatus || event.phase,
              text: event.summary || '当前步骤没有完成，系统已保留现有进展。',
              actions: recoveryActions,
            }],
          });
        }
      }
    }
    if (eventType === 'message') {
      const eventRunId = String(event.runId || '');
      const current = store.get();
      if (!replaying && eventRunId && current.terminalRunIds?.includes(eventRunId)) {
        if (seq) store.set({ lastSeq: seq, ...(seq >= store.get().replayUntilSeq ? { replayUntilSeq: 0 } : {}) });
        return;
      }
      const terminalDocumentIds = new Set((event.blocks || [])
        .filter((block) => block.type === 'uploadResult')
        .flatMap((block) => block.details || [])
        .map((detail) => detail.documentId)
        .filter(Boolean));
      if (terminalDocumentIds.size) {
        store.set((state) => ({
          uploads: state.uploads.filter((item) => !terminalDocumentIds.has(item.documentId)),
          messages: state.messages.map((message) => ({
            ...message,
            blocks: (message.blocks || []).flatMap((block) => {
              if (block.type !== 'upload') return [block];
              const files = (block.files || []).filter((file) => !terminalDocumentIds.has(file.documentId));
              return files.length ? [{ ...block, files }] : [];
            }),
          })).filter((message) => message.blocks.length),
        }));
      }
      actions.appendMessage({ ...event, id: `event-${seq}` });
    }
    if (eventType === 'message_delta' && event.messageId && event.delta) {
      const eventRunId = String(event.runId || '');
      if (!replaying && eventRunId && store.get().terminalRunIds?.includes(eventRunId)) {
        if (seq) store.set({ lastSeq: seq, ...(seq >= store.get().replayUntilSeq ? { replayUntilSeq: 0 } : {}) });
        return;
      }
      store.set((state) => {
        const index = state.messages.findIndex((message) => message.id === event.messageId);
        if (index < 0) {
          return { messages: [...state.messages, {
            id: event.messageId,
            role: event.role || 'assistant',
            createdAt: event.createdAt,
            streaming: true,
            blocks: [{ type: 'text', text: String(event.delta) }],
          }] };
        }
        const messages = [...state.messages];
        const message = messages[index];
        const blocks = [...(message.blocks || [])];
        const textIndex = blocks.findIndex((block) => block.type === 'text');
        if (textIndex < 0) blocks.push({ type: 'text', text: String(event.delta) });
        else blocks[textIndex] = { ...blocks[textIndex], text: `${blocks[textIndex].text || ''}${event.delta}` };
        messages[index] = { ...message, streaming: true, blocks };
        return { messages };
      });
    }
    if (eventType === 'message_done' && event.messageId) {
      store.set((state) => ({
        messages: state.messages.map((message) => message.id === event.messageId
          ? { ...message, streaming: false, incomplete: Boolean(event.incomplete) }
          : message),
      }));
    }
    if (eventType === 'reasoning_delta' && event.messageId && event.delta) {
      const eventRunId = String(event.runId || '');
      if (!replaying && eventRunId && store.get().terminalRunIds?.includes(eventRunId)) {
        if (seq) store.set({ lastSeq: seq, ...(seq >= store.get().replayUntilSeq ? { replayUntilSeq: 0 } : {}) });
        return;
      }
      store.set((state) => {
        const index = state.messages.findIndex((message) => message.id === event.messageId);
        if (index < 0) {
          return { messages: [...state.messages, {
            id: event.messageId,
            role: event.role || 'assistant',
            createdAt: event.createdAt,
            blocks: [{
              type: 'reasoning',
              text: String(event.delta),
              streaming: true,
              truncated: Boolean(event.truncated),
            }],
          }] };
        }
        const messages = [...state.messages];
        const message = messages[index];
        const blocks = [...(message.blocks || [])];
        const reasoningIndex = blocks.findIndex((block) => block.type === 'reasoning');
        if (reasoningIndex < 0) {
          blocks.push({
            type: 'reasoning',
            text: String(event.delta),
            streaming: true,
            truncated: Boolean(event.truncated),
          });
        } else {
          const block = blocks[reasoningIndex];
          blocks[reasoningIndex] = {
            ...block,
            text: `${block.text || ''}${event.delta}`,
            streaming: true,
            truncated: Boolean(block.truncated || event.truncated),
          };
        }
        messages[index] = { ...message, blocks };
        return { messages };
      });
    }
    if (eventType === 'reasoning_done' && event.messageId) {
      store.set((state) => ({
        messages: state.messages.map((message) => message.id === event.messageId
          ? {
            ...message,
            blocks: (message.blocks || []).map((block) => block.type === 'reasoning'
              ? { ...block, streaming: false }
              : block),
          }
          : message),
      }));
    }
    if (eventType === 'interaction') {
      if (event.status !== 'pending') {
        store.set((state) => ({
          interaction: state.interaction?.id === event.id ? null : state.interaction,
          messages: state.messages.map((message) => message.id === `interaction-${event.id}`
            ? {
              ...message,
              blocks: message.blocks.map((block) => block.interaction
                ? { ...block, interaction: { ...block.interaction, status: event.status, answer: event.answer } }
                : block),
            }
            : message),
        }));
      } else {
        const interaction = {
          id: event.id,
          kind: event.kind || event.payload?.kind,
          payload: event.payload || {},
          status: event.status || 'pending',
        };
        const authoritativeInteractionId = String(store.get().interaction?.id || '');
        const restoreAsPending = !replaying || authoritativeInteractionId === interaction.id;
        store.set((state) => ({
          ...(restoreAsPending ? { interaction } : {}),
          reviewedChangeGroups: state.interaction?.id === interaction.id
            ? state.reviewedChangeGroups
            : [],
          ...(!replaying ? {
            activity: null,
            run: { ...(store.get().run || {}), status: 'waiting' },
          } : {}),
        }));
        actions.appendMessage({
          id: `interaction-${event.id}`,
          role: 'assistant',
          createdAt: event.createdAt,
          blocks: [{ type: (() => { if (interaction.kind === 'confirmation') return 'confirm'; return interaction.kind === 'release_confirmation' ? 'release' : 'ask'; })(),
          interaction: restoreAsPending ? interaction : { ...interaction, status: 'superseded' } }],
        });
      }
    }
    if (eventType === 'status' && !replaying) {
      const statusTime = Date.parse(String(event.createdAt || ''));
      store.set((state) => {
        const eventRunId = String(event.runId || state.run?.id || '');
        const sameRun = Boolean(eventRunId && eventRunId === String(state.run?.id || ''));
        const currentStatus = String(state.run?.status || '');
        const normalizedStatus = ['RUN_CANCELLING', 'RUN_CANCELLED'].includes(String(event.status || ''))
          ? 'cancelled'
          : String(event.status || '');
        if (eventRunId && state.run?.id && eventRunId !== String(state.run.id)
          && ACTIVE_RUN_STATUSES.has(currentStatus)) {
          return TERMINAL_RUN_STATUSES.has(normalizedStatus)
            ? {
                terminalRunIds: [...new Set([...(state.terminalRunIds || []), eventRunId])],
                trace: settleTerminalToolTrace(state.trace, eventRunId, normalizedStatus),
              }
            : state;
        }
        if (sameRun && currentStatus === 'stopping'
          && !['cancelled', 'suspended'].includes(normalizedStatus)) return state;
        if (sameRun && (
          TERMINAL_RUN_STATUSES.has(currentStatus)
          || (eventRunId && (state.terminalRunIds || []).includes(eventRunId))
        )) return state;
        if (sameRun && currentStatus === 'cancelled') return state;
        const inactive = ['waiting', 'suspended', 'completed', 'failed', 'cancelled'].includes(normalizedStatus);
        return {
          run: {
            ...(state.run || {}),
            id: eventRunId,
            status: normalizedStatus,
            updatedAt: Number.isFinite(statusTime) ? event.createdAt : null,
          },
          ...(TERMINAL_RUN_STATUSES.has(normalizedStatus) && eventRunId
            ? { terminalRunIds: [...new Set([...(state.terminalRunIds || []), eventRunId])] }
            : {}),
          statusText: normalizedStatus === 'cancelled'
            ? (event.text || '运行已停止，当前操作已取消')
            : event.text || '',
          ...(normalizedStatus === 'cancelled' || normalizedStatus === 'suspended'
            ? {
              cancellation: {
                count: Number(event.cancelledToolCount) || 0,
                tools: Array.isArray(event.cancelledTools) ? event.cancelledTools : [],
                at: event.createdAt || new Date().toISOString(),
              },
            }
            : {}),
          ...(inactive ? { activity: null } : {}),
          ...(normalizedStatus === 'cancelled'
            ? { trace: settleCancelledToolTrace(state.trace, eventRunId) }
            : {}),
        };
      });
    }
    if (seq) store.set({
      lastSeq: seq,
      ...(seq >= store.get().replayUntilSeq ? { replayUntilSeq: 0 } : {}),
    });
  },
  appendMessage(message) {
    store.set((state) => {
      const existingIndex = state.messages.findIndex((item) => item.id === message.id);
      if (existingIndex >= 0) {
        const messages = [...state.messages];
        messages[existingIndex] = message;
        return { messages };
      }
      const optimisticIndex = state.messages.findIndex((item) => item.optimistic
        && item.role === message.role
        && messageFingerprint(item) === messageFingerprint(message));
      if (optimisticIndex < 0) return { messages: [...state.messages, message] };
      const messages = [...state.messages];
      messages[optimisticIndex] = message;
      return { messages };
    });
  },
  async respondChangePlan(planId, approved) {
    const { projectId } = store.get();
    if (!projectId || !planId) return null;
    try {
      const result = await respondChangePlanRequest(projectId, planId, approved);
      store.set((state) => ({
        changePlan: result.plan,
        error: '',
        messages: state.messages.map((message) => ({
          ...message,
          blocks: message.blocks?.map((block) => (
            block.type === 'plan' && block.plan?.id === planId
              ? { ...block, plan: result.plan }
              : block
          )) ?? [],
        })),
      }));
      await waitForPlanContinuationBoundary(projectId);
      await actions.send(planContinuationPrompt(approved));
      return result;
    } catch (error) {
      const data = error?.data && typeof error.data === 'object' ? error.data : {};
      const staleRun = data.code === 'CHANGE_PLAN_STALE_RUN';
      // 计划决定本身是先落库再发开工指令的两步。等待助手空闲超时时，
      // 决定已经生效，只是指令没发出去；此时说“计划仍保持原状态”是假的，
      // 会把用户引去重新批准（见 #30）。
      const settled = Boolean(error?.planSettled);
      store.set({
        error: {
          scope: 'change_plan',
          message: error instanceof Error ? error.message : '计划决定提交失败',
          recovery: staleRun
            ? '当前运行还没有接续服务重启前的建模计划，请先安全终止失效计划后重新规划。'
            : settled
              ? '决定已经生效，只是助手当时仍在收尾、没有收到开工指令。等助手空闲后点「继续执行」即可。'
              : '计划仍保持原状态，请在当前计划卡重试。',
          ...(staleRun ? { staleRun: true } : {}),
          ...(settled ? { resumePlan: { planId, approved } } : {}),
        },
        ...(staleRun ? {
          recovery: {
            status: 'stale_run',
            missing: ['run'],
            nextSteps: Array.isArray(data.next) ? data.next : ['安全终止失效计划后重新规划'],
          },
        } : {}),
      });
      return null;
    }
  },
  /** 计划决定已落库、只差开工指令时的补发出口（见 #30）。 */
  async resumeChangePlan() {
    const { projectId, error } = store.get();
    const resume = error?.resumePlan;
    if (!projectId || !resume?.planId) return null;
    try {
      await waitForPlanContinuationBoundary(projectId);
      await actions.send(planContinuationPrompt(resume.approved));
      store.set({ error: '' });
      return true;
    } catch (cause) {
      store.set({
        error: {
          scope: 'change_plan',
          message: cause instanceof Error ? cause.message : '开工指令补发失败',
          recovery: '助手仍在收尾，稍候再点「继续执行」。',
          resumePlan: resume,
        },
      });
      return null;
    }
  },
  async safeTerminateChangePlan(planId) {
    const { projectId, recovery } = store.get();
    if (!projectId || !planId) return null;
    try {
      const result = shouldUseStaleRunTermination(recovery)
        ? await terminateStaleRunRequest(projectId)
        : await safeTerminateChangePlanRequest(projectId, planId);
      store.set((state) => ({
        changePlan: null,
        error: '',
        recovery: null,
        messages: state.messages.map((message) => ({
          ...message,
          blocks: (message.blocks || []).filter((block) => !(block.type === 'plan' && block.plan?.id === planId)),
        })),
      }));
      await actions.refreshAgentState();
      return result;
    } catch (error) {
      store.set({ error: {
        scope: 'change_plan',
        message: error instanceof Error ? error.message : '安全终止失效计划失败，请重试',
        recovery: '候选内容与审计记录不会被删除，可以再次尝试。',
      } });
      return null;
    }
  },
  async resumePausedPlan(planId) {
    const { projectId, pausedPlan } = store.get();
    if (!projectId || !planId || pausedPlan?.id !== planId) return null;
    try {
      const result = await resumeAgentPlan(projectId, planId);
      store.set({
        run: result.run || null,
        pausedPlan: null,
        changePlan: { ...pausedPlan, status: 'active' },
        activity: null,
        statusText: '正在从停止点继续未完成计划',
        error: '',
      });
      await actions.refreshSnapshot();
      return result;
    } catch (error) {
      await actions.refreshSnapshot().catch(() => undefined);
      store.set({ error: {
        scope: 'change_plan',
        message: error instanceof Error ? error.message : '未完成计划恢复失败',
        recovery: '计划与候选内容仍然保留，请刷新状态后重试。',
      } });
      return null;
    }
  },
  async endPausedPlan(planId) {
    const { projectId, pausedPlan } = store.get();
    if (!projectId || !planId || pausedPlan?.id !== planId) return null;
    try {
      const result = await safeTerminateChangePlanRequest(projectId, planId);
      store.set({
        pausedPlan: null,
        changePlan: null,
        statusText: '已结束暂停计划，候选内容和审计记录保留，可以重新规划',
        error: '',
      });
      await actions.refreshSnapshot();
      return result;
    } catch (error) {
      store.set({ error: {
        scope: 'change_plan',
        message: error instanceof Error ? error.message : '暂停计划结束失败',
        recovery: '计划仍保持暂停状态，请刷新后重试。',
      } });
      return null;
    }
  },
  async terminateStaleRun() {
    const { projectId } = store.get();
    if (!projectId) return null;
    try {
      const result = await terminateStaleRunRequest(projectId);
      store.set({
        run: null,
        changePlan: null,
        error: '',
        recovery: null,
        statusText: '已安全终止失效运行，候选内容与审计记录保留，可以重新规划',
      });
      await actions.refreshAgentState();
      return result;
    } catch (error) {
      store.set({ error: {
        scope: 'change_plan',
        message: error instanceof Error ? error.message : '安全终止失效运行失败，请重试',
        recovery: '候选内容与审计记录不会被删除，可以再次尝试。',
      } });
      return null;
    }
  },
  async refreshAgentState() {
    const { projectId } = store.get();
    if (!projectId) return null;
    try {
      const agent = await getAgentState(projectId);
      store.set({
        run: agent.run,
        terminalRunIds: agent.run && TERMINAL_RUN_STATUSES.has(String(agent.run.status || ''))
          ? [...new Set([...(store.get().terminalRunIds || []), String(agent.run.id || '')].filter(Boolean))]
          : store.get().terminalRunIds,
        interaction: agent.interaction,
        workflow: agent.state || null,
        recovery: agent.state?.recovery || null,
        activity: agent.activity
          ? progressActivity(agent.activity, store.get().activity)
          : (agent.run && TERMINAL_RUN_STATUSES.has(String(agent.run.status || '')) ? null : store.get().activity),
        statusText: agent.statusText || store.get().statusText,
        cancellation: agent.cancellation || store.get().cancellation || null,
        trace: agent.run && TERMINAL_RUN_STATUSES.has(String(agent.run.status || ''))
          ? settleTerminalToolTrace(store.get().trace, agent.run.id, agent.run.status)
          : store.get().trace,
        lastSeq: Number(agent.lastSeq) || 0,
      });
      return agent;
    } catch (error) {
      return null;
    }
  },
  async send(text, options = {}) {
    const value = String(text || '').trim();
    const { projectId } = store.get();
    if (!projectId || !value) return;
    const messageId = optimisticId('message');
    actions.appendMessage({
      id: messageId,
      role: 'user',
      createdAt: new Date().toISOString(),
      optimistic: true,
      blocks: [{ type: 'text', text: value }],
    });
    try {
      const result = await steerAgent(projectId, value, options);
      if (result.run) store.set({ run: result.run, error: '' });
      return result;
    } catch (error) {
      store.set((state) => ({
        messages: state.messages.map((message) => message.id === messageId
          ? { ...message, optimistic: false, sendFailed: true }
          : message),
        error: {
          scope: 'send',
          message: error instanceof Error ? error.message : '消息发送失败',
          recovery: '输入内容仍保留在发送框中，请检查后重试。',
        },
      }));
      return null;
    }
  },
  async startIncrementRound() {
    const { projectId } = store.get();
    if (!projectId) return null;
    try {
      const result = await startAgentIncrementRound(projectId);
      store.set({
        workflow: result.state || store.get().workflow,
        error: '',
      });
      return result;
    } catch (error) {
      store.set({
        error: {
          scope: 'increment_round',
          message: error instanceof Error ? error.message : '增量建模启动失败',
          recovery: '当前正式版本保持不变，请处理页面提示后重试。',
        },
      });
      return null;
    }
  },
  async resumeIssue(issueId, actionId) {
    const { projectId } = store.get();
    if (!projectId || !issueId || !actionId) return null;
    try {
      const result = await resumeAgentIssue(projectId, issueId, actionId);
      if (result.run) store.set({ run: result.run, error: '' });
      store.set((state) => ({
        messages: state.messages.filter((message) => !message.blocks?.some((block) => (
          ['error', 'recovery'].includes(block.type)
            && String(block.issueId || '') === String(issueId)
        ))),
      }));
      return result;
    } catch (error) {
      store.set({ error: {
        scope: 'issue_recovery',
        message: error instanceof Error ? error.message : '当前问题恢复失败',
        recovery: '问题状态可能已经变化，请刷新后查看当前有效状态。',
      } });
      return null;
    }
  },
  async answer(interactionId, answer) {
    const { projectId, interaction } = store.get();
    if (!projectId || !interactionId) return;
    const submittedAnswer = enrichInteractionAnswer(interaction, answer);
    const messageId = optimisticId('answer');
    store.set((state) => ({
      interaction: null,
      messages: state.messages.map((message) => message.id === `interaction-${interactionId}`
        ? {
            ...message,
            blocks: message.blocks.map((block) => block.interaction
            ? { ...block, interaction: { ...block.interaction, status: 'answered', answer: submittedAnswer } }
            : block),
        }
        : message).concat({
          id: messageId,
          role: 'user',
          createdAt: new Date().toISOString(),
          optimistic: true,
          blocks: [{ type: 'answer', interactionId, answer: submittedAnswer }],
        }),
    }));
    try {
      const result = await answerAgent(projectId, interactionId, submittedAnswer);
      store.set({ run: result.run, error: '' });
      if (interaction?.kind === 'confirmation') {
        const { refreshError } = await refreshWorkbenchAfterConfirmation({
          refreshSnapshot: () => actions.refreshSnapshot(),
        });
        if (refreshError) {
          store.set({
            error: '确认决定已处理，但页面状态刷新失败；请使用顶部待确认入口重新载入',
          });
        }
      }
      return result;
    } catch (error) {
      const failure = error instanceof Error ? error.message : '提交失败，请重试';
      const restoredInteraction = interaction?.kind === 'confirmation'
        ? {
            ...interaction,
            payload: { ...(interaction.payload || {}), lastSubmissionError: failure },
          }
        : interaction;
      store.set((state) => ({
        interaction: state.interaction || restoredInteraction,
        messages: state.messages
          .filter((message) => message.id !== messageId)
          .map((message) => message.id === `interaction-${interactionId}`
            ? {
              ...message,
              blocks: message.blocks.map((block) => block.interaction
                ? {
                    ...block,
                    interaction: {
                      ...block.interaction,
                      status: 'pending',
                      answer: undefined,
                      ...(interaction?.kind === 'confirmation' ? {
                        payload: { ...(block.interaction.payload || {}), lastSubmissionError: failure },
                      } : {}),
                    },
                  }
                : block),
            }
            : message),
        error: interaction?.kind === 'confirmation' ? '' : failure,
      }));
      return null;
    }
  },
  async setPackageDataSource(dataSourceType) {
    const { projectId, packageVersion } = store.get();
    if (!projectId) return null;
    const result = await savePackageDataSource(projectId, dataSourceType);
    store.set({ packageDataSource: result.packageDataSource || { dataSourceType }, error: '' });
    if (result.requiresRegeneration && result.message && packageVersion) {
      actions.appendMessage({
        id: optimisticId('package-data-source-note'),
        role: 'assistant',
        createdAt: new Date().toISOString(),
        blocks: [{ type: 'text', text: result.message }],
      });
    }
    return result;
  },
  async upload(filesLike, options = {}) {
    const { projectId } = store.get();
    const files = [...(filesLike || [])].filter((file) => file instanceof File);
    if (!projectId || !files.length) return;
    const packageFiles = files.filter((file) => file.name.toLocaleLowerCase('zh-CN').endsWith('.zip'));
    const materialFiles = files.filter((file) => !packageFiles.includes(file));
    const packageResults = [];
    for (const file of packageFiles) {
      const result = await actions.uploadPackage(file, { followUp: false });
      if (result?.source) packageResults.push(result.source);
    }
    if (packageResults.length && options.followUp !== false) {
      void actions.send(packageUploadFollowUp(packageResults));
    }
    if (!materialFiles.length) return {
      parsed: packageResults.length,
      failed: packageFiles.length - packageResults.length,
      details: packageResults,
    };
    const uploadBatchId = globalThis.crypto?.randomUUID?.()
      || `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const batch = materialFiles.map((file) => {
      const directives = options.directivesByName?.[file.name] || {};
      return {
        id: `${Date.now()}-${file.name}`, name: file.name, status: 'uploading',
        purpose: directives.purpose || options.purpose || 'auto',
        readingMode: directives.readingMode || options.readingMode || 'on_demand',
      };
    });
    store.set((state) => ({ uploads: [...state.uploads, ...batch] }));
    const uploadMessageId = `upload-${batch[0].id}`;
    actions.appendMessage({
      id: uploadMessageId,
      role: 'system',
      createdAt: new Date().toISOString(),
      blocks: [{ type: 'upload', files: batch }],
    });
    const publishBatch = () => store.set((state) => ({
      uploads: state.uploads.map((item) => {
        const detail = batch.find((candidate) => candidate.id === item.id);
        return detail ? { ...item, ...detail } : item;
      }),
      materials: mergeUploadMaterials(state.materials, batch),
      messages: state.messages.map((message) => message.id === uploadMessageId
        ? { ...message, blocks: [{ type: 'upload', files: [...batch] }] }
        : message),
    }));
    for (const [index, file] of materialFiles.entries()) {
      let result;
      try {
        result = { status: 'fulfilled', value: await uploadMaterial(projectId, file, options.replacementOf, {
          purpose: batch[index].purpose,
          readingMode: batch[index].readingMode,
          autoContinue: options.followUp !== false,
          uploadBatchId,
          uploadBatchSize: materialFiles.length,
        }) };
      } catch (reason) {
        result = { status: 'rejected', reason };
      }
      batch[index] = {
        ...batch[index],
        status: result.status === 'fulfilled' ? uploadStatus(result.value?.document) : 'failed',
        documentId: result.status === 'fulfilled' ? result.value?.document?.id : undefined,
        kind: result.status === 'fulfilled' ? result.value?.document?.kind : undefined,
        purpose: result.status === 'fulfilled' ? result.value?.document?.purpose : batch[index].purpose,
        readingMode: result.status === 'fulfilled' ? result.value?.document?.readingMode : batch[index].readingMode,
        error: result.status === 'rejected' ? result.reason?.message : result.value?.document?.parseError,
      };
      publishBatch();
    }

    const deadline = Date.now() + UPLOAD_POLL_TIMEOUT_MS;
    while (batch.some((item) => item.status === 'parsing') && Date.now() < deadline) {
      try {
        const response = await getMaterialRevisions(projectId);
        for (const item of batch) {
          if (item.status !== 'parsing' || !item.documentId) continue;
          const document = response.revisions?.find((candidate) => candidate.id === item.documentId);
          if (!document || document.parseStatus === 'pending') continue;
          item.status = uploadStatus(document);
          item.error = document.parseError;
        }
        publishBatch();
      } catch {
        // 网络短暂中断时保留“后台解析中”；下一轮继续查询同一资料修订。
      }
      if (batch.some((item) => item.status === 'parsing')) await wait(UPLOAD_POLL_INTERVAL_MS);
    }
    for (const item of batch) {
      if (item.status !== 'parsing') continue;
      item.status = 'failed';
      item.error = '长时间未收到解析结果，请刷新项目查看后台结果';
    }
    publishBatch();

    const parsed = batch.filter((item) => item.status === 'parsed').length;
    const failed = batch.length - parsed;
    const details = batch.map((item) => ({ ...item }));
    await actions.refreshSnapshot().catch((error) => {
      store.set({ error: error instanceof Error ? error.message : '资料列表刷新失败' });
    });
    return {
      parsed: parsed + packageResults.length,
      failed: failed + packageFiles.length - packageResults.length,
      details: [...packageResults, ...details],
    };
  },
  async setMaterialDirectives(documentId, patch) {
    const { projectId, materials, uploads, materialContents, messages } = store.get();
    if (!projectId || !documentId) return null;
    const current = materials.find((item) => item.id === documentId)
      || uploads.find((item) => item.documentId === documentId);
    if (!current) return null;
    const next = {
      purpose: patch.purpose || current.purpose || 'auto',
      readingMode: patch.readingMode || current.readingMode || 'on_demand',
      directiveRevision: Number(current.directiveRevision || 0) + 1,
    };
    const apply = (item) => item.id === documentId || item.documentId === documentId ? { ...item, ...next } : item;
    store.set((state) => ({
      materials: state.materials.map(apply),
      uploads: state.uploads.map(apply),
      materialContents: state.materialContents[documentId]
        ? { ...state.materialContents, [documentId]: { ...state.materialContents[documentId], ...next } }
        : state.materialContents,
      messages: state.messages.map((message) => ({
        ...message,
        blocks: (message.blocks || []).map((block) => block.type === 'upload'
          ? { ...block, files: (block.files || []).map(apply) }
          : block),
      })),
    }));
    try {
      const result = await updateMaterialDirectives(projectId, documentId, {
        ...(Object.hasOwn(patch, 'purpose') ? { purpose: next.purpose } : {}),
        ...(Object.hasOwn(patch, 'readingMode') ? { readingMode: next.readingMode } : {}),
        expectedDirectiveRevision: Number(current.directiveRevision || 0),
      });
      const confirmed = { ...result.document, purposeReview: result.purposeReview };
      store.set((state) => ({
        materials: state.materials.map((item) => item.id === documentId ? { ...item, ...confirmed } : item),
        materialContents: state.materialContents[documentId]
          ? { ...state.materialContents, [documentId]: { ...state.materialContents[documentId], ...confirmed } }
          : state.materialContents,
      }));
      const existingReviewPending = current.purposeReview?.status === 'pending';
      if (current.parseStatus === 'parsed' && !existingReviewPending) {
        const request = next.readingMode === 'full'
          ? `请全文精读《${current.filename || current.name}》，并按“${materialPurposeLabel(next.purpose)}”使用；完成后主动更新受影响的建模阶段。`
          : `请按需读取《${current.filename || current.name}》，并按“${materialPurposeLabel(next.purpose)}”使用后主动继续。`;
        void actions.send(request);
      }
      return result.document;
    } catch (error) {
      store.set({ materials, uploads, materialContents, messages, error: error instanceof Error ? error.message : '资料设置保存失败' });
      return null;
    }
  },
  async confirmMaterialPurposeReview(documentId, purpose, sheetDecisions) {
    const { projectId, materials, uploads, materialContents, messages } = store.get();
    if (!projectId || !documentId) return null;
    const current = materialContents[documentId]
      || materials.find((item) => item.id === documentId)
      || uploads.find((item) => item.documentId === documentId);
    if (!current) return null;
    try {
      const result = await confirmMaterialPurposeReviewRequest(projectId, documentId, {
        purpose,
        sheetDecisions,
        expectedDirectiveRevision: Number(current.directiveRevision || 0),
      });
      const confirmed = { ...result.document, purposeReview: result.purposeReview };
      const apply = (item) => item.id === documentId || item.documentId === documentId
        ? { ...item, ...confirmed }
        : item;
      store.set((state) => ({
        materials: state.materials.map(apply),
        uploads: state.uploads.map(apply),
        materialContents: state.materialContents[documentId]
          ? { ...state.materialContents, [documentId]: { ...state.materialContents[documentId], ...confirmed } }
          : state.materialContents,
        messages: state.messages.map((message) => ({
          ...message,
          blocks: (message.blocks || []).map((block) => block.type === 'upload'
            ? { ...block, files: (block.files || []).map(apply) }
            : block),
        })),
        error: '',
      }));
      const progress = result.purposeReviewProgress;
      const remaining = pendingMaterialPurposeReviews(store.get().materials);
      if (progress?.allConfirmed !== true) {
        const pendingCount = Number.isSafeInteger(progress?.pendingCount)
          ? progress.pendingCount
          : remaining.length;
        store.set({ statusText: `资料用途已确认，尚有 ${pendingCount} 份待确认` });
        const next = nextReadyMaterialPurposeReview(remaining, documentId);
        if (next) actions.selectMaterial(next.id);
      } else {
        store.set({ statusText: '全部资料用途已确认' });
        void actions.send(
          PURPOSE_REVIEW_CONTINUATION,
          { purposeReviewCompleted: true },
        );
      }
      return confirmed;
    } catch (error) {
      store.set({
        materials,
        uploads,
        materialContents,
        messages,
        error: error instanceof Error ? error.message : '资料用途确认失败',
      });
      return null;
    }
  },
  async confirmAllMaterialPurposeReviews() {
    const pending = pendingMaterialPurposeReviews(store.get().materials);
    if (!pending.length) return null;
    if (pending.some((item) => item.purposeReview?.suggestedBy !== 'agent')) {
      store.set({ error: '仍有资料用途正在分析，请等待助手完成建议' });
      return null;
    }
    const { projectId } = store.get();
    if (!projectId) return null;
    try {
      const result = await confirmMaterialPurposeReviewsRequest(projectId, pending.map((item) => ({
        documentId: item.id,
        expectedDirectiveRevision: Number(item.directiveRevision || 0),
      })));
      const confirmedById = new Map((result.documents || []).map((item) => [
        item.document.id,
        { ...item.document, purposeReview: item.purposeReview },
      ]));
      const apply = (item) => {
        const confirmed = confirmedById.get(item.id || item.documentId);
        return confirmed ? { ...item, ...confirmed } : item;
      };
      store.set((state) => ({
        materials: state.materials.map(apply),
        uploads: state.uploads.map(apply),
        materialContents: Object.fromEntries(Object.entries(state.materialContents)
          .map(([id, item]) => [id, apply(item)])),
        messages: state.messages.map((message) => ({
          ...message,
          blocks: (message.blocks || []).map((block) => block.type === 'upload'
            ? { ...block, files: (block.files || []).map(apply) }
            : block),
        })),
        error: '',
      }));
      if (result.purposeReviewProgress?.allConfirmed === true) {
        store.set({ statusText: '全部资料用途已确认' });
        void actions.send(PURPOSE_REVIEW_CONTINUATION, { purposeReviewCompleted: true });
      } else {
        store.set({ statusText: `资料用途已确认，尚有 ${result.purposeReviewProgress?.pendingCount || 0} 份待确认` });
      }
      return result;
    } catch (error) {
      store.set({ error: error instanceof Error ? error.message : '资料用途批量确认失败' });
      return null;
    }
  },
  async cancel() {
    const { projectId, run, activity } = store.get();
    if (!projectId || !run || !['queued', 'running', 'waiting', 'suspended'].includes(run.status)) return null;
    store.set({
      run: { ...run, status: 'stopping' },
      activity: activity ? { ...activity, detail: '正在停止并核对已发出的修改，请稍候' } : activity,
      statusText: '正在停止并核对已完成内容',
      error: '',
    });
    try {
      const result = await cancelAgent(projectId);
      const responseRun = result?.run || null;
      const staleTerminated = result?.terminatedStale === true;
      const terminal = responseRun?.status === 'cancelled';
      const suspended = responseRun?.status === 'suspended';
      store.set((state) => ({
        run: staleTerminated
          ? null
          : responseRun || { ...run, status: suspended ? 'suspended' : 'stopping' },
        terminalRunIds: terminal
          ? [...new Set([...(state.terminalRunIds || []), String(run.id || '')].filter(Boolean))]
          : state.terminalRunIds,
        activity: staleTerminated || terminal || suspended ? null : state.activity,
        statusText: staleTerminated
          ? '已安全终止失效运行，候选内容与审计记录保留'
          : terminal
          ? '已停止，已完成内容保留'
          : suspended
            ? '停止请求已记录，仍有在途修改需要核对'
            : '正在核对停止结果',
        cancellation: result?.cancellation || state.cancellation || null,
        trace: terminal
          ? settleCancelledToolTrace(state.trace, run.id)
          : state.trace,
      }));
      if (!staleTerminated && !terminal && !suspended) {
        await actions.waitForCancellation(run.id);
      }
      await actions.refreshSnapshot().catch(() => undefined);
      return result;
    } catch (error) {
      await actions.refreshAgentState();
      store.set({ error: error instanceof Error ? error.message : '停止运行失败，请重试' });
      return null;
    }
  },
  async waitForCancellation(runId, timeoutMs = 20_000) {
    const { projectId } = store.get();
    if (!projectId) return null;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const agent = await getAgentState(projectId);
      const status = String(agent?.run?.status || '');
      if (status === 'cancelled' || status === 'suspended' || !['queued', 'running', 'stopping'].includes(status)) {
        store.set((state) => ({
          run: agent.run || state.run,
          terminalRunIds: status === 'cancelled'
            ? [...new Set([...(state.terminalRunIds || []), String(runId || '')].filter(Boolean))]
            : state.terminalRunIds,
          interaction: agent.interaction,
          workflow: agent.state || state.workflow,
          recovery: agent.state?.recovery || state.recovery,
          activity: status === 'cancelled' || status === 'suspended' ? null : (
            agent.activity ? progressActivity(agent.activity, state.activity) : state.activity
          ),
          statusText: agent.statusText || state.statusText,
          ...(TERMINAL_RUN_STATUSES.has(status)
            ? { trace: settleTerminalToolTrace(state.trace, runId, status) }
            : {}),
          lastSeq: Number(agent.lastSeq) || state.lastSeq,
        }));
        return agent;
      }
      await wait(100);
    }
    return null;
  },
  async uploadPackage(file, options = {}) {
    const { projectId } = store.get();
    if (!projectId || !(file instanceof File)) return null;
    const item = { id: `${Date.now()}-${file.name}`, name: file.name, status: 'uploading', kind: 'package' };
    const messageId = `upload-${item.id}`;
    actions.appendMessage({
      id: messageId,
      role: 'system',
      createdAt: new Date().toISOString(),
      blocks: [{ type: 'upload', files: [item] }],
    });
    try {
      const result = await uploadPackageSource(projectId, file);
      item.status = 'parsed';
      item.packageName = result.source?.name;
      store.set((state) => ({
        messages: state.messages.map((message) => message.id === messageId
          ? { ...message, blocks: [{ type: 'upload', files: [{ ...item }] }] }
          : message),
      }));
      if (options.followUp !== false) {
        void actions.send(`我导入了来源知识包“${result.source?.name || file.name}”${result.source?.version ? ` ${result.source.version}` : ''}。请先让我选择融合范围，再计算冲突并主动继续。`);
      }
      return result;
    } catch (error) {
      item.status = 'failed';
      item.error = error instanceof Error ? error.message : '来源知识包解析失败';
      store.set((state) => ({
        messages: state.messages.map((message) => message.id === messageId
          ? { ...message, blocks: [{ type: 'upload', files: [{ ...item }] }] }
          : message),
        error: item.error,
      }));
      return null;
    }
  },
  setScene(id) {
    const s = SCENES.find((x) => x.id === id);
    if (!s) return;
    store.set({ scene: id, leftTab: s.leftTab, follow: true, pendingFocus: null, userMessages: [],
      focus: { ...s.focus, target: focusTargetFor(s.focus.kind), node: id === 'modeling' ? 'D2' : undefined } });
  },
  setLeftTab: (leftTab) => store.set({ leftTab }),
  setFollow(follow) {
    store.set((s) => (follow && s.pendingFocus
      ? { follow, focus: s.pendingFocus, pendingFocus: null }
      : { follow }));
  },
  openEvaluation() {
    store.set({ focus: { kind: 'evaluation', target: '正式评测' }, follow: false, pendingFocus: null });
  },

  /** 助手移动焦点。follow=false 时不抢用户的浏览位置，只攒起来提示。 */
  agentFocus(focus) {
    const current = store.get();
    const visibleFocus = resolveProjectFocusForPhase(focus, current.workflow?.phase);
    if (!visibleFocus || !availableFocusTarget(visibleFocus, current)) return;
    const agentTarget = { ...visibleFocus, source: 'agent' };
    const material = visibleFocus.kind === 'doc'
      ? current.materials.find((item) => item.id === visibleFocus.target || item.filename === visibleFocus.target)
      : null;
    store.set((s) => (s.follow
      ? { focus: agentTarget, pendingFocus: null, ...(material ? { selectedMaterial: material.id } : {}) }
      : { pendingFocus: agentTarget }));
    if (visibleFocus.target && ['flow', 'file'].includes(visibleFocus.kind)) void actions.loadFile(visibleFocus.target);
    if (material) void actions.loadMaterial(material.id);
  },

  /** 用户点消息上的定位入口：跳过去并重新开始跟随。 */
  focusFrom(focus) {
    const current = store.get();
    const visibleFocus = resolveProjectFocusForPhase(focus, current.workflow?.phase);
    if (!visibleFocus || !availableFocusTarget(visibleFocus, current)) return;
    store.set({ focus: { ...visibleFocus, source: 'agent' }, follow: true, pendingFocus: null });
    if (visibleFocus.target && ['flow', 'file'].includes(visibleFocus.kind)) void actions.loadFile(visibleFocus.target);
  },
  selectPath(path) {
    store.set({ selectedPath: path, follow: false, pendingFocus: null, focus: focusForPath(path) });
    void actions.loadFile(path);
  },
  reviewChangeGroup(groupId, path = '') {
    store.set((state) => ({
      reviewedChangeGroups: state.reviewedChangeGroups.includes(groupId)
        ? state.reviewedChangeGroups
        : [...state.reviewedChangeGroups, groupId],
      ...(path ? {
        selectedPath: path,
        focus: { kind: 'diff', target: path },
        follow: false,
        pendingFocus: null,
      } : {}),
    }));
  },
  selectMaterial(id) {
    store.set({ selectedMaterial: id, activeAnchor: 0, focus: { kind: 'doc', target: id }, follow: false });
    void actions.loadMaterial(id);
  },
  setAnchor: (i) => store.set({ activeAnchor: i }),
  toggleTier: (tier) => store.set((s) => ({ openTiers: { ...s.openTiers, [tier]: !s.openTiers[tier] } })),
  toggleGroup: (id) => store.set((s) => ({ approved: { ...s.approved, [id]: !s.approved[id] } })),
  setTierApproved(tier, on) {
    store.set((s) => {
      const next = { ...s.approved };
      F.changeGroups.filter((g) => g.tier === tier).forEach((g) => { next[g.id] = on; });
      return { approved: next };
    });
  },
  setPolicy(tier, mode) {
    store.set((s) => ({ policy: s.policy.map((p) => (p.tier === tier && !p.locked ? { ...p, mode } : p)) }));
  },
  async setConfirmPolicy(tier, mode) {
    const { projectId } = store.get();
    if (!projectId) return;
    const result = await updateConfirmPolicy(projectId, tier, mode);
    store.set({ confirmPolicy: result.policy || [] });
  },
  setReviewMode: (reviewMode) => store.set({ reviewMode }),
  setActivity: (activity) => store.set({ activity }),
  clearError: () => store.set({ error: '' }),
  toggleActivity: () => store.set((s) => ({ activityOpen: !s.activityOpen })),
  setWidth(side, px) { store.set((s) => ({ widths: { ...s.widths, [side]: px } })); },
  toggleDrawer: () => store.set((s) => ({ drawerOpen: !s.drawerOpen })),
  pushMessage(msg) { store.set((s) => ({ userMessages: [...s.userMessages, { id: `u${s.userMessages.length + 1}`, ...msg }] })); },
};

export function mergeToolTrace(trace, event, seq) {
  const toolCallId = String(event.toolCallId || '');
  const createdAt = event.createdAt || new Date().toISOString();
  const legacySubject = event.name === 'read_document'
    ? String(event.summary || '').match(/《([^》]+)》/u)?.[1] || ''
    : '';
  const legacyStableRead = event.name === 'get_evaluation_results'
    ? `legacy:${event.name}`
    : '';
  const traceGroupId = String(event.traceGroupId || (
    legacySubject ? `legacy:${event.name}:${legacySubject}` : legacyStableRead
  ));
  const id = (() => { if (traceGroupId) return `tool-group:${event.runId || ''}:${traceGroupId}`; return toolCallId
    ? `tool:${event.runId || ''}:${toolCallId}`
    : seq || `${Date.now()}-${event.name}`; })();
  const entry = {
    id,
    seq,
    runId: event.runId || '',
    toolCallId,
    mutationId: String(event.mutationId || ''),
    createdAt,
    at: timeOf(createdAt),
    name: event.name,
    phase: event.phase,
    summary: event.summary,
    code: event.code || '',
    retryable: Boolean(event.retryable),
    repairStopped: Boolean(event.repairStopped),
    repairAttempt: event.repairAttempt ?? null,
    repairRoundId: event.repairRoundId || '',
    findingCount: Number(event.findingCount) || 0,
    issueId: event.issueId || '',
    issueStatus: event.issueStatus || '',
    problemVisibility: event.problemVisibility || '',
    recoveryActions: event.recoveryActions || [],
    resolvedIssueIds: event.resolvedIssueIds || [],
    cancelledAt: event.cancelledAt || '',
    segmentCount: 1,
    toolCallIds: toolCallId ? [toolCallId] : [],
  };
  if (!toolCallId && !traceGroupId) return [...trace, entry].slice(-300);
  const index = trace.findIndex((item) => item.id === id);
  if (index < 0) {
    const legacyIndex = legacyRejectedTraceIndex(trace, event, seq, createdAt);
    if (legacyIndex < 0) return [...trace, entry].slice(-300);
    const merged = [...trace];
    merged[legacyIndex] = {
      ...merged[legacyIndex],
      ...entry,
      phase: 'rejected',
      seq: merged[legacyIndex].seq || entry.seq,
      createdAt: merged[legacyIndex].createdAt || entry.createdAt,
      at: merged[legacyIndex].at || entry.at,
    };
    return merged.slice(-300);
  }
  const previousPhase = String(trace[index]?.phase || '');
  const nextPhase = String(event.phase || '');
  // Tool state is monotonic. A delayed started/repairing frame cannot reopen
  // an activity whose terminal receipt was already observed.
  if (TERMINAL_TOOL_PHASES.has(previousPhase) && ACTIVE_TOOL_PHASES.has(nextPhase)) {
    return trace;
  }
  if (previousPhase === 'completed' && nextPhase !== 'completed') {
    return trace;
  }
  if (previousPhase === 'cancelled' && !['cancelled', 'completed'].includes(nextPhase)) {
    return trace;
  }
  const merged = [...trace];
  const previousCallIds = merged[index].toolCallIds || [];
  const nextCallIds = toolCallId && !previousCallIds.includes(toolCallId)
    ? [...previousCallIds, toolCallId]
    : previousCallIds;
  merged[index] = {
    ...merged[index],
    ...entry,
    seq: merged[index].seq || entry.seq,
    createdAt: merged[index].createdAt || entry.createdAt,
    at: merged[index].at || entry.at,
    segmentCount: Math.max(1, nextCallIds.length || merged[index].segmentCount || 1),
    toolCallIds: nextCallIds,
    cancelledAt: event.cancelledAt || merged[index].cancelledAt || '',
  };
  return merged.slice(-300);
}

export function settleCancelledToolTrace(trace, runId) {
  return settleTerminalToolTrace(trace, runId, 'cancelled');
}

export function settleTerminalToolTrace(trace, runId, status) {
  const targetRunId = String(runId || '');
  const terminal = TERMINAL_RUN_STATUSES.has(String(status || ''))
    ? String(status)
    : 'cancelled';
  const summary = terminal === 'cancelled'
    ? '运行已停止，当前操作已取消'
    : terminal === 'completed'
      ? '运行已结束，当前操作已完成'
      : '运行已结束，当前操作未完成';
  return (Array.isArray(trace) ? trace : []).map((item) => (
    String(item?.runId || '') === targetRunId
      && ACTIVE_TOOL_PHASES.has(String(item?.phase || ''))
      ? { ...item, phase: terminal, summary, cancelledAt: terminal === 'cancelled' ? (item.cancelledAt || new Date().toISOString()) : item.cancelledAt }
      : item
  ));
}

function legacyRejectedTraceIndex(trace, event, seq, createdAt) {
  if (event.phase !== 'failed' || !event.toolCallId || !event.runId || !seq) return -1;
  const eventTime = Date.parse(createdAt);
  for (let index = trace.length - 1; index >= 0; index -= 1) {
    const item = trace[index];
    if (item.phase !== 'rejected' || item.toolCallId) continue;
    if (item.runId !== event.runId || item.name !== event.name || item.summary !== event.summary) continue;
    if (Number(seq) - Number(item.seq) !== 1) continue;
    const itemTime = Date.parse(String(item.createdAt || ''));
    const elapsed = eventTime - itemTime;
    if (!Number.isFinite(elapsed) || elapsed < 0 || elapsed > 2_000) continue;
    return index;
  }
  return -1;
}

function projectFromUrl() {
  if (typeof window === 'undefined') return null;
  return new URLSearchParams(window.location?.search || '').get('project');
}

function timeOf(value) {
  const date = value ? new Date(value) : new Date();
  return Number.isNaN(date.getTime())
    ? ''
    : `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
}

function messageFingerprint(message) {
  const block = message?.blocks?.[0] || {};
  if (block.type === 'answer') return `answer:${block.interactionId || ''}:${stableJson(block.answer || {})}`;
  return block.type === 'text' ? `text:${String(block.text || '').trim()}` : '';
}

function enrichInteractionAnswer(interaction, answer) {
  if (!answer || typeof answer !== 'object' || typeof answer.approved === 'boolean' || answer.displayText) return answer;
  const chosen = (() => { if (Array.isArray(answer.selected) && answer.selected.length) return answer.selected; return answer.value ? [answer.value] : []; })();
  const options = interaction?.payload?.options || [];
  const labels = chosen.map((value) => options.find((option) => option.value === value)?.label).filter(Boolean);
  return labels.length ? { ...answer, displayText: labels.join('、') } : answer;
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function materialPurposeLabel(purpose) {
  return ({
    auto: '由助手判断',
    goal: '建模目标依据',
    schema: '数据 Schema',
    term: '术语表',
    evidence: '通用业务依据',
  })[purpose] || '由助手判断';
}

function focusTargetFor(kind) {
  return { flow: 'diagrams/采购申请.mmd', diff: 'rules/安全库存判断.md', doc: '作业手册.pdf',
    impact: '影响分析', conflict: '冲突清单', gates: '正式发布门禁', studio: '知识包 Studio' }[kind];
}

/** 文件类型 → 中栏视图。这是「点左栏跳中栏」的唯一映射点。 */
export function focusForPath(path) {
  if (path.endsWith('.mmd')) return { kind: 'flow', target: path };
  return { kind: 'file', target: path };
}

function availableFocusTarget(focus, state) {
  if (focus?.kind === 'doc') {
    return state.materials.some((item) => item.id === focus.target || item.filename === focus.target);
  }
  if (!focus?.target || !['flow', 'file', 'diff'].includes(focus.kind)) return true;
  if (focus.kind === 'diff') {
    return state.candidateDiff.some((item) => item.path === focus.target);
  }
  return state.files.includes(focus.target);
}

export function resolveProjectFocusForPhase(focus, workflowPhase = '') {
  if (focus?.kind !== 'gates') return focus || null;
  // 正式门禁视图暂不开放；只有评测阶段才回退到可操作的评测界面。
  if (workflowPhase !== 'evaluation') return null;
  return { kind: 'evaluation', target: '正式评测' };
}

export function resolveBootstrapFocus({
  current,
  candidateFiles = [],
  interaction,
  agentFocus,
  routeFocus,
  workflowPhase = '',
}) {
  const currentFocus = resolveProjectFocusForPhase(current, workflowPhase);
  if (currentFocus?.kind && currentFocus.kind !== 'empty') return currentFocus;
  const routedFocus = resolveProjectFocusForPhase(routeFocus, workflowPhase);
  if (routedFocus?.kind) return routedFocus;
  if (interaction?.kind === 'confirmation' && candidateFiles[0]?.path) {
    return { kind: 'diff', target: candidateFiles[0].path };
  }
  const restoredFocus = resolveProjectFocusForPhase(agentFocus, workflowPhase);
  if (restoredFocus?.kind) return restoredFocus;
  if (workflowPhase === 'evaluation') return { kind: 'evaluation', target: '正式评测' };
  return { ...DEFAULT_STUDIO_FOCUS };
}

export function studioFocusFromLocation(location = globalThis.window?.location) {
  if (!location?.href) return null;
  const params = new URL(location.href).searchParams;
  const view = params.get('studioView');
  if (!['graph', 'table', 'entry'].includes(view)) return null;
  const elementId = params.get('studioElement') || '';
  return {
    kind: 'studio',
    target: '知识包 Studio',
    view,
    ...(elementId ? { elementId } : {}),
  };
}
