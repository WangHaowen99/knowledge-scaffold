import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'analysis-trace-'));

const { createProject } = await import('../src/projects/repo.js');
const { upsertNode } = await import('../src/workflow/engine.js');
const {
  appendTraceEvent,
  listTraceEvents,
  sanitizeTraceDetails,
  traceEventFromAgentEvent,
} = await import('../src/workflow/trace.js');

test('分析轨迹按顺序持久化并过滤敏感内容', () => {
  const project = createProject({ owner: 'alice', name: 'trace', domain: 'demo', background: '' });
  const node = upsertNode({ projectId: project.id, type: 'analyze', status: 'running', ordinal: 0 });

  appendTraceEvent({
    projectId: project.id,
    nodeId: node.id,
    phase: 'prepare',
    kind: 'phase_started',
    status: 'running',
    title: '准备材料',
  });
  appendTraceEvent({
    projectId: project.id,
    nodeId: node.id,
    phase: 'artifact_batch',
    kind: 'tool_started',
    status: 'running',
    title: '读取文档',
    details: { filename: 'manual.docx', apiKey: 'secret', content: 'x'.repeat(9000) },
  });

  const events = listTraceEvents(node.id);
  assert.deepEqual(events.map((event) => event.seq), [1, 2]);
  assert.equal(events[1].details?.apiKey, '[已隐藏]');
  assert.equal('content' in (events[1].details ?? {}), false);
  assert.deepEqual(sanitizeTraceDetails({ reasoning: 'private', query: '装置负荷' }), { query: '装置负荷' });
});

test('Agent 轨迹记录工具调用但丢弃私有 thinking 块', () => {
  assert.deepEqual(traceEventFromAgentEvent({
    type: 'tool_execution_start',
    toolName: 'read_document',
    args: { filename: 'manual.docx', offset: 6000 },
  }), {
    phase: 'agent',
    kind: 'tool_started',
    status: 'running',
    title: '读取文档',
    summary: 'manual.docx · offset 6000',
    details: { toolName: 'read_document', filename: 'manual.docx', offset: 6000 },
  });

  assert.equal(traceEventFromAgentEvent({
    type: 'turn_end',
    message: { content: [{ type: 'thinking', thinking: 'private' }] },
  }), null);
});
