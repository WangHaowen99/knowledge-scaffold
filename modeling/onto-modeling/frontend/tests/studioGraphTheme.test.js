// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { GRAPH_THEMES } from '../src/components/studio/legacy/webgl/graph-theme.js';

const KINDS = ['object', 'operation', 'rule', 'script', 'metric'];

function relativeLuminance(hex) {
  const channels = [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16) / 255)
    .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

test('selection, active and error accents stay clearly separable from every kind color', () => {
  for (const theme of Object.values(GRAPH_THEMES)) {
    // Selection/active live in the blue/orange kind families, so they must separate
    // by luminance. Error is an off-family crimson and only needs to stay distinct.
    for (const accent of ['selection', 'active']) {
      for (const kind of KINDS) {
        assert.ok(
          Math.abs(relativeLuminance(theme[accent]) - relativeLuminance(theme.colors[kind])) >= 0.08,
          `${theme.mode} ${accent} is too close to ${kind} (${theme[accent]} vs ${theme.colors[kind]})`,
        );
      }
    }
    assert.notEqual(theme.selection, theme.active);
    assert.notEqual(theme.selection, theme.error);
    assert.notEqual(theme.active, theme.error);
    for (const kind of KINDS) {
      assert.notEqual(theme.selection, theme.colors[kind]);
      assert.notEqual(theme.active, theme.colors[kind]);
      assert.notEqual(theme.error, theme.colors[kind]);
    }
  }
});
