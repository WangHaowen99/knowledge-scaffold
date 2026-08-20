import React from 'react';
import MarkdownContent from './MarkdownContent.jsx';
import { formatMessageTimestamp } from './messageTimestamp.js';
import { effectiveTaskPlanTasks } from './taskPlanStatus.js';
import RunObservabilityPanel from './RunObservabilityPanel.jsx';

/**
 * 术语已经在后端统一成「逐项修复 / 知识包修复助手」，这里只兜历史消息——
 * 库里存量消息是旧词，改不了。新文案不要再依赖这张表：它是个白名单，
 * 漏一个词就会让用户同时看到两套叫法。
 */
function friendlyRepairText(value, hideNonBlocking = false) {
  const text = String(value ?? '')
    .replace(/\s*·\s*警告\s+\d+\s*·\s*提示\s+\d+/gu, hideNonBlocking ? '' : '$&');
  return text
    .replaceAll('受控修复器', '知识包修复助手')
    .replaceAll('受控修复', '逐项修复')
    .replaceAll('自动修复仍有语义争议，正在准备专家复核', '这份修改还需要你确认后才能使用')
    .replaceAll('正在准备专家复核', '请确认这份修改')
    .replace(
      /候选修订的阻断错误[\s\S]*/u,
      '系统已经生成一份修改方案，但还有内容需要你做业务判断。请查看下面的“需要你确认的修改”卡片，再选择确认使用、告诉助手怎么改，或者不使用这份修改。',
    );
}

export function resolveLegacyFindingAction(message, action) {
  if (action?.id !== 'review_knowledge_files' || action.path) return action;
  const status = (message?.blocks ?? []).find((block) => (
    block?.type === 'status' && String(block.title ?? '').startsWith('处理校验发现：')
  ));
  const summary = String(status?.summary ?? '');
  const separator = summary.indexOf('·');
  const target = separator >= 0 ? summary.slice(separator + 1).trim() : '';
  if (!target || !/^[A-Za-z0-9_.:-]+(\/[A-Za-z0-9_.:-]+)*$/.test(target)) return action;
  return { ...action, id: 'open_package_file', path: target };
}

function etaLabel(eta) {
  if (!eta || eta.minMinutes == null || eta.maxMinutes == null) return '剩余时长待规划';
  if (eta.minMinutes === 0 && eta.maxMinutes === 0) return '当前步骤已完成';
  if (eta.minMinutes === eta.maxMinutes) return `预计 ${eta.minMinutes} 分钟`;
  return `预计 ${eta.minMinutes}–${eta.maxMinutes} 分钟`;
}

// 里程碑顺序是固定的，前端据此给百分比一个参照物，不必让后端在 33 处状态块里都带上
const MILESTONE_ORDER = [
  'goal_confirmed', 'materials_ready', 'process_confirmed',
  'knowledge_confirmed', 'deterministic_confirmed', 'construction_completed',
];
export function milestonePosition(milestoneId) {
  const index = MILESTONE_ORDER.indexOf(String(milestoneId ?? ''));
  return index < 0 ? null : { index: index + 1, total: MILESTONE_ORDER.length };
}

function completedTitle(title, taskTitle) {
  const normalized = friendlyRepairText(title).replace(/^正在/u, '').trim();
  if (normalized && normalized !== friendlyRepairText(title)) return `${normalized}已完成`;
  return `${friendlyRepairText(taskTitle || title || '当前任务')}已完成`;
}

