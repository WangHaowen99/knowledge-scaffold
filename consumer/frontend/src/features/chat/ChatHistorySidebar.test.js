// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const sidebarSource = readFileSync(join(currentDir, 'ChatHistorySidebar.jsx'), 'utf8');
const sidebarStyles = readFileSync(join(currentDir, 'chat.css'), 'utf8');

test('history renders distinct accessible icons for queued, running and awaiting-input sessions', () => {
  assert.equal(sidebarSource.includes('Clock3'), true);
  assert.equal(sidebarSource.includes('Loader2'), true);
  assert.equal(sidebarSource.includes('MessageCircleQuestion'), true);
  assert.match(
    sidebarSource,
    /className=\{\s*runStatus === 'running'\s*\?\s*'spin'\s*:\s*undefined\s*\}/,
  );
  assert.equal(sidebarSource.includes('aria-label={runStatusLabel}'), true);
  assert.equal(sidebarSource.includes("queued: { label: '排队中', Icon: Clock3 }"), true);
  assert.equal(sidebarSource.includes("running: { label: '运行中', Icon: Loader2 }"), true);
  assert.equal(sidebarSource.includes("awaiting_input: { label: '需要输入', Icon: MessageCircleQuestion }"), true);
});

test('history uses the remaining sidebar height and exposes searchable session context', () => {
  assert.equal(sidebarStyles.includes('flex: 1 1 280px'), true);
  assert.equal(sidebarStyles.includes('height: 268px'), false);
  assert.equal(sidebarSource.includes('aria-label="搜索历史对话"'), true);
  assert.equal(sidebarSource.includes('未找到匹配的对话'), true);
  assert.equal(sidebarSource.includes('sessions.length'), true);
});
