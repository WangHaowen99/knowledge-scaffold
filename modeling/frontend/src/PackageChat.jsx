import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  discardPackageDraft, getPackage, getPackageDraft, packageChat, publishPackageDraft,
  runAnalyze, runPackageCheck, uploadDocument,
} from './api.js';
import { confirmReplayBlockedMessage, shouldReplayConfirm } from './confirmReplay.js';
import {
  adjustAiConstructionTask,
  cancelAiConstructionOperation,
  confirmAiConstructionMilestone,
  proposeAiConstructionIncrement,
  startAiConstructionIncrement,
  deferAiConstructionKnowledgeErrors,
  deferAiConstructionScripts,
  editAiConstructionTasks,
  generateAiConstructionKnowledge,
  getAiConstruction,
  repairAiConstructionKnowledge,
  previewAiConstructionTaskAdjustment,
  previewAiConstructionTaskEdit,
  respondToInteraction,
  retryAiConstructionScripts,
  reviseAiConstructionModelingBrief,
} from './authoring-api.js';
import { uploadModelingMaterials } from './modeling-material-upload.js';
import { useElapsed } from './ui.jsx';
import AiConstructionMessageCard from './AiConstructionMessageCard.jsx';
import PendingInteraction from './PendingInteraction.jsx';
import PendingInteractionHint from './PendingInteractionHint.jsx';
import TaskAdjustmentPreviewCard from './TaskAdjustmentPreviewCard.jsx';
import TaskGraphEditDialog from './TaskGraphEditDialog.jsx';
import ValidationDeferralCard from './ValidationDeferralCard.jsx';
import MarkdownContent from './MarkdownContent.jsx';
import { confirmFlowchartNavigation, confirmUploadDuringAnalysis } from './flowchartNavigationGuard.js';
import { formatMessageTimestamp } from './messageTimestamp.js';
import { NAVIGATION_TARGETS, assistantActionKind, isReadOnlyAction } from './assistantActions.js';
import { completionBlockerMessage, markSurfacedAsBlocker } from './completionBlocker.js';
import { repairTargetCandidatesFrom } from './repairTargetChoice.js';
import { isPackageChatRunning } from './packageChatRun.js';
import { activeRunLabel, activeRunOf } from './activeRun.js';
import { CANCEL_PENDING_HINT, cancellableOperationOf } from './cancellableOperation.js';
import { stallHint, stalledForMs } from './taskStall.js';
import { incrementEntryState } from './incrementEntry.js';
import { effectiveTaskPlanTasks, summarizeTaskPlan } from './taskPlanStatus.js';
import {
  canUseStructuredMessageActions,
  confirmedMilestoneActionIds,
  isMaterialAnalysisRunning,
  latestStructuredActionMessageId,
  milestoneNavigationTarget,
  mergePackageChatTimeline,
} from './packageChatTimeline.js';


// 知识包工作区右栏：建模对话（咨询 + 两段式受控修改）。
// 发消息 → agent 影响分析：咨询直接回答；修改类返回计划卡片（改哪些对象/操作 + 风险）→ 专家确认 → 应用落新版本。
const MATERIAL_ACCEPT = '.xlsx,.xls,.csv,.md,.markdown,.txt,.docx,.doc,.pdf';
// 进度经事件流推送后不再需要秒级轮询，这里只兜底事件连接中断的情况
const LONG_TASK_FALLBACK_REFRESH_MS = 15_000;
const timedTurn = (turn) => ({ ...turn, createdAt: new Date().toISOString() });

const MILESTONE_CONFIRM_NOTE = {
  materials_ready: '确认当前建模资料范围',
  goal_confirmed: '确认当前建模目标说明',
  process_confirmed: '确认当前业务流程',
  knowledge_confirmed: '确认当前知识结构',
  deterministic_confirmed: '确认当前确定性能力',
  construction_completed: '确认完成 AI 辅助构建',
};

export function shouldOpenWarningReview(error) {
  return error?.data?.code === 'PACKAGE_WARNINGS_UNREVIEWED';
}

export function interactionBusyState(purpose) {
  if (purpose === 'controlled_repair') return 'repair_knowledge';
  if (purpose === 'script_contract') return 'script_generation';
  if (purpose === 'script_generation') return 'script_apply';
  return 'interaction';
}

export function interactionResponseChangedPackage(result) {
  return Boolean(result?.packageRevision || result?.revision || result?.result?.version);
}

export function MaterialAnalysisStoppedNotice({ task, busy = false, onRetry }) {
  if (!task || task.status !== 'failed'
    || !['material_analysis', 'package_baseline_review'].includes(task.kind)) return null;
  return (
    <section className="material-analysis-stopped" role="alert">
      <div>
        <strong>资料分析已停止</strong>
        <p>{task.reason || '资料分析未完成，请重新执行当前步骤。'}</p>
      </div>
      <button type="button" className="btn btn-p" disabled={busy} onClick={onRetry}>
        {busy ? '正在重新分析…' : '重新分析资料'}
      </button>
    </section>
  );
}

function hasMaterialAnalysisFailureMessage(messages, taskId) {
  return messages.some((message) => message.taskId === taskId
    && (message.blocks ?? []).some((block) => block.type === 'next_actions'
      && (block.actions ?? []).some((action) => action.id === 'analyze_materials'))
    && (message.blocks ?? []).some((block) => block.type === 'status'
      && /分析.*(?:停止|未完成)/u.test(String(block.title ?? ''))));
}