function StatusBlock({
  block, hideNonBlocking = false, runtime = null, task = null, tracksTask = false,
  stageProgress = null, taskProgressMessage = false,
}) {
  const taskCompleted = tracksTask && ['completed', 'skipped'].includes(task?.status);
  const runtimeProgress = Number.isFinite(Number(runtime?.job?.progress))
    ? Math.max(0, Math.min(100, Math.round(Number(runtime.job.progress))))
    : null;
  const reportedTaskProgress = taskProgressMessage && Number.isFinite(Number(block.progress))
    ? Math.max(0, Math.min(100, Math.round(Number(block.progress))))
    : null;
  const progress = taskCompleted ? 100 : (runtime ? runtimeProgress : reportedTaskProgress);
  const title = taskCompleted
    ? completedTitle(block.title, task?.title)
    : (runtime?.title ?? friendlyRepairText(block.title));
  const statusLabel = taskCompleted ? '已完成' : runtime?.statusLabel;
  return (
    <section className={`ac-message-status${runtime ? ' has-runtime' : ''}`}>
      <div className="ac-message-status-head">
        <strong>{title}</strong>
        {tracksTask && stageProgress?.total > 0 && (
          <span className="ac-message-stage-progress">阶段 {stageProgress.completed}/{stageProgress.total}</span>
        )}
        {statusLabel && <span className="ac-message-runtime-status">{statusLabel}</span>}
        {progress != null && <span className="ac-message-task-progress-label">任务 {progress}%</span>}
      </div>
      {(runtime?.summary || block.summary) && (
        <p>{runtime?.summary ?? friendlyRepairText(block.summary, hideNonBlocking)}</p>
      )}
      {progress != null && (
        <div className="ac-message-progress" title="当前任务进度"
          aria-label={`当前任务进度 ${progress}%`}>
          <span style={{ width: `${progress}%` }} />
        </div>
      )}
      {!runtime && tracksTask && !taskCompleted && <div className="ac-message-eta">{etaLabel(block.eta)}</div>}
      {runtime && <RunObservabilityPanel {...runtime} integrated />}
    </section>
  );
}

function NextActionsBlock({
  block, onAction, actionsEnabled = true, disabledActionIds = [], hiddenActionIds = [],
}) {
  const actions = (block.actions ?? []).filter((action) => !hiddenActionIds.includes(action.id));
  if (!actions.length) return null;
  return (
    <section className={`ac-message-actions${actionsEnabled ? '' : ' is-history'}`}>
      {block.title && <div className="ac-message-actions-title">{block.title}
        {!actionsEnabled && (
          <span title="建模已经走到后面的步骤，这里的选项基于当时的状态，继续操作请看最新一条消息">
            历史步骤 · 已不可操作
          </span>
        )}
      </div>}
      <div className="ac-message-action-list">
        {actions.map((action) => {
          const actionEnabled = actionsEnabled && !disabledActionIds.includes(action.id);
          return <button
            key={action.id}
            type="button"
            data-action={action.id}
            className={`ac-message-action ${action.style ?? 'secondary'}`}
            disabled={!actionEnabled}
            // 窄栏下说明文字会被收成两行，title 保证完整内容仍可读
            title={action.description ? friendlyRepairText(action.description) : undefined}
            onClick={() => { if (actionEnabled) onAction?.(action); }}
          >
            <strong>{friendlyRepairText(action.label)}</strong>
            {action.description && <span>{friendlyRepairText(action.description)}</span>}
          </button>;
        })}
      </div>
    </section>
  );
}

function OpenFileButton({ path, label, onAction }) {
  if (!path || !onAction) return null;
  return <button type="button" className="ac-message-open-file" data-action="open_package_file" title={path}
    onClick={() => onAction({ id: 'open_package_file', path })}>{label}</button>;
}

function EvidenceBlock({ block, onAction }) {
  return <section className="ac-message-evidence"><div className="ac-message-evidence-title">{block.title}</div>
    <div className="ac-message-evidence-list">{(block.items ?? []).map((item, index) => <article key={`${item.source}-${index}`}>
      <div className="ac-message-evidence-source"><strong>{item.source}</strong>{item.locator && <span>{item.locator}</span>}</div><p>{item.summary}</p>
      <OpenFileButton path={item.path} label="查看依据" onAction={onAction} />
    </article>)}</div></section>;
}

