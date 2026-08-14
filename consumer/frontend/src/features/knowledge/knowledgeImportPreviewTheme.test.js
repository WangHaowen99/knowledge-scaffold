import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const workbenchSource = readFileSync(join(currentDir, 'KnowledgeWorkbench.jsx'), 'utf8');
const knowledgeCss = readFileSync(join(currentDir, 'knowledge.css'), 'utf8');
const baseCss = readFileSync(join(currentDir, '../../shared/styles/base.css'), 'utf8');

function parseHexColor(value) {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(value);
  assert.ok(match, `expected a six-digit hex color, received ${value}`);
  return match.slice(1).map((channel) => Number.parseInt(channel, 16) / 255);
}

function relativeLuminance(value) {
  return parseHexColor(value)
    .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4))
    .reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);
}

function contrastRatio(first, second) {
  const [lighter, darker] = [relativeLuminance(first), relativeLuminance(second)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

function readThemeToken(block, token) {
  const value = new RegExp(`--${token}:\\s*(#[0-9a-f]{6})\\b`, 'i').exec(block)?.[1];
  assert.ok(value, `missing --${token} hex value`);
  return value;
}

test('every knowledge import preview branch uses the themed content class', () => {
  const importPreviewSource = /function ImportPreview[\s\S]*?(?=\nfunction stripFrontmatter)/.exec(workbenchSource)?.[0];
  assert.ok(importPreviewSource, 'ImportPreview source should be present');

  const previewTags = [...importPreviewSource.matchAll(/<pre\b[^>]*>/g)].map(([tag]) => tag);
  assert.equal(previewTags.length, 3);
  for (const tag of previewTags) assert.match(tag, /className="import-preview-content"/);
});

test('knowledge import preview uses semantic colors with WCAG AA contrast', () => {
  const declarations = /\.mwr \.import-preview-content\s*\{([^}]*)\}/.exec(knowledgeCss)?.[1];
  assert.ok(declarations, 'the import preview content rule should be present');
  assert.match(declarations, /background:\s*var\(--surface-subtle\)/);
  assert.match(declarations, /color:\s*var\(--text-primary\)/);
  assert.match(declarations, /border:\s*1px solid var\(--border-subtle\)/);

  const lightTheme = /\.mwr\s*\{([\s\S]*?)\n\}/.exec(baseCss)?.[1];
  const darkTheme = /html\[data-mwr-mode='dark'\] \.mwr\s*\{([\s\S]*?)\n\}/.exec(baseCss)?.[1];
  assert.ok(lightTheme && darkTheme, 'light and dark theme token blocks should be present');

  for (const theme of [lightTheme, darkTheme]) {
    const background = readThemeToken(theme, 'surface-subtle');
    const foreground = readThemeToken(theme, 'text-primary');
    assert.ok(contrastRatio(background, foreground) >= 4.5, `${foreground} on ${background} should meet WCAG AA`);
  }
});
