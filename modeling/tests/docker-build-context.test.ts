import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('Modeling Docker 上下文排除本地依赖和构建产物', async () => {
  const ignore = await readFile(new URL('../.dockerignore', import.meta.url), 'utf8');
  const entries = new Set(ignore.split(/\r?\n/).map((line) => line.trim()).filter(Boolean));

  for (const entry of ['node_modules', 'frontend/node_modules', 'dist', 'frontend/dist']) {
    assert.ok(entries.has(entry), `.dockerignore 缺少 ${entry}`);
  }
});
