import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { batchesForNode, latestNodeOf as latestFrontendNodeOf } from '../frontend/src/workflowNodes.js';
import { latestNodeOf as latestBackendNodeOf } from '../src/workflow/engine.js';
import type { WorkflowNode } from '../src/types.js';

const node = (id: string, type: WorkflowNode['type'], status: WorkflowNode['status'], ordinal: number): WorkflowNode => ({
  id,
  projectId: 'pj_test',
  type,
  status,
  ordinal,
  title: type,
  output: { domainOverview: id },
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:00:00.000Z',
});

test('frontend selects the latest rerun node instead of the stale historical result', () => {
  const nodes = [
    node('old-analysis', 'analyze', 'stale', 2),
    node('new-analysis', 'analyze', 'done', 8),
    node('interview', 'interview', 'done', 5),
  ];

  assert.equal(latestFrontendNodeOf(nodes, 'analyze')?.id, 'new-analysis');
});

test('backend selects the latest completed analysis for downstream context', () => {
  const nodes = [
    node('new-analysis', 'analyze', 'done', 8),
    node('old-analysis', 'analyze', 'stale', 2),
    node('running-analysis', 'analyze', 'running', 9),
  ];

  assert.equal(latestBackendNodeOf(nodes, 'analyze', ['done', 'stale'])?.id, 'new-analysis');
});

test('rerun interview only uses batches produced by the new interview node', () => {
  const batches = [
    { id: 'old-1', nodeId: 'old-interview', answered: true },
    { id: 'new-1', nodeId: 'new-interview', answered: false },
  ];

  assert.deepEqual(batchesForNode(batches, 'new-interview').map((batch) => batch.id), ['new-1']);
  assert.deepEqual(batchesForNode(batches, undefined), []);
});

test('stale banner navigates from other steps and reruns when already on analysis', () => {
  const source = readFileSync(new URL('../frontend/src/Workbench.jsx', import.meta.url), 'utf8');
  const banner = source.slice(source.indexOf('<div className="stale-banner">'), source.indexOf('<div className="wb-b">'));

  assert.match(source, /if \(effFocus !== 'analyze'\) \{\s+setFocus\('analyze'\);\s+return;\s+\}\s+doAnalyze\(\);/);
  assert.match(banner, /onClick=\{handleStaleReanalyze\}/);
  assert.match(banner, /busy=\{analyzeRunning\}/);
  assert.match(source, /\{analysisStale && staleTypes\.length > 0 && \(/);
});

test('rerunning analysis does not regress the project stage', () => {
  const source = readFileSync(new URL('../src/routes.ts', import.meta.url), 'utf8');
  const analyzeRoute = source.slice(source.indexOf("r.post('/api/projects/:id/analyze'"), source.indexOf('// 长任务防重入'));

  assert.match(analyzeRoute, /setProjectStage\(p\.id, 'analyze'\)/);
  assert.doesNotMatch(analyzeRoute, /\bsetStage\(p\.id, 'analyze'\)/);
});

test('stale interview cannot unlock or bypass package generation', () => {
  const workbench = readFileSync(new URL('../frontend/src/Workbench.jsx', import.meta.url), 'utf8');
  const routes = readFileSync(new URL('../src/routes.ts', import.meta.url), 'utf8');
  const generateRoute = routes.slice(routes.indexOf("r.post('/api/projects/:id/generate'"), routes.indexOf('// ---- 节点 7'));
  const m2 = readFileSync(new URL('../src/workflow/m2.ts', import.meta.url), 'utf8');

  assert.match(workbench, /const interviewDone = interviewNode\?\.status === 'done'/);
  assert.match(workbench, /const generateDone = generateNode\?\.status === 'done'/);
  assert.match(generateRoute, /interviewNode\?\.status !== 'done'/);
  assert.match(m2, /some\(\(batch\) => batch\.nodeId === nodeId\)/);
});
