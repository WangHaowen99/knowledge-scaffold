// 页面命令任务（契约 S-19 / S-20 / EP-10 CommandTask）：插件页、资料页只发命令，助手在工作台对话里跑，
// 页面显示进度条和「去对话里看」，跑完结果回到页面列表（详设 5.9.3）。

export const ACTIVE_TASK_STATES = ['queued', 'running', 'waiting_user'];

export const isTaskActive = (task) => !!task && ACTIVE_TASK_STATES.includes(task.state);

// 按 task_id 替换或追加；中间百分比是瞬时事件，状态变化才持久，两种都按最新的合并
export function mergeTask(list, task) {
  if (!task?.task_id) return list;
  const i = list.findIndex((t) => t.task_id === task.task_id);
  if (i < 0) return [...list, task];
  const next = list.slice();
  next[i] = { ...list[i], ...task };
  return next;
}

// 这一页最近发起、还没结束的任务
export function pageTask(list, page) {
  return list
    .filter((t) => isTaskActive(t) && (!page || t.origin?.page === page))
    .sort((a, b) => String(b.started_at || '').localeCompare(String(a.started_at || '')))[0] || null;
}

// 从进行中变成结束的任务（页面据此刷新列表、提示结果）
export function finishedTasks(prev, next) {
  const before = new Map(prev.map((t) => [t.task_id, t]));
  return next.filter((t) => !isTaskActive(t) && isTaskActive(before.get(t.task_id)));
}

const STATE_TEXT = { queued: '排队中，前一步结束后开始', waiting_user: '等你在对话里回答' };

// 进度条第二行：步骤说明（或排队、等回答）+ 页面补充的只读与结果去向说明
export function taskDetail(task, suffix) {
  if (!task) return suffix || '';
  const head = task.state === 'running' ? task.step_label : STATE_TEXT[task.state] || task.step_label;
  return [head, suffix].filter(Boolean).join(' · ');
}

export function taskPercent(task) {
  if (!task || task.percent == null) return null;
  return Math.max(0, Math.min(100, Math.round(task.percent)));
}

// 按钮的悬浮说明（设计稿 mrSend）
export const sendHint = (what) => `发到工作台对话里执行：${what}。页面上显示进度，跑完结果回到这里`;
