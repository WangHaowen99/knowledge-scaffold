import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
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

test('backend selects the latest completed analysis for downstream context', () => {
  const nodes = [
    node('new-analysis', 'analyze', 'done', 8),
    node('old-analysis', 'analyze', 'stale', 2),
    node('running-analysis', 'analyze', 'running', 9),
  ];

  assert.equal(latestBackendNodeOf(nodes, 'analyze', ['done', 'stale'])?.id, 'new-analysis');
});

test('rerunning analysis does not regress the project stage', () => {
  const source = readFileSync(new URL('../src/routes.ts', import.meta.url), 'utf8');
  const analyzeRoute = source.slice(source.indexOf("r.post('/api/projects/:id/analyze'"), source.indexOf('// 长任务防重入'));

  assert.match(analyzeRoute, /setProjectStage\(p\.id, 'analyze'\)/);
  assert.doesNotMatch(analyzeRoute, /\bsetStage\(p\.id, 'analyze'\)/);
});
