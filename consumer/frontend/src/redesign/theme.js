// 主题配色：L3 个人偏好，存浏览器本地，挂在 <html data-mwr-theme=...>。
// 色板定义见 redesign.css 的 “主题配色” 段。
const KEY = 'mw_pref_theme';

export const THEMES = [
  { id: 'default', label: '靛蓝（默认）', color: '#2a78d6' },
  { id: 'violet', label: '晨紫', color: '#7048c8' },
  { id: 'teal', label: '青瓷', color: '#0f9d8a' },
  { id: 'orange', label: '暖橙', color: '#d97a26' },
  { id: 'graphite', label: '石墨', color: '#3f4854' },
];

export function getTheme() {
  try {
    const t = localStorage.getItem(KEY);
    return THEMES.some((x) => x.id === t) ? t : 'default';
  } catch {
    return 'default';
  }
}

export function applyTheme(id) {
  const theme = THEMES.some((x) => x.id === id) ? id : 'default';
  if (theme === 'default') delete document.documentElement.dataset.mwrTheme;
  else document.documentElement.dataset.mwrTheme = theme;
  try { localStorage.setItem(KEY, theme); } catch { /* ignore */ }
  return theme;
}
