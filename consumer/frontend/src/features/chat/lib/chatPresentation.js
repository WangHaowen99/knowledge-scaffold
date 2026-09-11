// 对话展示相关的纯工具函数：侧边栏历史列表、消息线程与 Trace 共用。

export function groupSessions(sessions, search) {
  const now = new Date();
  const today = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  const yesterday = today - 86400000;
  const buckets = { 已固定: [], 今天: [], 昨天: [], 更早: [] };
  for (const session of sessions) {
    if (
      search &&
      !(session.title || '').toLowerCase().includes(search.toLowerCase())
    ) {
      continue;
    }
    if (session?.metadata?.pinned) {
      buckets['已固定'].push(session);
      continue;
    }
    const timestamp =
      Date.parse(session.updated_at || session.created_at || '') || 0;
    if (timestamp >= today) {
      buckets['今天'].push(session);
    } else if (timestamp >= yesterday) {
      buckets['昨天'].push(session);
    } else {
      buckets['更早'].push(session);
    }
  }
  return Object.entries(buckets).filter(([, items]) => items.length);
}

export function formatTime(timestamp) {
  if (!timestamp) {
    return '';
  }
  const date =
    typeof timestamp === 'number' ?
      new Date(timestamp) :
      new Date(String(timestamp));
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const hours = `${date.getHours()}`.padStart(2, '0');
  const minutes = `${date.getMinutes()}`.padStart(2, '0');
  const seconds = `${date.getSeconds()}`.padStart(2, '0');
  return sameDay ?
    `${hours}:${minutes}:${seconds}` :
    `${date.getMonth() + 1}-${date.getDate()} ${hours}:${minutes}:${seconds}`;
}

export function truncate(value, length) {
  const text = String(value || '')
    .replace(/\s+/g, ' ')
    .trim();
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

export const formatDurationMs = (value) => {
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) {
    return '';
  }
  if (milliseconds < 1000) {
    return `${Math.round(milliseconds)} ms`;
  }
  if (milliseconds < 60_000) {
    return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 2 : 1)} s`;
  }
  const minutes = Math.floor(milliseconds / 60_000);
  return `${minutes}m ${Math.round((milliseconds % 60_000) / 1000)}s`;
};

export const businessDataTraceLabel = (node) => {
  const action = (
    node?.input?.action ||
    node?.parsedOutput?.action ||
    node?.outputProjection?.action ||
    ''
  )
    .toString()
    .trim()
    .toLowerCase();
  return action === 'lookup_values' ? '查询字段值域' : '查询业务数据';
};
