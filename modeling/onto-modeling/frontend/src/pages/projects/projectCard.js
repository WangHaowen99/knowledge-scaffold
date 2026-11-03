// 项目卡片的显示规则（纯函数，便于测试）

// 卡片图标与底色：按包名稳定取色；已归档统一灰色
const LOOKS = [
  ['cube', 'linear-gradient(140deg,#60A5FA,#2563EB)'],
  ['gauge', 'linear-gradient(140deg,#FCD34D,#D97706)'],
  ['badge', 'linear-gradient(140deg,#5EEAD4,#0D9488)'],
  ['zap', 'linear-gradient(140deg,#818CF8,#4338CA)'],
  ['bolt', 'linear-gradient(140deg,#7DD3FC,#0284C7)'],
];
const ARCHIVED = ['archive', 'linear-gradient(140deg,#CBD5E1,#64748B)'];

export function projectLook(p) {
  if (p.archived) return { icon: ARCHIVED[0], color: ARCHIVED[1] };
  let h = 3;
  for (const ch of p.package_name || p.id || '') h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const [icon, color] = LOOKS[h % LOOKS.length];
  return { icon, color };
}

// DisplayState → 分段小进度条的格子样式（设计稿 segbar）
const CELL = {
  confirmed: 'ok', pending: 'pending', pending_confirm: 'pending', stale: 'stale', running: 'run', draft: 'draft',
  skipped: 'skip', conflict: 'conflict', needs_review: 'review', auto: 'review',
};
export const cellClass = (s) => CELL[s] || '';

const pad = (n) => String(n).padStart(2, '0');

// 卡片右下角的时间：运行中 / 归档于 10-12 / 5 分钟前 / 昨天 18:20 / 3 天前
export function activityText(p, now = Date.now()) {
  if (p.autobuild && ['running', 'queued', 'pausing'].includes(p.autobuild.state)) return '运行中';
  if (p.archived) {
    const d = new Date(p.archived_at || p.last_activity_at || now);
    return `归档于 ${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  if (!p.last_activity_at) return '';
  const t = Date.parse(p.last_activity_at);
  const min = Math.floor((now - t) / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const d = new Date(t);
  const today = new Date(now);
  const startToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (t >= startToday) return `${Math.floor(min / 60)} 小时前`;
  if (t >= startToday - 86400000) return `昨天 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${Math.max(2, Math.ceil((startToday - t) / 86400000))} 天前`;
}
