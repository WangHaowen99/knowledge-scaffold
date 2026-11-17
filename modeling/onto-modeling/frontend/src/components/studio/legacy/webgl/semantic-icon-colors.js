// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { resolveGraphTheme } from './graph-theme.js';

const toLinear = (channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
const toSrgb = (channel) => channel <= 0.0031308 ? channel * 12.92 : 1.055 * channel ** (1 / 2.4) - 0.055;

function mixLinear(hex, scale, white = 0) {
  const channels = [1, 3, 5].map((offset) => {
    const value = toLinear(parseInt(hex.slice(offset, offset + 2), 16) / 255);
    return Math.round(toSrgb(value * scale + white) * 255).toString(16).padStart(2, '0');
  });
  return `#${channels.join('')}`;
}

// The SVG legend and physical materials use the same opaque sRGB role colors.
// Mix in linear light, as Three.js does, instead of blending SVG opacity into the background.
export function semanticIconRoleColors(kind, mode = 'light', fallbackColor = '#aebfd0') {
  const theme = resolveGraphTheme(mode);
  const body = theme.colors[kind] || fallbackColor;
  return {
    body,
    dark: mixLinear(body, theme.mode === 'light' ? 0.22 : 0.18),
    inset: theme.sides[kind] || mixLinear(body, 0.38),
    light: mixLinear(body, 0.78, 0.22),
    paper: mixLinear(body, 0.32, 0.68),
    white: '#f4f9ff',
  };
}
