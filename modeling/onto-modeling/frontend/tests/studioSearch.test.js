// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  studioGraphNeighborhood,
  studioGraphNeighborhoodHops,
  studioSearchCandidates,
  filterStudioGraph,
  locateStudioGraphElement,
  projectStudioLargeGraph,
} from '../src/components/studio/legacy/studio-model.js';

const nodes = [
  { id: 'obj:tank', label: '储罐', kind: 'object' },
  { id: 'obj:rate', label: '罐容利用率', kind: 'object' },
  { id: 'op:balance', label: '罐容稳态优先管理', kind: 'operation' },
  { id: 'rule:band', label: '稳态容忍带', kind: 'rule' },
  { id: 'metric:peak', label: '月度峰值', kind: 'metric' },
];
const elements = [
  { id: 'obj:tank', title: '储罐', description: '存储液态物料的立式常压容器' },
  { id: 'obj:rate', title: '罐容利用率', description: '' },
  { id: 'op:balance', title: '罐容稳态优先管理', description: '按月度峰谷调度罐存' },
  { id: 'rule:band', title: '稳态容忍带', description: '高位与低位之间的缓冲区间' },
  { id: 'metric:peak', title: '月度峰值', description: '' },
];
const edges = [
  { id: 'e1', source: 'obj:tank', target: 'op:balance' },
  { id: 'e2', source: 'obj:rate', target: 'op:balance' },
  { id: 'e3', source: 'op:balance', target: 'rule:band' },
  { id: 'e4', source: 'rule:band', target: 'metric:peak' },
];

test('search candidates rank name matches first and description matches last', () => {
  const ranked = studioSearchCandidates(nodes, '罐', elements);
  assert.equal(ranked[0].id, 'obj:rate', 'A label starting with the query ranks first');
  assert.equal(ranked[0].score, 0);
  assert.ok(ranked.some((candidate) => candidate.id === 'obj:tank'));
  assert.ok(ranked.every((candidate) => candidate.score <= 2));
  const byDescription = studioSearchCandidates(nodes, '缓冲', elements);
  assert.deepEqual(byDescription.map((candidate) => candidate.id), ['rule:band']);
  assert.equal(byDescription[0].score, 2);
  assert.deepEqual(studioSearchCandidates(nodes, '  ', elements), []);
  assert.ok(studioSearchCandidates(nodes, '罐', elements, 2).length <= 2);
});

test('search candidates fall back to fuzzy Chinese matching', () => {
  // One character off the real name still finds the element once the query has
  // enough characters for the single-edit match.
  const fuzzy = studioSearchCandidates(nodes, '罐容用', elements);
  assert.ok(fuzzy.some((candidate) => candidate.id === 'obj:rate'));
  assert.ok(fuzzy.every((candidate) => candidate.score === 3));
  assert.ok(fuzzy.every((candidate) => !`${candidate.label} ${candidate.id}`.includes('罐容用')));
});

test('neighborhood hops extend one level per hop and stay identical at hop 1', () => {
  const direct = studioGraphNeighborhood('op:balance', edges);
  const hopOne = studioGraphNeighborhoodHops('op:balance', edges, 1);
  assert.deepEqual([...hopOne.nodes].sort(), [...direct.nodes].sort());
  assert.deepEqual([...hopOne.edges].sort(), [...direct.edges].sort());
  const two = studioGraphNeighborhoodHops('op:balance', edges, 2);
  assert.ok(two.nodes.has('metric:peak'));
  assert.ok(two.edges.has('e4'));
  const three = studioGraphNeighborhoodHops('op:balance', edges, 3);
  assert.deepEqual([...three.nodes].sort(), [...two.nodes].sort(), 'The synthetic chain has no third level');
  assert.deepEqual([...studioGraphNeighborhoodHops('', edges, 2).nodes], []);
});