const VALIDATION_SCOPE_LABEL = { node: '当前产物', incremental: '本次变更', milestone: '当前阶段', complete: '完整知识包', release: '发布候选' };
const FINDING_LABEL = { error: '错误', warning: '警告', hint: '提示' };
function ValidationBlock({ block, onAction }) {
  const counts = block.counts ?? {};
  const hideNonBlocking = block.scope !== 'release';
  const findings = hideNonBlocking
    ? (block.findings ?? []).filter((finding) => finding.severity === 'error')
    : (block.findings ?? []);
  if (hideNonBlocking && !counts.errors && !findings.length) return null;
  return <section className={`ac-message-validation${counts.errors ? ' has-errors' : ''}`}>
    <div className="ac-message-validation-head"><div><strong>{block.title}</strong><span>{VALIDATION_SCOPE_LABEL[block.scope] ?? '知识包校验'}</span></div>
      <div className="ac-message-validation-counts"><span className="error">错误 {counts.errors ?? 0}</span>
        {!hideNonBlocking && <><span className="warning">警告 {counts.warnings ?? 0}</span><span className="hint">提示 {counts.hints ?? 0}</span></>}
      </div>
    </div>
    {findings.length > 0 && <div className="ac-message-finding-list">{findings.map((finding, index) => <article key={`${finding.title}-${index}`} className={finding.severity}>
      <div><span>{FINDING_LABEL[finding.severity] ?? '发现'}</span><strong>{finding.title}</strong></div><p>{finding.summary}</p>{finding.target && <code>{finding.target}</code>}
      <OpenFileButton path={finding.target} label="定位问题" onAction={onAction} />
    </article>)}</div>}
  </section>;
}

const TASK_STATUS_LABEL = { pending: '待规划', ready: '可开始', running: '进行中', waiting_user: '等待回答', waiting_confirmation: '待确认', completed: '已完成', failed: '需重试', skipped: '已跳过', stale: '待确认', cancelled: '已取消' };
function TaskPlanBlock({ block }) {
  const tasks = effectiveTaskPlanTasks(block.tasks);
  return <section className="ac-message-task-plan"><div className="ac-message-task-plan-head"><strong>{friendlyRepairText(block.title)}</strong><span>{tasks.filter((task) => ['completed', 'skipped'].includes(task.status)).length}/{tasks.length}</span></div>
    <div className="ac-message-task-list">{tasks.map((task, index) => <article key={task.id} className={task.status}>
      <span className="ac-message-task-index">{index + 1}</span><div><div><strong>{friendlyRepairText(task.title)}</strong>{task.required && <em>必选</em>}</div>{task.reason && <p>{friendlyRepairText(task.reason)}</p>}</div>
      <span className="ac-message-task-status">{TASK_STATUS_LABEL[task.status] ?? task.status}</span>
    </article>)}</div>{block.note && <div className="ac-message-task-note">{block.note}</div>}
  </section>;
}

const CHANGE_STATUS_LABEL = {
  pending: '待确认', approved: '已确认，执行中', applied: '已应用', failed: '执行失败，可重试', rejected: '已取消',
};
const CHANGE_ACTION_LABEL = { add: '新增', modify: '修改', remove: '删除' };
const CHANGE_TARGET_LABEL = { object: '对象', operation: '操作', rule: '规则', metric: '指标', script: '脚本' };
function ChangePlanBlock({ block }) {
  return <section className={`ac-message-change-plan ${block.status ?? 'pending'}`}>
    <div className="ac-message-change-plan-head"><div><strong>{block.title}</strong><p>{block.summary}</p></div>
      <span>{CHANGE_STATUS_LABEL[block.status] ?? block.status}</span></div>
    <div className="ac-message-change-list">{(block.changes ?? []).map((change) => <article key={change.id}>
      <div><span className={`action ${change.action}`}>{CHANGE_ACTION_LABEL[change.action] ?? change.action}</span>
        <strong>{CHANGE_TARGET_LABEL[change.targetKind] ?? change.targetKind} {change.targetKey}</strong></div>
      {/* 修改类先给出当前内容：不看见现在是什么样，「怎么改」无从判断 */}
      {change.current && <div className="ac-message-change-current">
        <span>当前内容</span><pre>{change.current}</pre>
      </div>}
      <p>{change.current ? '计划这样改：' : ''}{change.description}</p>
    </article>)}</div>
    {(block.risks ?? []).length > 0 && <div className="ac-message-change-risks"><strong>影响与风险</strong>
      <ul>{block.risks.map((risk, index) => <li key={`${risk}-${index}`}>{risk}</li>)}</ul></div>}
  </section>;
}

