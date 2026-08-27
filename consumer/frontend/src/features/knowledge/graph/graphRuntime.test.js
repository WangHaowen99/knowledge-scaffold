import assert from 'node:assert/strict';
import test from 'node:test';

import { deriveGraphRuntimeState, graphEdgeIdentity } from './graphRuntime.js';

const graph = {
  nodes: [
    { id: 'operation:cost-check', type: 'operation', key: 'cost-check', label: '成本核查', source: 'operations/cost-check.md' },
    { id: 'object:TargetItem', type: 'object', key: 'TargetItem', label: '制造件', source: 'objects/TargetItem.md' },
    { id: 'object:Supplier', type: 'object', key: 'Supplier', label: '供应商', source: 'objects/Supplier.md' },
  ],
  edges: [
    { source: 'operation:cost-check', target: 'object:TargetItem', type: 'uses' },
    { source: 'object:TargetItem', target: 'object:Supplier', type: 'provided_by' },
  ],
};

test('canonical messages keep latest terminal run recent and earlier runs historical', () => {
  const state = deriveGraphRuntimeState(graph, [
    {
      runId: 'run-1',
      status: 'finished',
      blocks: [{
        kind: 'business_data',
        status: 'finished',
        toolName: 'business_data_search',
        input: { entity: 'Supplier' },
      }],
    },
    {
      runId: 'run-2',
      status: 'finished',
      blocks: [{
        kind: 'business_data',
        status: 'finished',
        toolName: 'business_data_search',
        input: { entity: 'TargetItem' },
      }],
    },
  ], { now: Date.parse('2026-07-15T10:00:00Z'), recentWindowMs: 0 });

  assert.equal(state.nodes['object:Supplier'].state, 'history');
  assert.equal(state.nodes['object:TargetItem'].state, 'recent');
});

test('structured tool output lights exact nodes and an existing graph edge', () => {
  const edge = graph.edges[1];
  const state = deriveGraphRuntimeState(graph, [{
    runId: 'run-edge',
    status: 'finished',
    streamSegments: [{
      type: 'tool',
      state: 'completed',
      finished: true,
      meta: {
        tool_name: 'business_data_search',
        output_preview: JSON.stringify({
          nodes: [{ entity_type: 'TargetItem' }, { entity_type: 'Supplier' }],
          relationships: [{ source: 'TargetItem', target: 'Supplier', type: 'provided_by' }],
        }),
      },
    }],
  }]);

  assert.equal(state.nodes['object:TargetItem'].state, 'recent');
  assert.equal(state.nodes['object:Supplier'].state, 'recent');
  assert.equal(state.edges[graphEdgeIdentity(edge, 1)].state, 'recent');
});

test('a canonical stream segment can be projected without a message wrapper', () => {
  const state = deriveGraphRuntimeState(graph, [{
    type: 'tool',
    state: 'running',
    finished: false,
    meta: {
      name: 'ontology_context',
      input: { resource: 'operation', key: 'cost-check' },
    },
  }]);

  assert.equal(state.nodes['operation:cost-check'].state, 'live');
});

test('free-form prose and graph instance ids do not create false ontology hits', () => {
  const state = deriveGraphRuntimeState(graph, [{
    runId: 'run-prose',
    status: 'finished',
    streamSegments: [{
      type: 'tool',
      finished: true,
      meta: {
        tool_name: 'other_tool',
        output_preview: 'TargetItem Supplier cost-check',
        output: { records: [{ entity_instance_id: 'TargetItem' }] },
      },
    }],
  }]);

  assert.deepEqual(state.nodes, {});
  assert.deepEqual(state.edges, {});
});

test('nested subagent tool events are included without depending on a renderer component', () => {
  const state = deriveGraphRuntimeState(graph, [{
    runId: 'nested-run',
    status: 'running',
    streamSegments: [{
      type: 'subagent_group',
      status: 'running',
      finished: false,
      children: {
        child: {
          status: 'running',
          events: [{
            kind: 'tool',
            toolName: 'ontology_context',
            input: { resource: 'object', key: 'Supplier' },
            finished: false,
          }],
        },
      },
    }],
  }]);

  assert.equal(state.nodes['object:Supplier'].state, 'live');
});

test('a running parent does not turn an already finished stream tool back into a live hit', () => {
  const state = deriveGraphRuntimeState(graph, [{
    runId: 'run-still-streaming',
    status: 'running',
    isLoading: true,
    streamSegments: [{
      type: 'tool',
      state: 'completed',
      finished: true,
      endTs: Date.parse('2026-07-15T10:00:00Z'),
      meta: {
        tool_name: 'ontology_context',
        input: { resource: 'object', key: 'TargetItem' },
      },
    }],
  }], { now: Date.parse('2026-07-15T10:00:01Z') });

  assert.equal(state.nodes['object:TargetItem'].state, 'recent');
});

test('session ontology metadata scopes canonical trace blocks to the selected package', () => {
  const trace = {
    metadata: { selected_ontology_id: 'onto-a' },
    messages: [{
      runId: 'run-a',
      status: 'finished',
      streamSegments: [{
        type: 'tool',
        finished: true,
        meta: { tool_name: 'ontology_context', input: { resource: 'object', key: 'TargetItem' } },
      }],
    }],
  };

  assert.equal(deriveGraphRuntimeState(graph, trace, { ontologyId: 'onto-a' }).nodes['object:TargetItem'].state, 'recent');
  assert.deepEqual(deriveGraphRuntimeState(graph, trace, { ontologyId: 'onto-b' }).nodes, {});
});

test('message metadata filters mixed-package traces and unscoped traces fail closed', () => {
  const messages = [
    {
      metadata: { selected_ontology_id: 'onto-a' },
      runId: 'run-a',
      status: 'finished',
      streamSegments: [{ type: 'tool', finished: true, input: { entity: 'TargetItem' } }],
    },
    {
      metadata: { selected_ontology_id: 'onto-b' },
      runId: 'run-b',
      status: 'finished',
      streamSegments: [{ type: 'tool', finished: true, input: { entity: 'Supplier' } }],
    },
  ];
  const scoped = deriveGraphRuntimeState(graph, messages, { ontologyId: 'onto-a' });

  assert.equal(scoped.nodes['object:TargetItem'].state, 'recent');
  assert.equal(scoped.nodes['object:Supplier'], undefined);
  assert.deepEqual(deriveGraphRuntimeState(graph, [{
    runId: 'unknown-scope',
    status: 'finished',
    streamSegments: [{ type: 'tool', finished: true, input: { entity: 'TargetItem' } }],
  }], { ontologyId: 'onto-a' }).nodes, {});
});

test('tool input ontology id scopes automatic-selection traces without session metadata', () => {
  const state = deriveGraphRuntimeState(graph, [{
    runId: 'auto-selected',
    status: 'finished',
    streamSegments: [{
      type: 'tool',
      finished: true,
      meta: {
        tool_name: 'ontology_context',
        input: { ontology_id: 'onto-a', resource: 'object', key: 'Supplier' },
      },
    }],
  }], { ontologyId: 'onto-a' });

  assert.equal(state.nodes['object:Supplier'].state, 'recent');
  assert.deepEqual(deriveGraphRuntimeState(graph, [{
    type: 'tool',
    finished: true,
    input: { ontology_id: 'onto-b', entity: 'Supplier' },
  }], { ontologyId: 'onto-a' }).nodes, {});
});
