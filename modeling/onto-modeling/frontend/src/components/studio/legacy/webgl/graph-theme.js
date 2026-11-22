// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
// Appearance tokens only. The graph background always comes from the app surface.
export const GRAPH_THEMES = Object.freeze({
  light: Object.freeze({
    mode: 'light',
    colors: Object.freeze({
      object: '#3A739D', operation: '#417FAE', rule: '#7DAACB',
      script: '#AD5727', metric: '#E1A057', cluster: '#648199',
    }),
    sides: Object.freeze({ rule: '#4F789A', metric: '#B77436' }),
    edge: '#6F879C', edgeOpacity: 0.42, edgeMutedOpacity: 0.06, edgeActiveOpacity: 0.92,
    // Selection/active accents deliberately sit far outside the five kind colors in
    // luminance so related-node strokes, selected edges and the selection ring stay
    // visible on every element kind (the kind palette itself is frozen upstream).
    selection: '#1C3D5A', active: '#6B3411', error: '#B8324D', gridOpacity: 0.12,
    material: Object.freeze({
      roughness: 0.46, metalness: 0.025, clearcoat: 0.12,
      clearcoatRoughness: 0.5, emissiveIntensity: 0.34, emphasis: 0.035,
    }),
    lights: Object.freeze({ ambient: 1.45, key: 0.9, fill: 0.35, rim: 0.3 }),
    bloom: Object.freeze({ strength: 0.08, radius: 0.16, threshold: 1.15 }),
  }),
  dark: Object.freeze({
    mode: 'dark',
    colors: Object.freeze({
      object: '#5F9FCB', operation: '#78B1D5', rule: '#B2D0E6',
      script: '#D47B3E', metric: '#F2B96F', cluster: '#90AFC5',
    }),
    sides: Object.freeze({ rule: '#6F9FBE', metric: '#C98D4D' }),
    edge: '#7893AA', edgeOpacity: 0.34, edgeMutedOpacity: 0.05, edgeActiveOpacity: 0.92,
    selection: '#E8F1F7', active: '#FFE3BE', error: '#F08396', gridOpacity: 0.15,
    material: Object.freeze({
      roughness: 0.4, metalness: 0.035, clearcoat: 0.16,
      clearcoatRoughness: 0.45, emissiveIntensity: 0.30, emphasis: 0.055,
    }),
    lights: Object.freeze({ ambient: 1.45, key: 0.9, fill: 0.35, rim: 0.3 }),
    bloom: Object.freeze({ strength: 0.17, radius: 0.2, threshold: 1.08 }),
  }),
});

export function resolveGraphTheme(mode = 'light') {
  return GRAPH_THEMES[mode === 'dark' ? 'dark' : 'light'];
}

/** Use the resolved shared surface, including user-configured appearance colors. */
export function graphThemeMode(surfaceColor, fallback = 'light') {
  const value = String(surfaceColor || '').trim();
  const hex = /^#(?<channels>[\da-f]{6})$/iu.exec(value);
  let components = null;
  if (hex) {
    components = [0, 2, 4].map((index) => parseInt(hex.groups.channels.slice(index, index + 2), 16));
  } else if (/^rgba?\(/iu.test(value)) {
    components = value.match(/[\d.]+/gu)?.slice(0, 3).map(Number);
  }
  if (!components || components.length !== 3) { return fallback === 'dark' ? 'dark' : 'light'; }
  const linear = components.map((component) => {
    const channel = component / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722 < 0.25 ? 'dark' : 'light';
}
