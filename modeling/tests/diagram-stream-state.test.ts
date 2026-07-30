import assert from 'node:assert/strict';
import test from 'node:test';

import {
  advanceDiagramEventCursor,
  advanceProjectEventReplayGate,
  createProjectEventReplayGate,
  reduceDiagramStream,
} from '../frontend/src/diagramStreamState.js';

test('Mermaid 流事件按文件路径增量合并并保留当前生成状态', () => {
  let state = reduceDiagramStream(null, {
    type: 'process.diagram.stream.started',
    payload: { streamId: 'stream-1', phase: 'drafting', message: '正在整理流程草稿' },
  });
  state = reduceDiagramStream(state, {
    type: 'process.diagram.stream.file_started',
    payload: { streamId: 'stream-1', path: 'diagrams/process_1.mmd', title: '月度排产' },
  });
  state = reduceDiagramStream(state, {
    type: 'process.diagram.stream.delta',
    payload: { streamId: 'stream-1', path: 'diagrams/process_1.mmd', title: '月度排产', chunk: 'flowchart TD\n' },
  });
  state = reduceDiagramStream(state, {
    type: 'process.diagram.stream.delta',
    payload: { streamId: 'stream-1', path: 'diagrams/process_1.mmd', title: '月度排产', chunk: '  start --> step_1\n' },
  });

  assert.deepEqual(state, {
    streamId: 'stream-1',
    status: 'streaming',
    phase: 'rendering',
    message: '正在整理流程草稿',
    activePath: 'diagrams/process_1.mmd',
    files: [{
      path: 'diagrams/process_1.mmd',
      title: '月度排产',
      content: 'flowchart TD\n  start --> step_1\n',
    }],
  });
});

test('Mermaid 流完成和失败状态可被中间栏直接展示', () => {
  const initial = reduceDiagramStream(null, {
    type: 'process.diagram.stream.started', payload: { streamId: 'stream-2' },
  });
  const completed = reduceDiagramStream(initial, {
    type: 'process.diagram.stream.completed', payload: { streamId: 'stream-2' },
  });
  const ignored = reduceDiagramStream(completed, {
    type: 'process.diagram.stream.delta',
    payload: { streamId: 'old-stream', path: 'old.mmd', chunk: 'stale' },
  });

  assert.equal(completed.status, 'completed');
  assert.equal(ignored, completed);
  assert.equal(reduceDiagramStream(initial, {
    type: 'process.diagram.stream.failed',
    payload: { streamId: 'stream-2', message: '模型生成失败' },
  }).status, 'failed');
});

test('Mermaid 事件回放游标只按合法递增序号前进', () => {
  assert.equal(advanceDiagramEventCursor(0, { seq: 12 }), 12);
  assert.equal(advanceDiagramEventCursor(12, { seq: 8 }), 12);
  assert.equal(advanceDiagramEventCursor(12, { seq: '15' }), 15);
  assert.equal(advanceDiagramEventCursor(15, { seq: null }), 15);
});

test('历史普通事件回放完成时合并为一次工作区刷新', () => {
  let gate = createProjectEventReplayGate(3);
  let result = advanceProjectEventReplayGate(gate, { seq: 1 }, { diagramEvent: false });
  gate = result.gate;
  assert.equal(result.refresh, false);
  assert.equal(result.historical, true);

  result = advanceProjectEventReplayGate(gate, { seq: 2 }, { diagramEvent: true });
  gate = result.gate;
  assert.equal(result.refresh, false);

  result = advanceProjectEventReplayGate(gate, { seq: 3 }, { diagramEvent: false });
  gate = result.gate;
  assert.equal(result.refresh, true);
  assert.equal(gate.replaying, false);

  result = advanceProjectEventReplayGate(gate, { seq: 4 }, { diagramEvent: false });
  assert.equal(result.refresh, true);
  assert.equal(result.historical, false);
});
