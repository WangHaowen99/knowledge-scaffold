// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const sourcePath = fileURLToPath(
  new URL('../src/features/chat/ChatHistorySidebar.jsx', import.meta.url),
);

test('session pagination labels are valid UTF-8 Chinese text', async () => {
  const source = await readFile(sourcePath, 'utf8');
  assert.match(source, /loadingMoreSessions \? '正在加载' : '加载更多'/);
  assert.doesNotMatch(source, /姝ｅ湪鍔犺浇|鍔犺浇鏇村/);
});