function MessageBlock({
  block, onAction, actionsEnabled, disabledActionIds, hiddenActionIds,
  hideTaskPlan, hideNonBlocking, runtime, task, taskProgressMessage, stageProgress,
}) {
  if (block?.type === 'status') {
    const tracksTask = Boolean(taskProgressMessage || runtime);
    return <StatusBlock block={block} hideNonBlocking={hideNonBlocking} runtime={runtime}
      task={task} tracksTask={tracksTask} stageProgress={stageProgress}
      taskProgressMessage={taskProgressMessage} />;
  }
  if (block?.type === 'text') return <div className="ac-message-text"><MarkdownContent>{friendlyRepairText(block.text, hideNonBlocking)}</MarkdownContent></div>;
  if (block?.type === 'evidence') return <EvidenceBlock block={block} onAction={onAction} />;
  if (block?.type === 'validation') return <ValidationBlock block={block} onAction={onAction} />;
  // 任务计划只在助手栏顶部的状态浮层里展示；对话流里再放一遍既占地方又没有操作价值
  if (block?.type === 'task_plan') return hideTaskPlan ? null : <TaskPlanBlock block={block} />;
  if (block?.type === 'change_plan') return <ChangePlanBlock block={block} />;
  if (block?.type === 'next_actions') return <NextActionsBlock block={block} onAction={onAction}
    actionsEnabled={actionsEnabled} disabledActionIds={disabledActionIds}
    hiddenActionIds={hiddenActionIds} />;
  return <div className="ac-message-unsupported">暂不支持的消息内容</div>;
}

function displayBlocks(message) {
  const blocks = message?.blocks ?? [];
  if (!String(message?.key ?? '').startsWith('chat.reply.')) return blocks;
  return blocks.reduce((merged, block) => {
    const previous = merged.at(-1);
    if (block?.type === 'text' && previous?.type === 'text') {
      merged[merged.length - 1] = {
        ...previous,
        text: `${previous.text}${block.text}`,
      };
    } else {
      merged.push(block);
    }
    return merged;
  }, []);
}

export default function AiConstructionMessageCard({
  message, onAction, actionsEnabled = true, disabledActionIds = [], hiddenActionIds = [],
  hideTaskPlan = false, runtime = null, task = null, stageProgress = null,
}) {
  const findingStatus = (message?.blocks ?? []).find((block) => (
    block?.type === 'status' && String(block.title ?? '').startsWith('处理校验发现：')
  ));
  if (/^(?:警告|提示)\s*·/u.test(String(findingStatus?.summary ?? ''))) return null;
  const timestamp = formatMessageTimestamp(message?.createdAt);
  const blocks = displayBlocks(message);
  const hideNonBlocking = blocks.some((block) => (
    block?.type === 'validation' && block.scope !== 'release'
  )) || blocks.some((block) => (
    block?.type === 'status' && block.milestoneId
  ));
  const handleAction = onAction
    ? (action) => onAction(resolveLegacyFindingAction(message, action))
    : undefined;
  const taskProgressMessage = String(message?.key ?? '').startsWith('progress.task.')
    || String(message?.key ?? '') === 'analysis.progress.current';
  return (
    <article className={`ac-message-card ${message?.role ?? 'assistant'}${runtime ? ' has-job-runtime' : ''}`}>
      {blocks.map((block, index) => (
        <MessageBlock key={`${message?.id ?? 'message'}-${index}`} block={block} onAction={handleAction}
          actionsEnabled={actionsEnabled} disabledActionIds={disabledActionIds}
          hiddenActionIds={hiddenActionIds} hideTaskPlan={hideTaskPlan}
          hideNonBlocking={hideNonBlocking} runtime={runtime} task={task}
          taskProgressMessage={taskProgressMessage} stageProgress={stageProgress} />
      ))}
      {timestamp && <time className="ac-message-time" dateTime={message.createdAt}>{timestamp}</time>}
    </article>
  );
}