function etaText(eta) {
  if (eta?.minMinutes == null || eta?.maxMinutes == null) return '剩余时长正在估算';
  return eta.minMinutes === eta.maxMinutes
    ? `预计剩余 ${eta.minMinutes} 分钟`
    : `预计剩余 ${eta.minMinutes}–${eta.maxMinutes} 分钟`;
}

export default function PackageChat({
  projectId,
  taskStageLabel = '',
  structuredMessages = [],
  hasOlderMessages = false,
  olderMessagesBusy = false,
  olderMessagesError = '',
  onLoadOlderMessages,
  aiConstruction,
  authoring,
  // 当前界面渲染的是哪一版知识包。修订冲突后判断「内容有没有变」要用它做基线，
  // 不能等冲突发生了再去取——那时候拿到的已经是新版本，证明不了用户看过什么。
  packageRevision = '',
  // 后端正持有写租约时，待办要显示成「已提交，正在处理」而不是可点的表单
  activeOperation = null,
  pendingInteraction,
  flowchartDraftState,
  candidateRevisionPanel,
  candidateRevisionAttentionKey = '',
  onPendingInteractionBusyChange,
  confirmFlowchartMutation,
  onProjectChanged,
  onNavigate,
  uploadFile = uploadDocument,
}) {
  // 服务端结构化消息是完整会话记录；turns 只用于尚未刷新回来的临时界面反馈。
  const [turns, setTurns] = useState([]);
  const [input, setInput] = useState('');
  const [localBusy, setLocalBusy] = useState('');
  // 本地发起操作的时刻。服务端 run 未必立刻出现在快照里，这段空窗期如果没有基线，
  // 停滞判定会退回到任务上的旧消息，把刚点下去的操作误报成「已经 N 分钟没有新进展」。
  const localBusySince = useRef('');
  useEffect(() => {
    if (localBusy && !localBusySince.current) localBusySince.current = new Date().toISOString();
    if (!localBusy) localBusySince.current = '';
  }, [localBusy]);
  const [taskAdjustment, setTaskAdjustment] = useState(null);
  const [taskEditConfig, setTaskEditConfig] = useState(null);
  const [validationDeferral, setValidationDeferral] = useState(null);
  const [completionBlocker, setCompletionBlocker] = useState(null);
  // 被修订冲突挡下的那次确认，连同当时界面上的知识包版本一起记着，供 reload_and_retry 判断
  const [conflictedConfirm, setConflictedConfirm] = useState(null);
  // 已经按过取消、还没停下来的那段时间，按钮要禁用并说明正在停止
  const [cancelling, setCancelling] = useState('');
  // 助手分析在服务端后台跑，本地 busy 只覆盖提交那一瞬；两者合成对外统一的忙碌态，
  // 这样刷新页面或换标签页也能继续看到“正在分析”。
  const chatRunning = useMemo(() => isPackageChatRunning(authoring), [authoring]);
  // 服务端还在跑的运行。本地 busy 一刷新就没了，但后端没停——只认本地会让用户
  // 以为提交丢了。本地态优先（文案更贴合刚点的按钮），没有本地态时用服务端的。
  const serverRun = useMemo(() => activeRunOf(authoring), [authoring]);
  const busy = localBusy || (chatRunning ? 'plan' : '') || (serverRun ? 'server_run' : '');
  // 计时也走服务端开始时间，刷新后不会从 0 秒重新数
  const sec = useElapsed(!!busy, localBusy ? undefined : serverRun?.createdAt);
  const bodyRef = useRef(null);
  const loadingOlderRef = useRef(false);
  const materialInputRef = useRef(null);
  const timeline = useMemo(() => mergePackageChatTimeline({ turns, structuredMessages }), [turns, structuredMessages]);
  const latestActionMessageId = useMemo(() => latestStructuredActionMessageId(timeline), [timeline]);
  const analysisRunning = useMemo(() => isMaterialAnalysisRunning(aiConstruction), [aiConstruction]);
  const confirmedActionIds = useMemo(
    () => confirmedMilestoneActionIds(aiConstruction),
    [aiConstruction],
  );
  useEffect(() => {
    setTurns([]); setTaskAdjustment(null); setTaskEditConfig(null); setValidationDeferral(null);
    setCompletionBlocker(null);
  }, [projectId]);

  const latestValidationErrorCount = useMemo(() => {
    for (const message of [...structuredMessages].reverse()) {
      for (const block of [...(message.blocks ?? [])].reverse()) {
        const count = Number(block?.type === 'validation' ? block.counts?.errors : 0);
        if (count > 0) return count;
      }
    }
    return 0;
  }, [structuredMessages]);

  useEffect(() => {
    if (loadingOlderRef.current) {
      loadingOlderRef.current = false;
      return;
    }
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: 'smooth' });
  }, [timeline.length, busy]);

  // 候选卡片在独立请求完成后才挂载，不能依赖消息数量变化来进入可视区。
  useEffect(() => {
    if (!candidateRevisionAttentionKey) return;
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: 'smooth' });
  }, [candidateRevisionAttentionKey]);

  const loadOlder = async () => {
    if (!hasOlderMessages || olderMessagesBusy || !onLoadOlderMessages) return;
    loadingOlderRef.current = true;
    const loaded = await onLoadOlderMessages();
    if (!loaded) loadingOlderRef.current = false;
  };

  // 体检面板「交给修缮对话」预填
  useEffect(() => {
    const h = (e) => setInput(e.detail || '');
    window.addEventListener('mw-chat-prefill', h);
    return () => window.removeEventListener('mw-chat-prefill', h);
  }, []);

  // 进度由事件流推送，这里只做兜底：事件连接中断时长任务仍能看到进展。
  useEffect(() => {
    const longTask = ['analyze', 'generate_knowledge', 'repair_knowledge', 'script_generation', 'script_apply']
      .includes(busy);
    if (!longTask && !analysisRunning && !chatRunning) return undefined;
    let refreshing = false;
    const timer = window.setInterval(() => {
      if (refreshing) return;
      refreshing = true;
      Promise.resolve(onProjectChanged?.()).finally(() => { refreshing = false; });
    }, LONG_TASK_FALLBACK_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [analysisRunning, chatRunning, busy, onProjectChanged, projectId]);

  const incrementEntry = useMemo(() => incrementEntryState(aiConstruction), [aiConstruction]);
  const cancellableOperation = useMemo(
    () => cancellableOperationOf({ busy, serverRun }),
    [busy, serverRun],
  );
  // 「还在跑」和「已经挂了」原先长得一样。sec 每秒都变，顺带让这个判断持续刷新。
  const stalledMs = useMemo(() => (busy ? stalledForMs({
    messages: structuredMessages,
    taskId: aiConstruction?.progress?.currentTaskId,
    startedAt: localBusy ? localBusySince.current : serverRun?.createdAt,
  }) : null), [busy, localBusy, sec, structuredMessages, aiConstruction, serverRun]);
  // 忙碌态结束就把「正在停止」清掉，否则下一轮任务的按钮会一直是禁用的
  useEffect(() => { if (!busy) setCancelling(''); }, [busy]);

  const cancelRunningOperation = async (operation) => {
    if (cancelling) return;
    setCancelling(operation);
    try {
      await cancelAiConstructionOperation(projectId, operation);
    } catch (error) {
      setCancelling('');
      setTurns((items) => items.concat([timedTurn({
        role: 'assistant',
        text: `无法取消：${error instanceof Error ? error.message : String(error)}`,
      })]));
    }
  };

  const refreshConstruction = async () => {
    await onProjectChanged?.();
  };

  // 导航类动作只切视图、不写数据，目标在 assistantActions 的注册表里声明
  const runNavigation = (action) => {
    const target = NAVIGATION_TARGETS[action.id];
    if (!target) return;
    setCompletionBlocker(null);
    if (target.reload) { void refreshConstruction(); return; }
    if (target.file) {
      const path = target.file(action);
      if (!path) return;
      window.dispatchEvent(new CustomEvent('mw-package-file-select', {
        detail: { projectId, path, target: path },
      }));
      return;
    }
    if (target.step) { onNavigate?.(target.step); return; }
    if (target.evaluationView) {
      window.dispatchEvent(new CustomEvent('mw-open-evaluation-view', {
        detail: { projectId, view: target.evaluationView },
      }));
      return;
    }
    if (target.openPackageCheck) {
      window.dispatchEvent(new CustomEvent('mw-package-check-open', { detail: { projectId } }));
      return;
    }
    if (target.focus) {
      window.requestAnimationFrame?.(() => {
        bodyRef.current?.querySelector?.(target.focus)?.focus?.();
      });
    }
  };

  const handleStructuredAction = async (action) => {
    if (analysisRunning && ['analyze_materials', 'review_package'].includes(action.id)) return;
    if (flowchartDraftState?.dirty && !isReadOnlyAction(action)) {
      const allowed = confirmFlowchartMutation
        ? confirmFlowchartMutation()
        : confirmFlowchartNavigation(true);
      if (!allowed) return;
    }
    if (assistantActionKind(action) === 'navigate') {
      runNavigation(action);
      return;
    }
    if (action.id === 'preview_task_adjustment') {
      if (busy || !action.taskId || !action.adjustment) return;
      setLocalBusy('task_preview');
      try {
        const result = await previewAiConstructionTaskAdjustment(projectId, action.taskId, action.adjustment);
        setTaskAdjustment(result.preview);
      } catch (error) {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant', text: `无法调整任务：${error instanceof Error ? error.message : String(error)}`,
        })]));
      } finally { setLocalBusy(''); }
      return;
    }
    // 新建流程的弹窗归工作台管，助手只发一个打开请求，不重复实现一套
    if (action.id === 'add_flowchart_process') {
      setCompletionBlocker(null);
      window.dispatchEvent(new CustomEvent('mw-flowchart-add-open', { detail: { projectId } }));
      return;
    }
    // 增量补充：开一轮 / 传完资料后生成方案。两者都由后端做准入判断并给出口，
    // 前端只负责发起和把失败渲染成可操作卡片。
    if (action.id === 'start_increment_round' || action.id === 'propose_increment') {
      if (busy) return;
      const starting = action.id === 'start_increment_round';
      setLocalBusy(starting ? 'increment_start' : 'increment_propose');
      try {
        if (starting) await startAiConstructionIncrement(projectId);
        else await proposeAiConstructionIncrement(projectId);
        setCompletionBlocker(null);
      } catch (error) {
        const blocked = completionBlockerMessage(error);
        if (blocked) setCompletionBlocker(blocked);
        else {
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: `${starting ? '开始增量补充' : '生成增量方案'}失败：${error instanceof Error ? error.message : String(error)}`,
          })]));
        }
      } finally {
        setLocalBusy('');
        await refreshConstruction();
      }
      return;
    }
    if (action.id === 'defer_validation_errors') {
      if (!busy) setValidationDeferral({ errorCount: Math.max(1, latestValidationErrorCount) });
      return;
    }
    // 修订冲突后的重试。不无条件重发：先看你当时确认的那份知识包内容还在不在，
    // 变了就退回让人重新看过——否则等于替用户确认了一份他没看过的东西。
    if (action.id === 'reload_and_retry') {
      if (busy) return;
      const intent = conflictedConfirm;
      setLocalBusy('confirm');
      try {
        await refreshConstruction();
        if (!intent?.milestoneId) {
          setCompletionBlocker(null);
          return;
        }
        // 直接取最新值，不用 props——refreshConstruction 之后 props 还没重新渲染进来
        const [currentPackage, current] = await Promise.all([
          getPackage(projectId),
          getAiConstruction(projectId),
        ]);
        const currentPackageRevision = currentPackage?.revision ?? '';
        const graph = current?.aiConstruction;
        if (!shouldReplayConfirm({
          seenPackageRevision: intent.seenPackageRevision, currentPackageRevision,
        })) {
          setConflictedConfirm(null);
          setCompletionBlocker(null);
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: confirmReplayBlockedMessage({
              seenPackageRevision: intent.seenPackageRevision, currentPackageRevision,
            }),
          })]));
          return;
        }
        if (graph?.milestones?.find((item) => item.id === intent.milestoneId)?.status === 'confirmed') {
          setConflictedConfirm(null);
          setCompletionBlocker(null);
          return;
        }
        if (!Number.isInteger(graph?.revision)) throw new Error('建模状态尚未加载，请刷新后重试');
        await confirmAiConstructionMilestone(
          projectId, intent.milestoneId, graph.revision, MILESTONE_CONFIRM_NOTE[intent.milestoneId],
        );
        setConflictedConfirm(null);
        setCompletionBlocker(null);
        const navigationTarget = milestoneNavigationTarget(intent.milestoneId);
        if (navigationTarget) onNavigate?.(navigationTarget);
      } catch (error) {
        const blocked = completionBlockerMessage(error);
        if (blocked) setCompletionBlocker(blocked);
        else {
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: `重试确认失败：${error instanceof Error ? error.message : String(error)}`,
          })]));
        }
      } finally {
        setLocalBusy('');
        await refreshConstruction().catch(() => undefined);
      }
      return;
    }
    // 未发布草稿会拦住确认和发布，而 发布/放弃 按钮只在文件编辑器里才出现，
    // 光把用户送去知识文件他还得自己翻到编辑态——这里直接给出口。
    if (['publish_package_draft', 'discard_package_draft'].includes(action.id)) {
      if (busy) return;
      const discard = action.id === 'discard_package_draft';
      const draft = await getPackageDraft(projectId).catch(() => null);
      if (!draft?.id) {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant', text: '没有找到未发布的人工编辑草稿，可能已经被处理过了。',
        })]));
        await refreshConstruction();
        return;
      }
      const confirmed = window.confirm(discard
        ? '放弃整份人工编辑草稿？所有已保存但未发布的修改都会丢失。'
        : `发布这份人工编辑草稿（${draft.changedFileCount ?? 0} 个文件）并生成新版本？`);
      if (!confirmed) return;
      setLocalBusy(discard ? 'draft_discard' : 'draft_publish');
      try {
        if (discard) await discardPackageDraft(projectId, draft.revision, draft.id);
        else await publishPackageDraft(projectId, draft.revision, '人工编辑', draft.id);
        setCompletionBlocker(null);
        window.dispatchEvent(new CustomEvent('mw-package-updated'));
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: discard ? '已放弃人工编辑草稿，可以继续了。' : '已发布人工编辑草稿，可以继续了。',
        })]));
      } catch (error) {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `${discard ? '放弃' : '发布'}草稿失败：${error instanceof Error ? error.message : String(error)}`,
        })]));
      } finally {
        setLocalBusy('');
        await refreshConstruction();
      }
      return;
    }
    if (['open_task_add', 'open_task_reorder', 'open_task_split', 'open_task_merge'].includes(action.id)) {
      if (busy) return;
      if (action.id === 'open_task_merge' && (!action.tasks || action.tasks.length < 2)) return;
      setTaskEditConfig({
        mode: action.mode,
        ...(action.milestoneId ? { milestoneId: action.milestoneId } : {}),
        ...(action.task ? { task: action.task } : {}),
        ...(action.tasks ? { tasks: action.tasks } : {}),
        ...(action.edit ? { edit: action.edit } : {}),
      });
      return;
    }
    if (action.id === 'run_package_check') {
      if (busy) return;
      setLocalBusy('check');
      try {
        await runPackageCheck(projectId);
        window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
      } catch (error) {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `知识包校验失败：${error instanceof Error ? error.message : String(error)}`,
        })]));
      } finally { setLocalBusy(''); }
      return;
    }
    if (action.id === 'upload_materials') {
      if (!busy) materialInputRef.current?.click();
      return;
    }
    if (['generate_knowledge_structure', 'repair_knowledge_structure'].includes(action.id)) {
      if (busy) return;
      const repairing = action.id === 'repair_knowledge_structure';
      setLocalBusy(repairing ? 'repair_knowledge' : 'generate_knowledge');
      try {
        const result = repairing
          ? await repairAiConstructionKnowledge(projectId)
          : await generateAiConstructionKnowledge(projectId);
        window.dispatchEvent(new CustomEvent('mw-package-updated'));
        if (repairing && result.status === 'waiting_expert') {
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: '修复涉及业务阈值、流程分支、计算口径或冲突裁决，请先完成右侧专家问题。',
          })]));
        } else if (repairing && result.status === 'waiting_approval') {
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: '已生成知识语义修复方案，请在右侧确认后再应用。',
          })]));
        } else if (repairing && result.status === 'stopped') {
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: '自动修复已达到重试上限，已停止并保留全部尝试记录。请检查知识文件或校验详情。',
          })]));
        } else if (result.validation?.errors > 0) {
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: repairing
              ? `本轮逐项修复已完成，但仍有 ${result.validation.errors} 个错误，请继续处理。`
              : `知识结构已生成，但校验仍有 ${result.validation.errors} 个错误，请按右侧建议继续修复。`,
          })]));
        }
        await refreshConstruction();
      } catch (error) {
        await refreshConstruction().catch(() => undefined);
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `${repairing ? '知识结构修复' : '知识结构生成'}失败：${error instanceof Error ? error.message : String(error)}`,
        })]));
      } finally {
        setLocalBusy('');
      }
      return;
    }
    if (action.id === 'defer_script_generation') {
      if (busy) return;
      setLocalBusy('interaction');
      try {
        await deferAiConstructionScripts(projectId);
        await refreshConstruction();
      } catch (error) {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `暂缓确定性脚本失败：${error instanceof Error ? error.message : String(error)}`,
        })]));
      } finally {
        setLocalBusy('');
      }
      return;
    }
    if (action.id === 'retry_script_generation') {
      if (busy) return;
      setLocalBusy('script_generation');
      try {
        await retryAiConstructionScripts(projectId);
        await refreshConstruction();
      } catch (error) {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `重新生成确定性脚本失败：${error instanceof Error ? error.message : String(error)}`,
        })]));
      } finally {
        setLocalBusy('');
      }
      return;
    }
    if (action.id === 'revise_goal') {
      if (busy) return;
      setLocalBusy('interaction');
      try {
        await reviseAiConstructionModelingBrief(projectId);
        await refreshConstruction();
      } catch (error) {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `重新填写建模目标失败：${error instanceof Error ? error.message : String(error)}`,
        })]));
      } finally {
        setLocalBusy('');
      }
      return;
    }
    const milestoneByAction = {
      confirm_materials: 'materials_ready',
      confirm_goal: 'goal_confirmed',
      confirm_process: 'process_confirmed',
      confirm_knowledge: 'knowledge_confirmed',
      confirm_deterministic: 'deterministic_confirmed',
      confirm_construction: 'construction_completed',
    };
    const milestoneId = milestoneByAction[action.id];
    if (busy || (action.id !== 'analyze_materials' && !milestoneId)) return;
    setLocalBusy(action.id === 'analyze_materials' ? 'analyze' : 'confirm');
    try {
      if (action.id === 'analyze_materials') {
        await runAnalyze(projectId);
      } else {
        const milestone = aiConstruction?.milestones?.find((item) => item.id === milestoneId);
        if (milestone?.status === 'confirmed') {
          const confirmedLabel = {
            materials_ready: '资料范围', goal_confirmed: '建模目标',
            process_confirmed: '业务流程', knowledge_confirmed: '知识结构',
            deterministic_confirmed: '确定性能力',
            construction_completed: 'AI 辅助构建',
          }[milestoneId] ?? '当前阶段';
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: `${confirmedLabel}已经确认，可以继续当前建模任务。`,
          })]));
          const navigationTarget = milestoneNavigationTarget(milestoneId);
          if (navigationTarget) onNavigate?.(navigationTarget);
          return;
        }
        if (!Number.isInteger(aiConstruction?.revision)) throw new Error('建模状态尚未加载，请刷新后重试');
        await confirmAiConstructionMilestone(
          projectId,
          milestoneId,
          aiConstruction.revision,
          MILESTONE_CONFIRM_NOTE[milestoneId],
        );
      }
      await refreshConstruction();
      const navigationTarget = milestoneNavigationTarget(milestoneId);
      if (navigationTarget) onNavigate?.(navigationTarget);
    } catch (error) {
      if (action.id === 'confirm_construction' && shouldOpenWarningReview(error)) {
        window.dispatchEvent(new CustomEvent('mw-package-warning-review', {
          detail: { projectId, warningReview: error.data?.warningReview },
        }));
      }
      // 记下这次确认针对的是哪一版内容，reload_and_retry 靠它判断能不能安全重发
      if (error?.data?.code === 'REVISION_CONFLICT' && milestoneId) {
        setConflictedConfirm({ milestoneId, seenPackageRevision: packageRevision });
      }
      // 被拦下时后端会声明可执行的下一步，渲染成卡片而不是一句无出口的错误文本
      const blocked = completionBlockerMessage(error);
      if (blocked) {
        setCompletionBlocker(blocked);
      } else {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `${action.id === 'analyze_materials' ? '资料分析' : '阶段确认'}失败：${error instanceof Error ? error.message : String(error)}`,
        })]));
      }
      // 拦截多半源于服务端状态已变（如修订被更新），不刷新会让下一次点击重复失败
      await refreshConstruction().catch(() => undefined);
    } finally {
      setLocalBusy('');
    }
  };

  const currentTask = aiConstruction?.tasks?.find(
    (task) => task.id === aiConstruction?.progress?.currentTaskId,
  );
  const currentTasks = aiConstruction?.tasks?.filter(
    (task) => task.milestoneId === aiConstruction?.progress?.currentMilestoneId,
  ) ?? [];
  const visibleTasks = effectiveTaskPlanTasks(currentTasks);
  const taskPlanSummary = summarizeTaskPlan(visibleTasks);
  const showMaterialAnalysisStopped = currentTask?.status === 'failed'
    && !hasMaterialAnalysisFailureMessage(structuredMessages, currentTask.id);
  const visibleTimeline = showMaterialAnalysisStopped
    ? timeline.filter((entry) => !(entry.kind === 'structured'
      && entry.item.taskId === currentTask.id
      && String(entry.item.key ?? '').startsWith('analysis.progress.')))
    : timeline;
  const liveTaskPlanMessage = visibleTasks.length ? {
    id: 'live-task-plan', role: 'assistant', createdAt: aiConstruction?.updatedAt, blocks: [{
      type: 'task_plan', title: '当前动态任务', note: aiConstruction?.progress?.eta?.note,
      milestoneId: aiConstruction?.progress?.currentMilestoneId,
      tasks: visibleTasks.map((task) => ({
        id: task.id, title: task.title, goal: task.goal, kind: task.kind,
        milestoneId: task.milestoneId, dependsOn: task.dependsOn, status: task.status,
        required: task.required, ...(task.reason ? { reason: task.reason } : {}),
      })),
    }],
  } : null;

  const confirmTaskAdjustment = async (reason) => {
    if (!taskAdjustment || busy) return;
    setLocalBusy('task_adjustment');
    try {
      await adjustAiConstructionTask(projectId, taskAdjustment.task.id, {
        expectedRevision: taskAdjustment.graphRevision,
        action: taskAdjustment.action,
        reason,
      });
      setTaskAdjustment(null);
      await refreshConstruction();
    } catch (error) {
      setTurns((items) => items.concat([timedTurn({
        role: 'assistant', text: `任务调整失败：${error instanceof Error ? error.message : String(error)}`,
      })]));
      await refreshConstruction().catch(() => undefined);
    } finally { setLocalBusy(''); }
  };

  const confirmValidationDeferral = async (reason) => {
    if (!validationDeferral || busy) return;
    setLocalBusy('defer_validation');
    try {
      await deferAiConstructionKnowledgeErrors(projectId, reason);
      setValidationDeferral(null);
      await refreshConstruction();
    } catch (error) {
      setTurns((items) => items.concat([timedTurn({
        role: 'assistant', text: `暂缓校验修复失败：${error instanceof Error ? error.message : String(error)}`,
      })]));
      await refreshConstruction().catch(() => undefined);
    } finally { setLocalBusy(''); }
  };

  const previewTaskEdit = async (edit) => {
    const result = await previewAiConstructionTaskEdit(projectId, edit);
    return result.preview;
  };

  const applyTaskEdit = async ({ edit, expectedRevision, reason }) => {
    if (busy) return;
    setLocalBusy('task_edit');
    try {
      await editAiConstructionTasks(projectId, { edit, expectedRevision, reason });
      setTaskEditConfig(null);
      await refreshConstruction();
    } catch (error) {
      setTurns((items) => items.concat([timedTurn({
        role: 'assistant', text: `任务图编辑失败：${error instanceof Error ? error.message : String(error)}`,
      })]));
      await refreshConstruction().catch(() => undefined);
      throw error;
    } finally { setLocalBusy(''); }
  };

  const uploadMaterials = async (files) => {
    if (!files?.length || busy) return;
    if (!confirmUploadDuringAnalysis(analysisRunning)) return;
    setLocalBusy('upload');
    try {
      const result = await uploadModelingMaterials({ projectId, files, uploadFile });
      setTurns((items) => items.concat([timedTurn({
        role: 'assistant',
        text: `已上传 ${result.count} 份建模资料：${result.filenames.join('、')}。我正在刷新建模状态。`,
      })]));
      await onProjectChanged?.();
    } catch (error) {
      setTurns((items) => items.concat([timedTurn({
        role: 'assistant',
        text: `资料上传失败：${error instanceof Error ? error.message : String(error)}`,
      })]));
    } finally {
      setLocalBusy('');
    }
  };

  const submitPendingInteraction = async (interactionId, response) => {
    if (busy) return;
    const purpose = pendingInteraction?.payload?.purpose;
    onPendingInteractionBusyChange?.(true);
    setLocalBusy(interactionBusyState(purpose));
    try {
      const result = await respondToInteraction(projectId, interactionId, response);
      if (interactionResponseChangedPackage(result)) {
        window.dispatchEvent(new CustomEvent('mw-package-updated'));
      }
      await refreshConstruction();
      return result;
    } catch (error) {
      // 待办提交原本自成一套错误显示（一行红字），后端声明的 nextActions 被丢掉。
      // 接到与其余路径同一张卡片上；REPAIR_TARGET_UNRESOLVED 除外——那条有更贴合的
      // 就地单选，卡片会变成重复信息。
      if (!repairTargetCandidatesFrom(error).length) {
        const blocked = completionBlockerMessage(error);
        if (blocked) {
          setCompletionBlocker(blocked);
          markSurfacedAsBlocker(error);
        }
      }
      await refreshConstruction().catch(() => undefined);
      throw error;
    } finally {
      setLocalBusy('');
      onPendingInteractionBusyChange?.(false);
    }
  };

  const send = async () => {
    const message = input.trim();
    if (!message || busy) return;
    const requestId = globalThis.crypto?.randomUUID?.() ?? `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setInput('');
    setTurns((t) => [...t, timedTurn({ requestId, role: 'user', text: message })]);
    setLocalBusy('plan');
    try {
      // 请求只负责把任务交给服务端；分析进度与结果都由事件流送回，忙碌态改由 activeRuns 接管
      await packageChat(projectId, message, requestId);
      await refreshConstruction();
      setTurns((items) => items.filter((item) => item.requestId !== requestId));
    } catch (e) {
      setTurns((t) => t.concat([timedTurn({ requestId, role: 'assistant', text: `出错了：${e.message}` })]));
      setInput((current) => current.trim() ? current : message); // 不覆盖请求期间新输入的草稿
      await refreshConstruction().catch(() => undefined);
    } finally { setLocalBusy(''); }
  };

  return (
    <div className="pchat">
      <input
        ref={materialInputRef}
        type="file"
        multiple
        accept={MATERIAL_ACCEPT}
        aria-label="上传建模资料"
        style={{ display: 'none' }}
        onChange={(event) => {
          void uploadMaterials([...event.target.files]);
          event.target.value = '';
        }}
      />
      <header className="pchat-head">
        <span className="pchat-avatar" aria-hidden="true">知</span>
        <div>
          <strong>知识包建模助手</strong>
          <span>咨询、检查并完善对象、操作、规则、指标与脚本</span>
        </div>
        {incrementEntry.visible && (
          <button type="button" className="pchat-increment-entry" disabled={Boolean(busy)}
            title="知识包已建完。补充新资料时只处理增量，已确认的内容保持不变。"
            onClick={() => { void handleStructuredAction({ id: incrementEntry.action }); }}>
            {incrementEntry.label}
          </button>
        )}
        {liveTaskPlanMessage && (
          <div className="pchat-task-status">
            <button type="button" className="pchat-task-status__chip"
              aria-label={`当前任务状态：${taskStageLabel ? `${taskStageLabel} · ` : ''}${taskPlanSummary.statusLabel}；${taskPlanSummary.title}；已完成 ${taskPlanSummary.completed}/${taskPlanSummary.total}，悬停查看详情`}>
              <span className={`pchat-task-status__dot is-${taskPlanSummary.tone}`} aria-hidden="true" />
              <span className="pchat-task-status__label">
                {taskStageLabel ? `${taskStageLabel} · ${taskPlanSummary.statusLabel}` : taskPlanSummary.statusLabel}
              </span>
              <span className="pchat-task-status__title">{taskPlanSummary.title}</span>
              <span className="pchat-task-status__count">{taskPlanSummary.completed}/{taskPlanSummary.total}</span>
            </button>
            <div className="pchat-task-status__popover" role="tooltip">
              <AiConstructionMessageCard
                message={liveTaskPlanMessage}
                onAction={(action) => { void handleStructuredAction(action); }}
              />
            </div>
          </div>
        )}
      </header>
      <div className="pchat-body" ref={bodyRef}>
        {(hasOlderMessages || olderMessagesBusy || olderMessagesError) && (
          <div className="pchat-history-loader">
            {hasOlderMessages && <button type="button" disabled={olderMessagesBusy}
              onClick={() => { void loadOlder(); }}>
              {olderMessagesBusy ? '正在加载更早消息…' : '加载更早消息'}
            </button>}
            {olderMessagesError && <span role="alert">{olderMessagesError}</span>}
          </div>
        )}
        {!structuredMessages.length && !turns.length && !pendingInteraction && (
          <div className="pchat-hint"><strong>你好，我是知识包建模助手。</strong><br />可以咨询当前知识包，也可以说明需要新增或调整的建模内容。</div>
        )}
        {visibleTimeline.map((entry) => entry.kind === 'structured' ? (
          <AiConstructionMessageCard key={`structured-${entry.item.id}`} message={entry.item} hideTaskPlan
            actionsEnabled={canUseStructuredMessageActions({
              busy: Boolean(busy),
              constructionStatus: aiConstruction?.status,
              messageId: entry.item.id,
              latestActionMessageId,
            })}
            disabledActionIds={[
              ...(analysisRunning ? ['analyze_materials', 'review_package'] : []),
              ...((authoring?.pendingInteractions ?? []).length ? ['confirm_construction'] : []),
            ]}
            hiddenActionIds={confirmedActionIds}
            onAction={(action) => { void handleStructuredAction(action); }} />
        ) : (
          <div key={`turn-${entry.turnIndex}`} className={`pchat-msg ${entry.item.role}`}>
                <div className="bubble">
                  {entry.item.role === 'assistant'
                    ? <MarkdownContent>{String(entry.item.text || '').replaceAll('知识包修缮助手', '知识包建模助手').replaceAll('受控修复', '逐项修复')}</MarkdownContent>
                    : String(entry.item.text || '').replaceAll('知识包修缮助手', '知识包建模助手')}
                  {entry.item.version && <div className="pchat-ver">已生成 v{entry.item.version}</div>}
                  {formatMessageTimestamp(entry.item.createdAt) && (
                    <time className="pchat-message-time" dateTime={entry.item.createdAt}>
                      {formatMessageTimestamp(entry.item.createdAt)}
                    </time>
                  )}
            </div>
          </div>
        ))}
        {candidateRevisionPanel && (
          <div className="pchat-msg assistant pchat-candidate-review">
            <div className="bubble">{candidateRevisionPanel}</div>
          </div>
        )}
        {showMaterialAnalysisStopped && <MaterialAnalysisStoppedNotice
          task={currentTask}
          busy={busy === 'analyze'}
          onRetry={() => { void handleStructuredAction({ id: 'analyze_materials' }); }}
        />}
        {completionBlocker && (
          <div className="ac-completion-blocker">
            <AiConstructionMessageCard
              message={completionBlocker}
              actionsEnabled={!busy}
              onAction={(action) => {
                setCompletionBlocker(null);
                void handleStructuredAction(action);
              }}
            />
            <button type="button" className="btn ac-completion-blocker__dismiss"
              onClick={() => setCompletionBlocker(null)}>知道了</button>
          </div>
        )}
        {taskAdjustment && <TaskAdjustmentPreviewCard
          preview={taskAdjustment}
          busy={busy === 'task_adjustment'}
          onConfirm={(reason) => { void confirmTaskAdjustment(reason); }}
          onCancel={() => setTaskAdjustment(null)}
        />}
        {validationDeferral && <ValidationDeferralCard
          errorCount={validationDeferral.errorCount}
          busy={busy === 'defer_validation'}
          onConfirm={(reason) => { void confirmValidationDeferral(reason); }}
          onCancel={() => setValidationDeferral(null)}
        />}
        {taskEditConfig && <TaskGraphEditDialog
          config={taskEditConfig}
          busy={busy === 'task_edit'}
          onPreview={previewTaskEdit}
          onApply={applyTaskEdit}
          onCancel={() => setTaskEditConfig(null)}
        />}
        {pendingInteraction && (
          <div className="ac-message-interaction">
            <PendingInteraction
              interaction={pendingInteraction}
              busy={Boolean(busy)}
              activeOperation={activeOperation}
              flowchartDraftState={flowchartDraftState}
              onSubmit={submitPendingInteraction}
            />
          </div>
        )}
        {busy && (
          <div className="pchat-msg assistant">
            <div className="bubble"><span className="spinner" /> {{
              plan: '正在分析', apply: '正在修改', upload: '正在上传建模资料',
              analyze: '正在分析建模资料', confirm: '正在确认当前阶段',
              interaction: '正在提交建模回答',
              generate_knowledge: `正在${currentTask?.title ?? '生成知识结构'} · ${etaText(aiConstruction?.progress?.eta)}`,
              repair_knowledge: `正在${String(currentTask?.title ?? '修复知识包').replaceAll('受控修复', '逐项修复')} · ${etaText(aiConstruction?.progress?.eta)}`,
              defer_validation: '正在记录暂缓决定并准备进入验证',
              script_generation: `正在生成脚本变更方案 · ${etaText(aiConstruction?.progress?.eta)}`,
              script_apply: `正在应用并校验确定性脚本 · ${etaText(aiConstruction?.progress?.eta)}`,
              task_preview: '正在分析任务调整影响', task_adjustment: '正在应用任务调整',
              task_edit: '正在应用任务图变更',
              increment_start: '正在开始增量补充',
              increment_propose: '正在生成增量修改方案',
              server_run: activeRunLabel(serverRun),
            }[busy] ?? '正在处理'} · {sec} 秒</div>
            {cancellableOperation && (
              <button type="button" className="pchat-cancel"
                disabled={Boolean(cancelling)}
                onClick={() => { void cancelRunningOperation(cancellableOperation); }}>
                {cancelling ? '正在停止…' : '取消'}
              </button>
            )}
          </div>
        )}
        {cancelling && <div className="pchat-msg assistant"><div className="bubble muted">{CANCEL_PENDING_HINT}</div></div>}
        {!cancelling && stalledMs != null && (
          <div className="pchat-msg assistant"><div className="bubble muted">{stallHint(stalledMs)}</div></div>
        )}
      </div>
      {pendingInteraction && (
        <PendingInteractionHint
          scrollRef={bodyRef}
          targetSelector=".ac-message-interaction"
          label={`还有 1 项待确认：${String(pendingInteraction.payload?.title ?? '请处理待办').slice(0, 20)}`}
        />
      )}
      <div className="pchat-input">
        <textarea rows={2} value={input} placeholder="咨询知识包，或说明要新增、修改的内容"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="btn btn-p" disabled={!!busy || !input.trim()} onClick={send}>发送</button>
      </div>
    </div>
  );
}
