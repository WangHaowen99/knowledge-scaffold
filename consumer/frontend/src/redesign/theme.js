// 主题配色：L3 个人偏好，存浏览器本地，挂在 <html data-mwr-theme=...>。
// 色板定义见 redesign.css 的 “主题配色” 段。
const KEY = 'mw_pref_theme';
const APPEARANCE_KEY = 'mw_pref_appearance';
const BRAND_FAVICON_FILTERS = {
  violet: 'hue-rotate(48deg) saturate(1.08)',
  teal: 'hue-rotate(-38deg) saturate(.92)',
  orange: 'hue-rotate(174deg) saturate(1.22)',
  graphite: 'grayscale(1) brightness(.78) contrast(1.16)',
};

export const THEMES = [
  { id: 'default', label: '靛蓝（默认）', color: '#256fd0' },
  { id: 'violet', label: '晨紫', color: '#7048c8' },
  { id: 'teal', label: '青瓷', color: '#0f8f7f' },
  { id: 'orange', label: '暖橙', color: '#cf7020' },
  { id: 'graphite', label: '石墨', color: '#3f4854' },
];

export const APPEARANCES = [
  { id: 'system', label: '跟随系统' },
  { id: 'light', label: '浅色' },
  { id: 'dark', label: '深色' },
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
  syncBrandFavicon(theme);
  return theme;
}

function syncBrandFavicon(theme) {
  if (typeof document.querySelector !== 'function') return;
  const link = document.querySelector('link[rel="icon"]');
  if (!link) return;
  if (theme === 'default' || typeof Image === 'undefined') {
    link.href = '/brand-mark.png';
    return;
  }
  const image = new Image();
  image.onload = () => {
    const currentTheme = document.documentElement.dataset.mwrTheme || 'default';
    if (currentTheme !== theme) return;
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 64;
    const context = canvas.getContext('2d');
    if (!context) return;
    context.filter = BRAND_FAVICON_FILTERS[theme] || 'none';
    context.drawImage(image, 0, 0, 64, 64);
    link.href = canvas.toDataURL('image/png');
  };
  image.src = '/brand-mark.png';
}

export function getAppearance() {
  try {
    const value = localStorage.getItem(APPEARANCE_KEY);
    return APPEARANCES.some((item) => item.id === value) ? value : 'system';
  } catch {
    return 'system';
  }
}

export function applyAppearance(id) {
  const appearance = APPEARANCES.some((item) => item.id === id) ? id : 'system';
  const resolved = appearance === 'system'
    ? (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : appearance;
  document.documentElement.dataset.mwrMode = resolved;
  try { localStorage.setItem(APPEARANCE_KEY, appearance); } catch { /* ignore */ }
  return appearance;
}