test('a graph label remains searchable when its element title is different', () => {
  // Synthetic fixture: display names and entry titles can legitimately differ.
  const graph = { nodes: [{ id: 'object:alpha', label: '显示名称甲', kind: 'object' }], edges: [] };
  const entries = [{ id: 'object:alpha', title: '另一个标题', description: '' }];
  assert.equal(studioSearchCandidates(graph.nodes, '显示名称甲', entries)[0]?.id, 'object:alpha');
  assert.equal(filterStudioGraph(graph, '', '显示名称甲', entries).nodes[0]?.id, 'object:alpha');
});

test('search candidates exclude non-graph document kinds', () => {
  const graphNodes = [
    { id: 'object:alpha', label: '合成示例', kind: 'object' },
    { id: 'document:alpha', label: '合成示例', kind: 'testcase' },
  ];
  assert.deepEqual(studioSearchCandidates(graphNodes, '合成示例').map((node) => node.id), ['object:alpha']);
});

test('one-hop emphasis does not include edges between two neighbors', () => {
  const triangle = [
    { id: 'ab', source: 'a', target: 'b' },
    { id: 'ac', source: 'a', target: 'c' },
    { id: 'bc', source: 'b', target: 'c' },
  ];
  assert.deepEqual(studioGraphNeighborhoodHops('a', triangle, 1), studioGraphNeighborhood('a', triangle));
  assert.ok(studioGraphNeighborhoodHops('a', triangle, 2).edges.has('bc'));
});

test('locating a hidden search target restores its type and context without changing graph data', () => {
  const graph = {
    nodes: [
      { id: 'object:alpha', label: '合成甲', kind: 'object' },
      { id: 'rule:beta', label: '合成乙', kind: 'rule' },
    ],
    edges: [{ id: 'relation:alpha-beta', source: 'object:alpha', target: 'rule:beta' }],
  };
  const session = { graph, elements: [], query: 'missing', queryDraft: '合成乙', kind: 'object', hiddenKinds: ['rule', 'metric'], nodePositions: { retained: {} } };
  const located = locateStudioGraphElement(session, 'rule:beta');
  const visible = filterStudioGraph(located.graph, located.kind, located.query, located.elements, located.hiddenKinds);
  assert.equal(located.selectedElementId, 'rule:beta');
  assert.deepEqual(visible.nodes.map((node) => node.id), ['object:alpha', 'rule:beta']);
  assert.equal(visible.edges.length, 1);
  assert.deepEqual(located.hiddenKinds, ['metric']);
  assert.equal(located.queryDraft, '合成乙');
  assert.equal(located.graph, session.graph);
  assert.equal(located.nodePositions, session.nodePositions);
  assert.equal(session.query, 'missing');
  assert.equal(locateStudioGraphElement(located, 'rule:beta').graphFocusRequest, located.graphFocusRequest + 1);
  assert.equal(locateStudioGraphElement(session, 'missing'), session);
});

test('locating a search result expands its collapsed cluster', () => {
  const graph = {
    nodes: Array.from({ length: 24 }, (_, index) => ({ id: `object:${index}`, label: `合成节点 ${index}`, kind: 'object' })),
    edges: [],
  };
  const before = projectStudioLargeGraph(graph, { threshold: 10, maxClusterSize: 6 });
  assert.ok(before.clustered);
  assert.ok(!before.nodes.some((node) => node.id === 'object:17'));
  const located = locateStudioGraphElement({ graph }, 'object:17');
  const after = projectStudioLargeGraph(graph, { threshold: 10, maxClusterSize: 6, selectedElementId: located.selectedElementId });
  assert.ok(after.nodes.some((node) => node.id === 'object:17'));
});

test('search is case insensitive for keys and never returns more than eight suggestions', () => {
  const graphNodes = Array.from({ length: 12 }, (_, index) => ({ id: `object:${index}`, key: `SyntheticKey${index}`, label: `合成节点 ${index}`, kind: 'object' }));
  assert.equal(studioSearchCandidates(graphNodes, 'synthetickey10')[0]?.id, 'object:10');
  assert.equal(studioSearchCandidates(graphNodes, '合成节点', [], 99).length, 8);
});
