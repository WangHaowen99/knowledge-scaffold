import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const { mergeToolTrace } = await import('../frontend/src/workbench/lib/appStore.js');
const { traceSummary } = await import('../frontend/src/workbench/shell/ProcessDrawer.jsx');

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

test('历史 Rail 拒绝与紧邻的失败终态合并为一条未执行记录', () => {
  const rejected = mergeToolTrace([], {
    runId: 'run-old',
    name: 'write_file',
    phase: 'rejected',
    summary: '候选自动修正已停止，需要检查校验明细',
    createdAt: '2026-08-24T12:47:06.104Z',
  }, 2951);

  const merged = mergeToolTrace(rejected, {
    runId: 'run-old',
    toolCallId: 'call-old-write',
    name: 'write_file',
    phase: 'failed',
    summary: '候选自动修正已停止，需要检查校验明细',
    createdAt: '2026-08-24T12:47:06.105Z',
  }, 2952);
  const nonAdjacent = mergeToolTrace(rejected, {
    runId: 'run-old',
    toolCallId: 'call-other-write',
    name: 'write_file',
    phase: 'failed',
    summary: '候选自动修正已停止，需要检查校验明细',
    createdAt: '2026-08-24T12:47:09.105Z',
  }, 2954);

  assert.equal(merged.length, 1);
  assert.equal(merged[0].id, 'tool:run-old:call-old-write');
  assert.equal(merged[0].phase, 'rejected');
  assert.equal(merged[0].seq, 2951);
  assert.equal(merged[0].toolCallId, 'call-old-write');
  assert.equal(nonAdjacent.length, 2);
});

test('同一评测轮次的历史重复读取合并为一条轨迹', () => {
  let trace: any[] = [];
  for (let index = 1; index <= 3; index += 1) {
    trace = mergeToolTrace(trace, {
      runId: 'run-evaluation',
      toolCallId: `call-results-${index}`,
      name: 'get_evaluation_results',
      phase: 'completed',
      summary: '已读取 7 条真实评测结果',
      createdAt: `2026-08-25T14:41:${String(index).padStart(2, '0')}.000Z`,
    }, index);
  }

  assert.equal(trace.length, 1);
  assert.equal(trace[0].segmentCount, 3);
  assert.deepEqual(trace[0].toolCallIds, [
    'call-results-1', 'call-results-2', 'call-results-3',
  ]);
  assert.equal(traceSummary(trace[0]), '已读取 7 条真实评测结果 · 共 3 次读取，已合并');
});
