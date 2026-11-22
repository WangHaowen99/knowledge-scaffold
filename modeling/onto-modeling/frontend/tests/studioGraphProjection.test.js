import assert from 'node:assert/strict';
import { test } from 'node:test';
import { filterStudioGraph } from '../src/components/studio/legacy/studio-model.js';

// Synthetic data only. The graph projection must not remove source documents.
function fixture() {
  const kinds = ['object', 'operation', 'rule', 'script', 'metric', 'testcase', 'mockdata', 'diagram', 'summary', 'table', 'unknown'];
  return {
    nodes: kinds.map((kind) => ({ id: `synthetic_${kind}`, kind, label: `${kind} example` })),
    edges: kinds.slice(1).map((kind) => ({
      id: `synthetic_relation_${kind}`, source: 'synthetic_object', target: `synthetic_${kind}`,
    })),
  };
}

test('graph projection includes only the five knowledge element kinds and their valid edges', () => {
  const graph = fixture();
  const original = structuredClone(graph);
  const result = filterStudioGraph(graph, '', '');
  assert.deepEqual(result.nodes.map((node) => node.kind), ['object', 'operation', 'rule', 'script', 'metric']);
  assert.equal(result.edges.length, 4);
  const ids = new Set(result.nodes.map((node) => node.id));
  assert.ok(result.edges.every((edge) => ids.has(edge.source) && ids.has(edge.target)));
  assert.deepEqual(graph, original, 'file, table and editing data remain intact');
});

test('search neighbors cannot reintroduce excluded kinds, including matches in document descriptions', () => {
  const graph = fixture();
  const documents = graph.nodes.map((node) => ({ ...node, title: node.label, description: 'shared search phrase' }));
  const result = filterStudioGraph(graph, '', 'shared search phrase', documents, ['script']);
  assert.deepEqual(result.nodes.map((node) => node.kind), ['object', 'operation', 'rule', 'metric']);
  assert.equal(result.edges.length, 3);
  assert.deepEqual(filterStudioGraph(graph, '', 'testcase'), { nodes: [], edges: [] });
});

test('an excluded kind requested by stale navigation never appears in the graph', () => {
  const graph = fixture();
  assert.deepEqual(filterStudioGraph(graph, 'testcase', ''), { nodes: [], edges: [] });
  const selected = filterStudioGraph(graph, 'rule', '');
  assert.deepEqual(selected.nodes.map((node) => node.id), ['synthetic_rule']);
  assert.deepEqual(selected.edges, []);
});
