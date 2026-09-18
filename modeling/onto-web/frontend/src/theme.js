// 唯一色源：色值与 consumer 服务的 base.css token 对齐。
// 同一份常量两路出口：antd ConfigProvider theme + CSS 变量（供非 antd 组件使用）。

export const ACCENTS = {
  blue: { primary: '#256fd0', hover: '#195cad', strong: '#103f78', muted: '#83b3ec', soft: '#e8f1fd' },
  violet: { primary: '#7048c8', hover: '#5a38a8', strong: '#43287f', muted: '#9a7ce0', soft: '#eae2f9' },
  teal: { primary: '#0f8f7f', hover: '#0c7468', strong: '#095e53', muted: '#4dbfae', soft: '#d5f3ee' },
  orange: { primary: '#cf7020', hover: '#aa5918', strong: '#824313', muted: '#e8a468', soft: '#fbe9d8' },
  graphite: { primary: '#3f4854', hover: '#2f3742', strong: '#20262e', muted: '#6b7684', soft: '#e4e7eb' },
};

export const STATUS = { success: '#16845b', warning: '#b77909', error: '#c53d4b' };

const LIGHT = {
  page: '#f3f5f8', card: '#ffffff', subtle: '#f7f8fa',
  text: '#111827', text2: '#4b5563', text3: '#6f7785',
  border: '#dde2e9', border2: '#ebedf1',
};

const DARK = {
  page: '#0d1118', card: '#151b24', subtle: '#1b222d',
  text: '#edf1f7', text2: '#b9c2cf', text3: '#8f9aaa',
  border: '#303947', border2: '#252e3a',
};

export const FONT_SANS =
  'system-ui, -apple-system, "PingFang SC", "Segoe UI", sans-serif';

export function antdTokens(mode = 'light', accent = 'blue') {
  const a = ACCENTS[accent] || ACCENTS.blue;
  const s = mode === 'dark' ? DARK : LIGHT;
  return {
    colorPrimary: a.primary,
    colorSuccess: STATUS.success,
    colorWarning: STATUS.warning,
    colorError: STATUS.error,
    colorBgLayout: s.page,
    colorBgContainer: s.card,
    colorText: s.text,
    colorTextSecondary: s.text2,
    colorTextTertiary: s.text3,
    colorBorder: s.border,
    colorBorderSecondary: s.border2,
    borderRadius: 8,
    fontFamily: FONT_SANS,
  };
}

export function applyCssVars(mode = 'light', accent = 'blue') {
  const a = ACCENTS[accent] || ACCENTS.blue;
  const s = mode === 'dark' ? DARK : LIGHT;
  const vars = {
    '--color-primary': a.primary,
    '--color-primary-hover': a.hover,
    '--color-primary-strong': a.strong,
    '--color-primary-soft': a.soft,
    '--surface-page': s.page,
    '--surface-card': s.card,
    '--surface-subtle': s.subtle,
    '--text-primary': s.text,
    '--text-secondary': s.text2,
    '--text-tertiary': s.text3,
    '--border-default': s.border,
    '--border-subtle': s.border2,
    '--status-success': STATUS.success,
    '--status-warning': STATUS.warning,
    '--status-error': STATUS.error,
  };
  for (const [key, value] of Object.entries(vars)) {
    document.documentElement.style.setProperty(key, value);
  }
}
