import React, { useState } from 'react';
import MarkdownContent from './MarkdownContent.jsx';
import { formatMessageTimestamp } from './messageTimestamp.js';
import { effectiveTaskPlanTasks } from './taskPlanStatus.js';

function friendlyRepairText(value) {
  return String(value ?? '')
    .replaceAll('受控修复器', '知识包修复助手')
    .replaceAll('受控修复', '逐项修复')
    .replaceAll('自动修复仍有语义争议，正在准备专家复核', '这份修改还需要你确认后才能使用')
    .replaceAll('正在准备专家复核', '请确认这份修改')
    .replace(
      /候选修订的阻断错误[\s\S]*/u,
      '系统已经生成一份修改方案，但还有内容需要你做业务判断。请查看下面的“需要你确认的修改”卡片，再选择确认使用、告诉助手怎么改，或者不使用这份修改。',
    );
}

function etaLabel(eta) {
  if (!eta || eta.minMinutes == null || eta.maxMinutes == null) return '剩余时长待规划';
  if (eta.minMinutes === 0 && eta.maxMinutes === 0) return '当前步骤已完成';
  if (eta.minMinutes === eta.maxMinutes) return `预计 ${eta.minMinutes} 分钟`;
  return `预计 ${eta.minMinutes}–${eta.maxMinutes} 分钟`;
}

