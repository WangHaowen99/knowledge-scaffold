// DA's provider events folded into a turn. Payload shapes follow DA's SSE events; synthetic data only.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  NO_EXTRAS, applyFileItem, applyProviderEvent, applyToolResult, isLong, pushedFileOf, tableOf, todoProgress, type Extras,
} from '../src/da/events.ts';

const fold = (events: [string, unknown][], start: Extras = NO_EXTRAS) =>
  events.reduce((x, [type, payload]) => applyProviderEvent(x, type, payload), start);

test('a pushed card keeps its type, title and content; a pushed file card joins the file list', () => {
  const x = fold([
    ['push_content', { type: 'markdown', title: '合成报告', data: '# 结论', dataLength: 4, timestamp: 't' }],
    ['push_content', { type: 'table', title: '对照表', data: [{ a: 1 }] }],
    ['push_content', { type: 'file', title: '附件', filePath: 'out/合成.xlsx' }],
  ]);
  assert.deepEqual(x.pushes.map((p) => [p.type, p.title]), [['markdown', '合成报告'], ['table', '对照表']]);
  assert.equal(x.pushes[1].data, '[\n  {\n    "a": 1\n  }\n]');
  assert.deepEqual(x.files, [{ name: '合成.xlsx', size: undefined }]);
});

test('each todo_update replaces the whole list', () => {
  const x = fold([
    ['todo_update', { todos: [{ id: '1', content: '检索', status: 'in_progress' }, { id: '2', content: '核对', status: 'pending' }] }],
    ['todo_update', { items: [{ id: '1', content: '检索', status: 'completed' }, { id: '3', content: '作废', status: 'cancelled' }] }],
  ]);
  assert.deepEqual(x.todos, [
    { id: '1', content: '检索', status: 'completed' },
    { id: '3', content: '作废', status: 'cancelled' },
  ]);
  assert.deepEqual(todoProgress(x.todos ?? []), { done: 1, total: 1 });
  assert.equal(applyProviderEvent(x, 'todo_update', { nothing: true }), x);
});

test('a workflow tracks its agents from start to completion', () => {
  const x = fold([
    ['workflow_event', { type: 'workflow_start', workflowId: 'w1', goal: '多角度查证', mode: 'parallel', agentCount: 2 }],
    ['workflow_event', { type: 'workflow_agent_start', workflowId: 'w1', agentId: 'a1', role: '设计', task: '查设计要求' }],
    ['workflow_event', { type: 'workflow_agent_tool_call', workflowId: 'w1', agentId: 'a1', toolName: 'kb_search' }],
    ['workflow_event', { type: 'workflow_agent_chunk', agentId: 'a1' }],
    ['workflow_event', { type: 'workflow_agent_start', workflowId: 'w1', agentId: 'a2', task: '查规程' }],
    ['workflow_event', { type: 'workflow_agent_complete', workflowId: 'w1', agentId: 'a2', status: 'failed', duration_ms: 1200 }],
    ['workflow_event', { type: 'workflow_complete', workflowId: 'w1', status: 'completed', totalDuration: 5000, resultCount: 1 }],
  ]);
  assert.equal(x.workflows.length, 1);
  const [w] = x.workflows;
  assert.deepEqual([w.goal, w.agentCount, w.status, w.durationMs, w.resultCount], ['多角度查证', 2, 'completed', 5000, 1]);
  assert.deepEqual(
    w.agents.map((a) => [a.id, a.label, a.status, a.tool, a.durationMs]),
    [['a1', '设计', 'running', 'kb_search', undefined], ['a2', 'a2', 'failed', undefined, 1200]],
  );
});

test('unknown events change nothing', () => {
  assert.equal(applyProviderEvent(NO_EXTRAS, 'subagent_start', {}), NO_EXTRAS);
  assert.equal(applyProviderEvent(NO_EXTRAS, 'workflow_event', { type: 'something_else' }), NO_EXTRAS);
});

test('a push_file result is a file; a skipped duplicate or another result is not', () => {
  const pushed = { pushed: true, type: 'file', title: '报表', fileName: '合成.pdf', fileSize: 2048, mimeType: 'application/pdf', downloadUrl: '/f/1' };
  assert.deepEqual(pushedFileOf(pushed), { name: '合成.pdf', size: 2048, mimeType: 'application/pdf', downloadUrl: '/f/1' });
  assert.deepEqual(pushedFileOf(JSON.stringify(pushed))?.name, '合成.pdf');
  assert.equal(pushedFileOf({ pushed: false, skipped: true, type: 'file', fileName: '合成.pdf' }), null);
  assert.equal(pushedFileOf('[]'), null);
  assert.equal(pushedFileOf({ pushed: true, type: 'markdown', title: 'x' }), null);
  const twice = applyToolResult(applyToolResult(NO_EXTRAS, pushed), { ...pushed, fileSize: 4096 });
  assert.deepEqual(twice.files.map((f) => [f.name, f.size]), [['合成.pdf', 4096]]);
});

test('table data: JSON rows, header rows, columns/rows, CSV and TSV; a Markdown table is left to Markdown', () => {
  assert.deepEqual(tableOf('[{"参数":"压力","值":0.4},{"参数":"电流"}]'), { columns: ['参数', '值'], rows: [['压力', '0.4'], ['电流', '']] });
  assert.deepEqual(tableOf('[["a","b"],[1,2]]'), { columns: ['a', 'b'], rows: [['1', '2']] });
  assert.deepEqual(tableOf('{"columns":["a"],"rows":[[1]]}'), { columns: ['a'], rows: [['1']] });
  assert.deepEqual(tableOf('a,b\n"x, y",2'), { columns: ['a', 'b'], rows: [['x, y', '2']] });
  assert.deepEqual(tableOf('a\tb\n1\t2'), { columns: ['a', 'b'], rows: [['1', '2']] });
  assert.equal(tableOf('| a | b |\n|---|---|\n| 1 | 2 |'), null);
  assert.equal(tableOf('只是一段话'), null);
});

test('long content starts folded', () => {
  assert.equal(isLong('短'), false);
  assert.equal(isLong('x'.repeat(1501)), true);
  assert.equal(isLong('行\n'.repeat(30)), true);
});

test('a file the host fetched becomes downloadable; one it did not stays listed only', () => {
  let x = applyToolResult(NO_EXTRAS, { pushed: true, type: 'file', fileName: '合成.csv', fileSize: 12, downloadUrl: '/api/x' });
  x = applyToolResult(x, { pushed: true, type: 'file', fileName: '留在DA.xlsx', fileSize: 30 });
  x = applyFileItem(x, { name: '合成.csv', size: 12, mime_type: 'text/csv' });
  assert.deepEqual(x.files.map((f) => [f.name, Boolean(f.local)]), [['合成.csv', true], ['留在DA.xlsx', false]]);
  assert.equal(x.files[0].mimeType, 'text/csv');
  assert.equal(applyFileItem(x, { size: 1 }), x); // no name: nothing to offer
});
