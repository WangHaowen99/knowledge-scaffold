// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const baseCss = readFileSync(
  new URL('../../shared/styles/base.css', import.meta.url),
  'utf8',
);
const knowledgeCss = readFileSync(
  new URL('./knowledge.css', import.meta.url),
  'utf8',
);
const workbenchSource = readFileSync(
  new URL('./KnowledgeWorkbench.jsx', import.meta.url),
  'utf8',
);

const THEME_NAMES = ['default', 'violet', 'teal', 'orange', 'graphite'];

function cssBlock(source, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 's'));
  assert.ok(match, `missing CSS block: ${selector}`);
  return match[1];
}

function tokenValue(block, name) {
  const match = block.match(
    new RegExp(`--${name}:\\s*(#[0-9a-f]{3,6})\\b`, 'i'),
  );
  assert.ok(match, `missing hex token: --${name}`);
  return match[1];
}

function rgb(hex) {
  const value = hex.slice(1);
  const expanded =
    value.length === 3 ? [...value].map((part) => part + part).join('') : value;
  return [0, 2, 4].map((offset) =>
    Number.parseInt(expanded.slice(offset, offset + 2), 16),
  );
}

function mix(foreground, background, foregroundRatio) {
  const fg = rgb(foreground);
  const bg = rgb(background);
  return `#${fg
    .map((channel, index) =>
      Math.round(channel * foregroundRatio + bg[index] * (1 - foregroundRatio)),
    )
    .map((channel) => channel.toString(16).padStart(2, '0'))
    .join('')}`;
}

function luminance(hex) {
  const channels = rgb(hex).map((channel) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function contrast(first, second) {
  const lighter = Math.max(luminance(first), luminance(second));
  const darker = Math.min(luminance(first), luminance(second));
  return (lighter + 0.05) / (darker + 0.05);
}

function themePalette(name) {
  const defaultBlock = cssBlock(baseCss, '.mwr');
  if (name === 'default') {
    return {
      primary: tokenValue(defaultBlock, 'color-primary'),
      strong: tokenValue(defaultBlock, 'color-primary-strong'),
      soft: tokenValue(defaultBlock, 'color-primary-soft'),
    };
  }

  const themeBlock = cssBlock(baseCss, `html[data-mwr-theme='${name}'] .mwr`);
  return {
    primary: tokenValue(themeBlock, 'color-primary'),
    strong: tokenValue(themeBlock, 'color-primary-strong'),
    soft: tokenValue(themeBlock, 'color-primary-soft'),
  };
}

test('workbench view controls expose their selected state to assistive technology', () => {
  assert.match(
    workbenchSource,
    /<button\s+type="button"\s+aria-pressed=\{view !== 'source'\}/,
  );
  assert.match(
    workbenchSource,
    /<button\s+type="button"\s+aria-pressed=\{view === 'source'\}/,
  );
});

test('version and view controls use semantic theme tokens instead of light-only colors', () => {
  assert.match(baseCss, /--selection-bg:\s*var\(--color-primary-soft\)/);
  assert.match(baseCss, /--selection-fg:\s*var\(--color-primary-strong\)/);
  assert.match(
    baseCss,
    /--selection-indicator:\s*var\(--color-primary-strong\)/,
  );

  const darkBlock = cssBlock(baseCss, "html[data-mwr-mode='dark'] .mwr");
  assert.match(darkBlock, /--selection-fg:\s*var\(--text-primary\)/);
  assert.match(darkBlock, /--selection-indicator:\s*color-mix\(/);

  const selectedView = cssBlock(
    knowledgeCss,
    '.mwr .viewtog > button:not(.btn).on',
  );
  assert.match(selectedView, /background:\s*var\(--selection-bg\)/);
  assert.match(selectedView, /color:\s*var\(--selection-fg\)/);
  assert.match(
    selectedView,
    /box-shadow:\s*inset 0 -2px var\(--selection-indicator\)/,
  );

  const viewDeclarations = [
    ...knowledgeCss.matchAll(
      /[^{}]*\.viewtog[^{}]*\{(?<declarations>[^{}]*)\}/g,
    ),
  ]
    .map((match) => match.groups.declarations)
    .join('\n');
  assert.doesNotMatch(viewDeclarations, /#[0-9a-f]{3,8}\b/i);
});

test('selected controls meet WCAG AA across every accent theme in light and dark mode', () => {
  const defaultBlock = cssBlock(baseCss, '.mwr');
  const darkBlock = cssBlock(baseCss, "html[data-mwr-mode='dark'] .mwr");
  const darkSurface = tokenValue(darkBlock, 'surface-card');
  const darkText = tokenValue(darkBlock, 'text-primary');

  for (const themeName of THEME_NAMES) {
    const palette = themePalette(themeName);
    const darkSelection = mix(palette.primary, darkSurface, 0.24);
    const darkIndicator = mix(palette.primary, '#ffffff', 0.58);

    assert.ok(
      contrast(palette.strong, palette.soft) >= 4.5,
      `${themeName} light selected text must have at least 4.5:1 contrast`,
    );
    assert.ok(
      contrast(palette.strong, palette.soft) >= 3,
      `${themeName} light selected indicator must have at least 3:1 contrast`,
    );
    assert.ok(
      contrast(darkText, darkSelection) >= 4.5,
      `${themeName} dark selected text must have at least 4.5:1 contrast`,
    );
    assert.ok(
      contrast(darkIndicator, darkSelection) >= 3,
      `${themeName} dark selected indicator must have at least 3:1 contrast`,
    );
  }

  assert.ok(
    contrast(
      tokenValue(defaultBlock, 'text-secondary'),
      tokenValue(defaultBlock, 'surface-card'),
    ) >= 4.5,
    'light inactive view text must have at least 4.5:1 contrast',
  );
  assert.ok(
    contrast(tokenValue(darkBlock, 'text-secondary'), darkSurface) >= 4.5,
    'dark inactive view text must have at least 4.5:1 contrast',
  );
});
