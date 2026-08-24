import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const { mergeToolTrace } = await import('../frontend/src/workbench/lib/appStore.js');

test('同一工具调用的自动修正终态只保留一条轨迹', () => {
  const started = mergeToolTrace([], {
    runId: 'run-1',
    toolCallId: 'call-write-1',
    name: 'write_file',
    phase: 'started',
    summary: '正在写入对象',
    createdAt: '2026-08-24T12:00:00.000Z',
  }, 1);

  const repairing = mergeToolTrace(started, {
    runId: 'run-1',
    toolCallId: 'call-write-1',
    name: 'write_file',
    phase: 'repairing',
    summary: '对象文件缺少必需章节',
    code: 'KNOWLEDGE_FILE_INVALID',
    retryable: true,
    repairAttempt: 1,
    repairRoundId: 'model-1',
    findingCount: 4,
    createdAt: '2026-08-24T12:00:01.000Z',
  }, 2);

  assert.equal(repairing.length, 1);
  assert.equal(repairing[0].phase, 'repairing');
  assert.equal(repairing[0].code, 'KNOWLEDGE_FILE_INVALID');
  assert.equal(repairing[0].retryable, true);
  assert.equal(repairing[0].repairAttempt, 1);
  assert.equal(repairing[0].repairRoundId, 'model-1');
  assert.equal(repairing[0].findingCount, 4);
  assert.equal(repairing[0].createdAt, '2026-08-24T12:00:00.000Z');
});

test('最终停止覆盖同一调用且前端提供修正中与阻塞标签', () => {
  const trace = mergeToolTrace([], {
    runId: 'run-1',
    toolCallId: 'call-write-2',
    name: 'write_file',
    phase: 'blocked',
    summary: '候选自动修正已停止',
    code: 'CANDIDATE_REPAIR_STOPPED',
    repairStopped: true,
    repairAttempt: 3,
  }, 3);
  const drawerSource = readFileSync(
    new URL('../frontend/src/workbench/shell/ProcessDrawer.jsx', import.meta.url),
    'utf8',
  );

  assert.equal(trace.length, 1);
  assert.equal(trace[0].phase, 'blocked');
  assert.equal(trace[0].repairStopped, true);
  assert.match(drawerSource, /repairing:\s*\{ label: '自动修正中'/u);
  assert.match(drawerSource, /blocked:\s*\{ label: '需要处理'/u);
});
