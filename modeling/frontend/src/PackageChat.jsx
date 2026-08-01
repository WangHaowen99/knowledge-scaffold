import React, { useEffect, useMemo, useRef, useState } from 'react';
import { packageChat, runAnalyze, runPackageCheck, uploadDocument } from './api.js';
import {
  adjustAiConstructionTask,
  confirmAiConstructionMilestone,
  deferAiConstructionKnowledgeErrors,
  deferAiConstructionScripts,
  editAiConstructionTasks,
  generateAiConstructionKnowledge,
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
import TaskAdjustmentPreviewCard from './TaskAdjustmentPreviewCard.jsx';
import TaskGraphEditDialog from './TaskGraphEditDialog.jsx';
import ValidationDeferralCard from './ValidationDeferralCard.jsx';
import MarkdownContent from './MarkdownContent.jsx';
import { confirmFlowchartNavigation } from './flowchartNavigationGuard.js';
import { formatMessageTimestamp } from './messageTimestamp.js';
import { completionBlockerMessage } from './completionBlocker.js';
import { isPackageChatRunning } from './packageChatRun.js';
import { effectiveTaskPlanTasks, summarizeTaskPlan } from './taskPlanStatus.js';
import {
  canUseStructuredMessageActions,
  confirmedMilestoneActionIds,
  isMaterialAnalysisRunning,
  latestStructuredActionMessageId,
  milestoneNavigationTarget,
  mergePackageChatTimeline,
} from './packageChatTimeline.js';

const FLOWCHART_READ_ONLY_ACTIONS = new Set([
  'open_package_file',
  'preview_task_adjustment',
  'defer_validation_errors',
  'open_task_add',
  'open_task_reorder',
  'open_task_split',
  'open_task_merge',
  'explain_goal',
  'review_materials',
  'review_package',
  'view_validation',
  'review_process_files',
  'review_knowledge_files',
  'open_optimization',
  'open_scenario_review',
]);

// 知识包工作区右栏：建模对话（咨询 + 两段式受控修改）。
// 发消息 → agent 影响分析：咨询直接回答；修改类返回计划卡片（改哪些对象/操作 + 风险）→ 专家确认 → 应用落新版本。
const MATERIAL_ACCEPT = '.xlsx,.xls,.csv,.md,.markdown,.txt,.docx,.doc,.pdf';
// 进度经事件流推送后不再需要秒级轮询，这里只兜底事件连接中断的情况
const LONG_TASK_FALLBACK_REFRESH_MS = 15_000;
const timedTurn = (turn) => ({ ...turn, createdAt: new Date().toISOString() });

export function shouldOpenWarningReview(error) {
  return error?.data?.code === 'PACKAGE_WARNINGS_UNREVIEWED';
}

export function interactionBusyState(purpose) {
  if (purpose === 'controlled_repair') return 'repair_knowledge';
  if (purpose === 'script_contract') return 'script_generation';
  if (purpose === 'script_generation') return 'script_apply';
  return 'interaction';
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
  pendingInteraction,
  flowchartDraftState,
  candidateRevisionPanel,
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
  const [taskAdjustment, setTaskAdjustment] = useState(null);
  const [taskEditConfig, setTaskEditConfig] = useState(null);
  const [validationDeferral, setValidationDeferral] = useState(null);
  const [completionBlocker, setCompletionBlocker] = useState(null);
  // 助手分析在服务端后台跑，本地 busy 只覆盖提交那一瞬；两者合成对外统一的忙碌态，
  // 这样刷新页面或换标签页也能继续看到“正在分析”。
  const chatRunning = useMemo(() => isPackageChatRunning(authoring), [authoring]);
  const busy = localBusy || (chatRunning ? 'plan' : '');
  const sec = useElapsed(!!busy);
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

  const refreshConstruction = async () => {
    await onProjectChanged?.();
  };

  const handleStructuredAction = async (action) => {
    if (analysisRunning && ['analyze_materials', 'review_package'].includes(action.id)) return;
    if (flowchartDraftState?.dirty && !FLOWCHART_READ_ONLY_ACTIONS.has(action.id)) {
      const allowed = confirmFlowchartMutation
        ? confirmFlowchartMutation()
        : confirmFlowchartNavigation(true);
      if (!allowed) return;
    }
    if (action.id === 'open_package_file' && action.path) {
      window.dispatchEvent(new CustomEvent('mw-package-file-select', {
        detail: { projectId, path: action.path, target: action.path },
      }));
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
    if (action.id === 'defer_validation_errors') {
      if (!busy) setValidationDeferral({ errorCount: Math.max(1, latestValidationErrorCount) });
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
    if (action.id === 'open_scenario_review') {
      setCompletionBlocker(null);
      onNavigate?.('scenario');
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
    // 评测缺陷交由优化更新处理：它才能从失败用例反推资源改动并过防过拟合校验
    if (action.id === 'open_optimization') {
      window.dispatchEvent(new CustomEvent('mw-open-evaluation-view', {
        detail: { projectId, view: 'optimize' },
      }));
      return;
    }
    if (action.id === 'explain_goal') {
      window.requestAnimationFrame?.(() => {
        bodyRef.current?.querySelector?.('[data-question-id="goal_text"]')?.focus?.();
      });
      return;
    }
    const prefills = {
      review_materials: '请回顾已上传资料的分析结果和待确认问题。',
      review_package: '请检查当前知识包基线，并告诉我需要优先补充什么。',
      view_validation: '请说明当前知识包的校验情况和需要处理的问题。',
    };
    if (prefills[action.id]) setInput(prefills[action.id]);
    if (action.id === 'upload_materials') {
      if (!busy) materialInputRef.current?.click();
      return;
    }
    const reviewPathByAction = {
      review_process_files: 'diagrams/index.md',
      review_knowledge_files: 'index.md',
    };
    if (reviewPathByAction[action.id]) {
      window.dispatchEvent(new CustomEvent('mw-package-file-select', {
        detail: { projectId, path: reviewPathByAction[action.id] },
      }));
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
          }[milestoneId] ?? '当前里程碑';
          setTurns((items) => items.concat([timedTurn({
            role: 'assistant',
            text: `${confirmedLabel}已经确认，可以继续当前建模任务。`,
          })]));
          const navigationTarget = milestoneNavigationTarget(milestoneId);
          if (navigationTarget) onNavigate?.(navigationTarget);
          return;
        }
        if (!Number.isInteger(aiConstruction?.revision)) throw new Error('建模状态尚未加载，请刷新后重试');
        const note = {
          materials_ready: '确认当前建模资料范围',
          goal_confirmed: '确认当前建模目标说明',
          process_confirmed: '确认当前业务流程',
          knowledge_confirmed: '确认当前知识结构',
          deterministic_confirmed: '确认当前确定性能力',
          construction_completed: '确认完成 AI 辅助构建',
        }[milestoneId];
        await confirmAiConstructionMilestone(
          projectId,
          milestoneId,
          aiConstruction.revision,
          note,
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
      // 被拦下时后端会声明可执行的下一步，渲染成卡片而不是一句无出口的错误文本
      const blocked = completionBlockerMessage(error);
      if (blocked) {
        setCompletionBlocker(blocked);
      } else {
        setTurns((items) => items.concat([timedTurn({
          role: 'assistant',
          text: `${action.id === 'analyze_materials' ? '资料分析' : '里程碑确认'}失败：${error instanceof Error ? error.message : String(error)}`,
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
      if (result.result?.version || result.revision) {
        window.dispatchEvent(new CustomEvent('mw-package-updated'));
      }
      await refreshConstruction();
      return result;
    } catch (error) {
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
          <AiConstructionMessageCard key={`structured-${entry.item.id}`} message={entry.item}
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
              flowchartDraftState={flowchartDraftState}
              onSubmit={submitPendingInteraction}
            />
          </div>
        )}
        {busy && (
          <div className="pchat-msg assistant">
            <div className="bubble"><span className="spinner" /> {{
              plan: '正在分析', apply: '正在修改', upload: '正在上传建模资料',
              analyze: '正在分析建模资料', confirm: '正在确认当前里程碑',
              interaction: '正在提交建模回答',
              generate_knowledge: `正在${currentTask?.title ?? '生成知识结构'} · ${etaText(aiConstruction?.progress?.eta)}`,
              repair_knowledge: `正在${String(currentTask?.title ?? '修复知识包').replaceAll('受控修复', '逐项修复')} · ${etaText(aiConstruction?.progress?.eta)}`,
              defer_validation: '正在记录暂缓决定并准备进入验证',
              script_generation: `正在生成脚本变更方案 · ${etaText(aiConstruction?.progress?.eta)}`,
              script_apply: `正在应用并校验确定性脚本 · ${etaText(aiConstruction?.progress?.eta)}`,
              task_preview: '正在分析任务调整影响', task_adjustment: '正在应用任务调整',
              task_edit: '正在应用任务图变更',
            }[busy] ?? '正在处理'} · {sec} 秒</div>
          </div>
        )}
      </div>
      <div className="pchat-input">
        <textarea rows={2} value={input} placeholder="咨询知识包，或说明要新增、修改的内容"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="btn btn-p" disabled={!!busy || !input.trim()} onClick={send}>发送</button>
      </div>
    </div>
  );
}
