// Studio 图里同一对节点之间的多条关系（含对象类型自关联）：环线与弧线几何、大图分簇时的保留规则。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  studioParallelEdgeOffsets,
  studioSelfLoopIndexes,
  studioSelfLoopPoints,
} from '../src/components/studio/legacy/studio-graph-geometry.js';
import { projectStudioLargeGraph } from '../src/components/studio/legacy/studio-model.js';
import { WebGLGraphRuntime } from '../src/components/studio/legacy/webgl/StudioWebGLRuntime.js';

const topOf = (points) => Math.min(...points.map((item) => item.y));

function runtimeFor(nodes, positions, edges) {
  return {
    renderPosition: (position) => position,
    positions: new Map(Object.entries(positions)),
    nodeById: new Map(nodes.map((node) => [node.id, node])),
    layoutMode: 'force',
    movedNodeIds: new Set(),
    scene: { routes: {} },
    parallelEdgeOffsets: studioParallelEdgeOffsets(edges),
    selfLoopIndexes: studioSelfLoopIndexes(edges),
  };
}

test('自关联画成从节点上方绕出再回到节点的环线', () => {
  const start = { x: -20, y: -50 };
  const end = { x: 20, y: -50 };
  const points = studioSelfLoopPoints(start, end);
  assert.deepEqual(points[0], start);
  assert.deepEqual(points.at(-1), end);
  assert.ok(topOf(points) < start.y - 30, '环线顶点应在节点上方');
  // 末段向下回到节点，箭头才会指向节点自己
  const [previous, last] = points.slice(-2);
  assert.ok(last.y > previous.y);
});

test('同一节点的多条自关联按出现顺序编号，环线由内到外不重叠', () => {
  const indexes = studioSelfLoopIndexes([
    { id: 'a1', source: '甲', target: '甲' },
    { id: 'ab', source: '甲', target: '乙' },
    { id: 'b1', source: '乙', target: '乙' },
    { id: 'a2', source: '甲', target: '甲' },
  ]);
  assert.deepEqual([...indexes], [['a1', 0], ['b1', 0], ['a2', 1]]);

  const start = { x: -20, y: -50 };
  const end = { x: 20, y: -50 };
  assert.ok(topOf(studioSelfLoopPoints(start, end, 1)) < topOf(studioSelfLoopPoints(start, end, 0)) - 15);
});

test('WebGL 图给自关联算出贴着节点边缘的环线，而不是长度为 0 的线段', () => {
  const node = { id: '合成仓区', kind: 'object', width: 148, height: 148 };
  const edges = [
    { id: 'self-1', source: node.id, target: node.id },
    { id: 'self-2', source: node.id, target: node.id },
  ];
  const runtime = runtimeFor([node], { [node.id]: { x: 0, y: 0 } }, edges);
  const [inner, outer] = edges.map((edge) => WebGLGraphRuntime.prototype.edgePoints.call(runtime, edge));
  const radius = node.width / 2;
  assert.ok(Math.abs(Math.hypot(inner[0].x, inner[0].y) - radius) < 1e-6, '起点在节点边缘');
  assert.ok(Math.abs(Math.hypot(inner.at(-1).x, inner.at(-1).y) - radius) < 1e-6, '终点在节点边缘');
  assert.ok(topOf(inner) < -radius, '环线绕到节点上方');
  assert.ok(topOf(outer) < topOf(inner), '第二条自关联画在第一条外侧');
});

test('同一对节点间的多条关系分配不同弧度：单条保持直线，互逆各一条与原来一样各弯 28', () => {
  const offsets = studioParallelEdgeOffsets([
    { id: 'single', source: '甲', target: '乙' },
    { id: 'go', source: '丙', target: '丁' },
    { id: 'back', source: '丁', target: '丙' },
    { id: 'p1', source: '戊', target: '己' },
    { id: 'p2', source: '戊', target: '己' },
    { id: 'p3', source: '戊', target: '己' },
    { id: 'go1', source: '庚', target: '辛' },
    { id: 'go2', source: '庚', target: '辛' },
    { id: 'back1', source: '辛', target: '庚' },
    { id: 'self', source: '甲', target: '甲' },
  ]);
  assert.deepEqual(Object.fromEntries(offsets), {
    single: 0,
    go: 28,
    back: 28,
    p1: -56,
    p2: 0,
    p3: 56,
    go1: 28,
    go2: 84,
    back1: 28,
  });
});

test('WebGL 图把同方向的两条关系画成两条分开的弧线', () => {
  const nodes = [
    { id: '合成客户', kind: 'object', width: 100, height: 100 },
    { id: '合成企业', kind: 'object', width: 100, height: 100 },
  ];
  const edges = [
    { id: 'owner', source: '合成客户', target: '合成企业' },
    { id: 'payer', source: '合成客户', target: '合成企业' },
  ];
  const runtime = runtimeFor(nodes, { 合成客户: { x: 0, y: 0 }, 合成企业: { x: 400, y: 0 } }, edges);
  const [owner, payer] = edges.map((edge) => WebGLGraphRuntime.prototype.edgePoints.call(runtime, edge));
  const middle = (points) => points[Math.floor(points.length / 2)];
  assert.ok(Math.abs(middle(owner).y - middle(payer).y) > 20, '两条弧线中段明显分开');

  const single = runtimeFor(nodes, { 合成客户: { x: 0, y: 0 }, 合成企业: { x: 400, y: 0 } }, edges.slice(0, 1));
  assert.equal(WebGLGraphRuntime.prototype.edgePoints.call(single, edges[0]).length, 2, '单条关系仍是直线');
});

test('分簇投影保留未折叠节点的自关联，折叠进同一簇的连线仍不画', () => {
  const nodes = Array.from({ length: 4 }, (_, index) => ({ id: `合成对象${index}`, kind: 'object' }));
  const edges = [
    { id: 'self', source: '合成对象0', target: '合成对象0', kind: 'object_relation' },
    { id: 'inner', source: '合成对象1', target: '合成对象2', kind: 'object_relation' },
  ];
  const options = { threshold: 1, maxClusterSize: 4 };

  const collapsed = projectStudioLargeGraph({ nodes, edges }, options);
  assert.deepEqual(collapsed.edges, []);

  const clusterId = Object.keys(collapsed.clusters)[0];
  const expanded = projectStudioLargeGraph({ nodes, edges }, { ...options, expandedClusterIds: [clusterId] });
  assert.deepEqual(
    expanded.edges.map((edge) => [edge.source, edge.target]).sort(),
    [['合成对象0', '合成对象0'], ['合成对象1', '合成对象2']],
  );
});
