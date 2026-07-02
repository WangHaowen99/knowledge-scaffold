const WINDOW_PRESETS = [
  { id: '1h', durationMs: 60 * 60 * 1000 },
  { id: '24h', durationMs: 24 * 60 * 60 * 1000 },
  { id: '7d', durationMs: 7 * 24 * 60 * 60 * 1000 },
];

const PERF_EVENT_LABELS = {
  'run.started': '运行开始',
  'run.finished': '运行完成',
  'run.failed': '运行失败',
  'run.cancelled': '运行取消',
  'run.summary': '运行汇总',
  'agent_loop.started': '智能体循环开始',
  'agent_loop.finished': '智能体循环结束',
  'agent_iteration.prepared': '迭代准备完成',
  'agent_iteration.finished': '迭代完成',
  'llm.started': 'LLM 请求开始',
  'llm.finished': 'LLM 响应完成',
  'llm.failed': 'LLM 请求失败',
  'tool_batch.started': '工具批次开始',
  'tool_batch.finished': '工具批次完成',
  'tool_batch.failed': '工具批次失败',
  'tool.started': '工具调用开始',
  'tool.progress': '工具调用进度',
  'tool.finished': '工具调用完成',
  'runtime_phase.started': '运行阶段开始',
  'runtime_phase.finished': '运行阶段完成',
  'runtime_phase.failed': '运行阶段失败',
};

const PERF_KIND_LABELS = {
  run: '运行',
  run_summary: '运行汇总',
  agent_loop: '智能体循环',
  agent_iteration: '迭代',
  llm_call: 'LLM 调用',
  tool_batch: '工具批次',
  tool_call: '工具调用',
  runtime_phase: '运行阶段',
};

const PERF_COMPONENT_LABELS = {
  orchestrator: '编排器',
  agent_loop: '智能体循环',
  tool_executor: '工具执行器',
  memory: '记忆',
  observability: '观测',
};

const PERF_PHASE_LABELS = {
  stream_events_batch_save: '批量保存流事件',
  chat_session_compact: '会话压缩',
  llm_context_save: '保存 LLM 上下文',
  turn_complete: '轮次收尾',
  run_complete: '运行收尾',
  turn_finished_emit: '发送轮次完成事件',
  memory_append_assistant_message: '写入助手消息记忆',
  memory_append_turn_summary: '写入轮次摘要记忆',
  memory_commit_session: '提交会话记忆',
};

const PERF_CALLER_LABELS = {
  main: '主智能体',
  subagent: '子智能体',
};

const PERF_AGENT_MODE_LABELS = {
  quick: '快速模式',
  thinking: '思考模式',
  multi_agent: '多智能体模式',
};

const PERF_TOOL_LABELS = {
  ontology_context: '本体上下文',
  skill: '技能加载',
  business_data_search: '业务数据查询',
  web_search: '联网搜索',
  execute_script: '执行脚本',
  memory: '记忆写入',
  spawn_sub_agent: '派生子智能体',
};

export function formatDuration(value) {
  const ms = Number(value || 0);
  if (!Number.isFinite(ms) || ms <= 0) return '0 ms';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatMetric(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return '-';
  return new Intl.NumberFormat('zh-CN').format(number);
}

export function formatTimestamp(value) {
  const date = new Date(Number(value || 0));
  if (Number.isNaN(date.getTime())) return '-';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

export function createPerformanceWindowQuery(windowId, endMs = Date.now()) {
  const preset = WINDOW_PRESETS.find((item) => item.id === windowId) || WINDOW_PRESETS[1];
  return new URLSearchParams({
    from: String(endMs - preset.durationMs),
    to: String(endMs),
  }).toString();
}

export function formatPerfEventLabel(value) {
  return formatMappedPerfValue(value, PERF_EVENT_LABELS);
}

export function formatPerfKindLabel(value) {
  return formatMappedPerfValue(value, PERF_KIND_LABELS);
}

export function formatPerfComponentLabel(value) {
  return formatMappedPerfValue(value, PERF_COMPONENT_LABELS);
}

export function formatPerfPhaseLabel(value) {
  return formatMappedPerfValue(value, PERF_PHASE_LABELS);
}

export function formatPerfCallerLabel(value) {
  return formatMappedPerfValue(value, PERF_CALLER_LABELS);
}

export function formatPerfAgentModeLabel(value) {
  return formatMappedPerfValue(value, PERF_AGENT_MODE_LABELS);
}

export function formatPerfToolLabel(value) {
  return formatMappedPerfValue(value, PERF_TOOL_LABELS);
}

function formatMappedPerfValue(value, labels) {
  const raw = String(value || '').trim();
  if (!raw) return '-';
  const direct = labels[raw];
  if (direct) return direct;
  const normalized = raw.toLowerCase();
  if (labels[normalized]) return labels[normalized];
  return humanizePerfKey(raw);
}

function humanizePerfKey(value) {
  return value
    .replace(/^subagent:(.+)$/i, '子智能体 $1')
    .replace(/_/g, ' ')
    .replace(/\./g, ' / ');
}
