const STATUS_PRIORITY = [
  'running',
  'waiting_user',
  'waiting_confirmation',
  'failed',
  'ready',
  'pending',
  'skipped',
  'completed',
];

const STATUS_DISPLAY = {
  running: { label: '运行中', tone: 'running' },
  waiting_user: { label: '等待回答', tone: 'waiting' },
  waiting_confirmation: { label: '等待确认', tone: 'waiting' },
  failed: { label: '需要处理', tone: 'error' },
  ready: { label: '可开始', tone: 'ready' },
  pending: { label: '待规划', tone: 'idle' },
  skipped: { label: '已跳过', tone: 'idle' },
  completed: { label: '已完成', tone: 'complete' },
};

export function effectiveTaskPlanTasks(tasks = []) {
  return (Array.isArray(tasks) ? tasks : []).filter(
    (task) => !['stale', 'cancelled'].includes(task?.status),
  );
}

export function summarizeTaskPlan(tasks = []) {
  const visible = effectiveTaskPlanTasks(tasks);
  const completed = visible.filter((task) => ['completed', 'skipped'].includes(task.status)).length;
  if (visible.length > 0 && visible.every((task) => ['completed', 'skipped'].includes(task.status))) {
    return {
      completed,
      total: visible.length,
      status: 'completed',
      statusLabel: STATUS_DISPLAY.completed.label,
      tone: STATUS_DISPLAY.completed.tone,
      title: '当前阶段已完成',
    };
  }
  const current = STATUS_PRIORITY
    .map((status) => visible.find((task) => task.status === status))
    .find(Boolean);
  const status = current?.status ?? 'completed';
  const display = STATUS_DISPLAY[status] ?? STATUS_DISPLAY.pending;
  return {
    completed,
    total: visible.length,
    status,
    statusLabel: display.label,
    tone: display.tone,
    title: current?.title ?? (visible.length ? '当前阶段已完成' : '暂无任务'),
  };
}
