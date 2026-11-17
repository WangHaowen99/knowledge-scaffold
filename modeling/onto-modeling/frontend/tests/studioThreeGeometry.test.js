import assert from 'node:assert/strict';
import { test } from 'node:test';
import { graphCameraDistance, graphDepthDistribution, graphDepthSpan, graphNodeDepth, graphWorldPosition, validCameraSnapshot } from '../src/components/studio/legacy/webgl/studio-three-geometry.js';

// All fixtures are synthetic and unrelated to any knowledge package.
test('3D presentation keeps edited planar coordinates and stable depth independent of category', () => {
  const node = { id: 'synthetic_node_a', kind: 'object' };
  const result = graphWorldPosition(node, { x: 120, y: -80 });
  assert.equal(result.x, 120);
  assert.equal(result.y, 80);
  assert.equal(result.z, graphNodeDepth(node));
  assert.equal(graphNodeDepth(node), graphNodeDepth({ ...node, kind: 'metric' }));
  assert.equal(graphNodeDepth(node), graphNodeDepth({ ...node }));
  assert.deepEqual(graphWorldPosition(node, { x: 120, y: -80 }, 720), { x: 120, y: 80, z: 720 });
});

test('relationship depth fills a volume proportional to the planar extent rather than a thin sheet', () => {
  const nodes = Array.from({ length: 100 }, (_, index) => ({ id: `synthetic_node_${index}`, kind: 'object' }));
  const positions = new Map(nodes.map((node, index) => [node.id, { x: (index % 10) * 200, y: Math.floor(index / 10) * 100 }]));
  const original = structuredClone(positions);
  const depthFactors = graphDepthDistribution(nodes);
  assert.deepEqual([...depthFactors], [...graphDepthDistribution([...nodes].reverse())]);
  const span = graphDepthSpan(positions, 'force');
  const points = nodes.map((node) => graphWorldPosition(node, positions.get(node.id), depthFactors.get(node.id) * span));
  const extent = (axis) => Math.max(...points.map((point) => point[axis])) - Math.min(...points.map((point) => point[axis]));
  assert.equal(extent('x'), 1800);
  assert.equal(extent('y'), 900);
  assert.equal(extent('z'), 1530);
  assert.ok(points.filter((point) => Math.abs(point.z) < span / 4).length >= 40, 'nodes also occupy the middle of the volume');
  const [a, b, c] = [points[0], points[9], points[90]];
  const u = { x: b.x - a.x, y: b.y - a.y, z: b.z - a.z };
  const v = { x: c.x - a.x, y: c.y - a.y, z: c.z - a.z };
  const normal = { x: u.y * v.z - u.z * v.y, y: u.z * v.x - u.x * v.z, z: u.x * v.y - u.y * v.x };
  const volume = Math.max(...points.map((point) => Math.abs(normal.x * (point.x - a.x) + normal.y * (point.y - a.y) + normal.z * (point.z - a.z))));
  assert.ok(volume > extent('x') * extent('y') * extent('z') * 0.02, 'the depth distribution is not another tilted plane');
  assert.deepEqual(positions, original, 'the display never changes persisted planar positions');
});

test('depth adapts after force positions expand while category rows keep milder depth', () => {
  const positions = new Map([['synthetic_a', { x: -100, y: -50 }], ['synthetic_b', { x: 100, y: 50 }]]);
  assert.equal(graphDepthSpan(positions), 360);
  positions.set('synthetic_a', { x: -1000, y: -500 });
  positions.set('synthetic_b', { x: 1000, y: 500 });
  assert.equal(graphDepthSpan(positions), 1700);
  assert.equal(graphDepthSpan(positions, 'layered'), 900);
  const layered = new Map([['synthetic_a', { x: -1000, y: -500, z: -640 }], ['synthetic_b', { x: 1000, y: 500, z: 640 }]]);
  assert.equal(graphDepthSpan(layered, 'layered'), 1280, 'category depth comes from the explicit 3D layout');
  assert.deepEqual([...graphDepthDistribution([{ id: 'synthetic_only' }]).values()], [0]);
  assert.equal(graphDepthDistribution([]).size, 0);
});

test('fit distance accounts for narrow viewports and scene depth', () => {
  const size = { x: 1000, y: 500, z: 200 };
  assert.ok(graphCameraDistance(size, 0.5) > graphCameraDistance(size, 2));
  assert.ok(graphCameraDistance({ ...size, z: 500 }, 2) > graphCameraDistance(size, 2));
  assert.ok(Number.isFinite(graphCameraDistance(size, 0)));
});

test('camera restoration rejects malformed or degenerate saved views', () => {
  assert.equal(validCameraSnapshot({ position: [0, 50, 800], target: [0, 0, 0] }), true);
  for (const camera of [null, {}, { position: [0, 0, 0], target: [0, 0, 0] }, { position: [0, NaN, 800], target: [0, 0, 0] }, { position: [0, 0, 800], target: [0, 0] }]) assert.equal(validCameraSnapshot(camera), false);
});
