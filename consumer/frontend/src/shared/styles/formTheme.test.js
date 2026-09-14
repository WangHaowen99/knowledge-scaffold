// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const sourceDir = join(currentDir, '../..');
const baseCss = readFileSync(join(currentDir, 'base.css'), 'utf8');
const uiCss = readFileSync(join(currentDir, '../ui/ui.css'), 'utf8');
const loginCss = readFileSync(
  join(sourceDir, 'features/auth/login.css'),
  'utf8',
);
const chatCss = readFileSync(join(sourceDir, 'features/chat/chat.css'), 'utf8');
const knowledgeCss = readFileSync(
  join(sourceDir, 'features/knowledge/knowledge.css'),
  'utf8',
);
const configCenterSource = readFileSync(
  join(sourceDir, 'features/settings/ConfigCenter.jsx'),
  'utf8',
);

const walk = (directory) =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });

const formControlSelector =
  /(?:^|[\s>+~,(])(?:input|select|textarea)(?=[\s>+~,.#:[)]|$)/i;
const hardcodedLightBackground =
  /background(?:-color)?\s*:\s*(?:#fff(?:fff)?|white|#faf9f4|#fafaf8|#f7f6f3)\b/i;

test('shared form controls expose a complete semantic color contract', () => {
  for (const token of [
    'control-bg',
    'control-fg',
    'control-border',
    'control-placeholder',
    'control-disabled-bg',
    'control-disabled-fg',
    'control-focus-ring',
    'control-error-border',
    'control-error-ring',
  ]) {
    assert.match(baseCss, new RegExp(`--${token}:`));
  }
  assert.match(baseCss, /input:-webkit-autofill/);
  assert.match(
    baseCss,
    /::placeholder\s*\{\s*color:\s*var\(--control-placeholder\)/,
  );
  assert.match(baseCss, /:is\(\[aria-invalid='true'\], :user-invalid\)/);
  assert.match(baseCss, /border-color:\s*var\(--control-error-border\)/);
  assert.match(
    baseCss,
    /:focus-visible\s*\{\s*outline:\s*3px solid var\(--control-focus-ring\)/,
  );
  assert.match(
    loginCss,
    /input:focus-visible\s*\{\s*outline:\s*3px solid var\(--control-focus-ring\)/,
  );
  assert.match(
    chatCss,
    /\.ti-box:focus-within\s*\{[^}]*var\(--control-focus-ring\)/,
  );
  assert.doesNotMatch(baseCss, /html\[data-mwr-mode='dark'\] \.mwr input\s*,/);
});

test('shared and feature-specific form surfaces use semantic control colors', () => {
  assert.match(
    uiCss,
    /background:\s*var\(--control-bg\);\s*color:\s*var\(--control-fg\)/,
  );
  assert.match(
    uiCss,
    /background:\s*var\(--control-disabled-bg\);\s*color:\s*var\(--control-disabled-fg\)/,
  );
  assert.match(
    knowledgeCss,
    /\.kb-file-editor\s*\{[^}]*background:\s*var\(--control-bg\);[^}]*color:\s*var\(--control-fg\)/s,
  );
  assert.match(configCenterSource, /background:\s*'var\(--control-bg\)'/);
  assert.match(configCenterSource, /color:\s*'var\(--control-fg\)'/);
});

test('native form-control selectors do not hardcode light-only backgrounds', () => {
  for (const file of walk(sourceDir).filter(
    (path) => extname(path) === '.css',
  )) {
    const css = readFileSync(file, 'utf8');
    for (const match of css.matchAll(
      /(?<selector>[^{}]+)\{(?<declarations>[^{}]*)\}/g,
    )) {
      const { selector, declarations } = match.groups;
      if (!formControlSelector.test(selector)) {
        continue;
      }
      assert.doesNotMatch(
        declarations,
        hardcodedLightBackground,
        `${file}: ${selector.trim()}`,
      );
    }
  }
});
