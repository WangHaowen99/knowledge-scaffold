// 对话相关的纯工具函数：侧边栏历史列表与消息线程共用。

export function groupSessions(sessions, search) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterday = today - 86400000;
  const buckets = { 已固定: [], 今天: [], 昨天: [], 更早: [] };
  for (const s of sessions) {
    if (search && !(s.title || '').toLowerCase().includes(search.toLowerCase())) continue;
    if (s?.metadata?.pinned) {
      buckets['已固定'].push(s);
      continue;
    }
    const ts = Date.parse(s.updated_at || s.created_at || '') || 0;
    if (ts >= today) buckets['今天'].push(s);
    else if (ts >= yesterday) buckets['昨天'].push(s);
    else buckets['更早'].push(s);
  }
  return Object.entries(buckets).filter(([, items]) => items.length);
}

export function formatTime(ts) {
  if (!ts) return '';
  const d = typeof ts === 'number' ? new Date(ts) : new Date(String(ts));
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hh = `${d.getHours()}`.padStart(2, '0');
  const mm = `${d.getMinutes()}`.padStart(2, '0');
  return sameDay ? `${hh}:${mm}` : `${d.getMonth() + 1}-${d.getDate()} ${hh}:${mm}`;
}

export function truncate(s, n) {
  const t = String(s || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? `${t.slice(0, n)}…` : t;
}