function StatusBlock({ block }) {
  const progress = Number.isFinite(Number(block.progress))
    ? Math.max(0, Math.min(100, Math.round(Number(block.progress))))
    : null;
  return (
    <section className="ac-message-status">
      <div className="ac-message-status-head">
        <strong>{friendlyRepairText(block.title)}</strong>
        {progress != null && <span>{progress}%</span>}
      </div>
      {block.summary && <p>{friendlyRepairText(block.summary)}</p>}
      {progress != null && (
        <div className="ac-message-progress" aria-label={`建模进度 ${progress}%`}>
          <span style={{ width: `${progress}%` }} />
        </div>
      )}
      <div className="ac-message-eta">{etaLabel(block.eta)}</div>
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
        {!actionsEnabled && <span>历史步骤</span>}
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

const VALIDATION_SCOPE_LABEL = { node: '当前产物', incremental: '本次变更', milestone: '当前里程碑', complete: '完整知识包', release: '发布候选' };
const FINDING_LABEL = { error: '错误', warning: '警告', hint: '提示' };
function ValidationBlock({ block, onAction }) {
  const counts = block.counts ?? {};
  return <section className={`ac-message-validation${counts.errors ? ' has-errors' : ''}`}>
    <div className="ac-message-validation-head"><div><strong>{block.title}</strong><span>{VALIDATION_SCOPE_LABEL[block.scope] ?? '知识包校验'}</span></div>
      <div className="ac-message-validation-counts"><span className="error">错误 {counts.errors ?? 0}</span><span className="warning">警告 {counts.warnings ?? 0}</span><span className="hint">提示 {counts.hints ?? 0}</span></div>
    </div>
    {(block.findings ?? []).length > 0 && <div className="ac-message-finding-list">{block.findings.map((finding, index) => <article key={`${finding.title}-${index}`} className={finding.severity}>
      <div><span>{FINDING_LABEL[finding.severity] ?? '发现'}</span><strong>{finding.title}</strong></div><p>{finding.summary}</p>{finding.target && <code>{finding.target}</code>}
      <OpenFileButton path={finding.target} label="定位问题" onAction={onAction} />
    </article>)}</div>}
  </section>;
}

const TASK_STATUS_LABEL = { pending: '待规划', ready: '可开始', running: '进行中', waiting_user: '等待回答', waiting_confirmation: '待确认', completed: '已完成', failed: '需重试', skipped: '已跳过', stale: '待确认', cancelled: '已取消' };
function taskActions(task) {
  if (['package_validation', 'package_repair'].includes(task.kind)
    && ['failed', 'waiting_user', 'waiting_confirmation'].includes(task.status)) {
    return [
      ...(task.status === 'failed'
        ? [{ directAction: 'repair_knowledge_structure', label: '重新修复' }]
        : []),
      { directAction: 'defer_validation_errors', label: '暂缓修复，进入验证' },
    ];
  }
  if (task.status === 'completed') return [{ action: 'redo', label: '重做' }];
  if (['skipped', 'stale'].includes(task.status)) return [{ action: 'resume', label: '恢复' }];
  if (['pending', 'ready', 'failed', 'waiting_user', 'waiting_confirmation'].includes(task.status)) {
    return [
      { action: 'defer', label: '暂缓' },
      ...(!task.required ? [{ action: 'skip', label: '跳过' }] : []),
    ];
  }
  return [];
}
function TaskPlanBlock({ block, onAction }) {
  const tasks = effectiveTaskPlanTasks(block.tasks);
  const [selected, setSelected] = useState([]);
  const editable = (task) => ['pending', 'ready', 'failed', 'waiting_user', 'waiting_confirmation'].includes(task.status);
  const selectedTasks = tasks.filter((task) => selected.includes(task.id));
  const toggle = (taskId) => setSelected((items) => items.includes(taskId)
    ? items.filter((id) => id !== taskId) : items.concat(taskId));
  return <section className="ac-message-task-plan"><div className="ac-message-task-plan-head"><strong>{friendlyRepairText(block.title)}</strong><span>{tasks.filter((task) => ['completed', 'skipped'].includes(task.status)).length}/{tasks.length}</span></div>
    {onAction && <div className="ac-message-task-edit-toolbar">
      <button type="button" data-action="open_task_add" onClick={() => onAction({
        id: 'open_task_add', mode: 'add', milestoneId: block.milestoneId,
      })}>新增任务</button>
      <button type="button" data-action="open_task_merge" disabled={selectedTasks.length < 2}
        onClick={() => onAction({ id: 'open_task_merge', mode: 'merge', tasks: selectedTasks })}>合并所选</button>
      <span>选中 {selectedTasks.length} 项</span>
    </div>}
    <div className="ac-message-task-list">{tasks.map((task, index) => <article key={task.id} className={`${task.status}${onAction ? ' task-editable-row' : ''}`}>
      {onAction && editable(task) && <input type="checkbox" aria-label={`选择任务 ${friendlyRepairText(task.title)}`}
        checked={selected.includes(task.id)} onChange={() => toggle(task.id)} />}
      {onAction && !editable(task) && <span className="ac-message-task-select-placeholder" />}
      <span className="ac-message-task-index">{index + 1}</span><div><div><strong>{friendlyRepairText(task.title)}</strong>{task.required && <em>必选</em>}</div>{task.reason && <p>{friendlyRepairText(task.reason)}</p>}
        {onAction && taskActions(task).length > 0 && <div className="ac-message-task-actions">{taskActions(task).map((item) => <button
          key={item.directAction ?? item.action} type="button"
          data-action={item.directAction ?? 'preview_task_adjustment'}
          onClick={() => onAction(item.directAction
            ? { id: item.directAction }
            : { id: 'preview_task_adjustment', taskId: task.id, adjustment: item.action })}>{item.label}</button>)}
          {editable(task) && <>
            <button type="button" disabled={index === 0} data-action="open_task_reorder"
              onClick={() => onAction({ id: 'open_task_reorder', mode: 'reorder', edit: {
                action: 'reorder', taskId: task.id, beforeTaskId: tasks[index - 1]?.id,
              } })}>前移</button>
            <button type="button" disabled={index === tasks.length - 1} data-action="open_task_reorder"
              onClick={() => onAction({ id: 'open_task_reorder', mode: 'reorder', edit: {
                action: 'reorder', taskId: task.id, ...(tasks[index + 2]?.id ? { beforeTaskId: tasks[index + 2].id } : {}),
              } })}>后移</button>
            <button type="button" data-action="open_task_split"
              onClick={() => onAction({ id: 'open_task_split', mode: 'split', task })}>拆分</button>
          </>}</div>}</div>
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
      <p>{change.description}</p>
    </article>)}</div>
    {(block.risks ?? []).length > 0 && <div className="ac-message-change-risks"><strong>影响与风险</strong>
      <ul>{block.risks.map((risk, index) => <li key={`${risk}-${index}`}>{risk}</li>)}</ul></div>}
  </section>;
}

function MessageBlock({ block, onAction, actionsEnabled, disabledActionIds, hiddenActionIds }) {
  if (block?.type === 'status') return <StatusBlock block={block} />;
  if (block?.type === 'text') return <div className="ac-message-text"><MarkdownContent>{friendlyRepairText(block.text)}</MarkdownContent></div>;
  if (block?.type === 'evidence') return <EvidenceBlock block={block} onAction={onAction} />;
  if (block?.type === 'validation') return <ValidationBlock block={block} onAction={onAction} />;
  if (block?.type === 'task_plan') return <TaskPlanBlock block={block} onAction={onAction} />;
  if (block?.type === 'change_plan') return <ChangePlanBlock block={block} />;
  if (block?.type === 'next_actions') return <NextActionsBlock block={block} onAction={onAction}
    actionsEnabled={actionsEnabled} disabledActionIds={disabledActionIds}
    hiddenActionIds={hiddenActionIds} />;
  return <div className="ac-message-unsupported">暂不支持的消息内容</div>;
}

export default function AiConstructionMessageCard({
  message, onAction, actionsEnabled = true, disabledActionIds = [], hiddenActionIds = [],
}) {
  const timestamp = formatMessageTimestamp(message?.createdAt);
  return (
    <article className={`ac-message-card ${message?.role ?? 'assistant'}`}>
      {(message?.blocks ?? []).map((block, index) => (
        <MessageBlock key={`${message?.id ?? 'message'}-${index}`} block={block} onAction={onAction}
          actionsEnabled={actionsEnabled} disabledActionIds={disabledActionIds}
          hiddenActionIds={hiddenActionIds} />
      ))}
      {timestamp && <time className="ac-message-time" dateTime={message.createdAt}>{timestamp}</time>}
    </article>
  );
}
