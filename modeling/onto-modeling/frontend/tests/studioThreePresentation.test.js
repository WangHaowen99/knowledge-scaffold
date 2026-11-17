import assert from 'node:assert/strict';
import { test } from 'node:test';
import { boundedStudioClusterExpansion, studioCategoryPositions } from '../src/components/studio/legacy/webgl/studio-three-presentation.js';

// Synthetic data only; no production knowledge package is loaded by these tests.
test('category layout is deterministic and separates categories without modifying saved positions', () => {
  const nodes = [
    { id: 'node_b', kind: 'operation', position: { x: 14, y: 20 } },
    { id: 'node_a', kind: 'object', position: { x: 15, y: 25 } },
    { id: 'node_c', kind: 'object', position: { x: 30, y: 40 } },
  ];
  const original = structuredClone(nodes);
  const positions = studioCategoryPositions(nodes);
  assert.deepEqual([...positions], [...studioCategoryPositions([...nodes].reverse())]);
  assert.deepEqual(nodes, original);
  assert.equal((positions.get('node_a').y + positions.get('node_c').y) / 2, 0);
  assert.ok(positions.get('node_b').y < Math.min(positions.get('node_a').y, positions.get('node_c').y));
  assert.equal(new Set([...positions.values()].map(({ x, y, z }) => `${x}:${y}:${z}`)).size, nodes.length);
});

test('category bodies are wide, nonplanar and centered around the object layer', () => {
  const counts = { object: 25, operation: 18, script: 18, rule: 10, metric: 8 };
  const nodes = Object.entries(counts).flatMap(([kind, count]) => Array.from({ length: count }, (_, index) => ({ id: `synthetic_${kind}_${index}`, kind })));
  const positions = studioCategoryPositions(nodes);
  const points = [...positions.values()];
  const span = (items, axis) => Math.max(...items.map((point) => point[axis])) - Math.min(...items.map((point) => point[axis]));
  const width = span(points, 'x'); const height = span(points, 'y'); const depth = span(points, 'z');
  assert.ok(width / height >= 1.6 && width / height <= 1.9);
  assert.ok(depth / width >= 0.55 && depth / width <= 0.65);
  const centers = Object.fromEntries(Object.keys(counts).map((kind) => {
    const members = nodes.filter((node) => node.kind === kind).map((node) => positions.get(node.id));
    return [kind, members.reduce((sum, point) => sum + point.y, 0) / members.length];
  }));
  const categoryGap = Math.abs(centers.operation - centers.object);
  for (const kind of Object.keys(counts)) {
    const layer = nodes.filter((node) => node.kind === kind).map((node) => positions.get(node.id));
    const centerY = layer.reduce((sum, point) => sum - point.y, 0) / layer.length;
    if (kind === 'object') assert.ok(Math.abs(centerY) < 1e-8);
    else if (kind === 'operation' || kind === 'script') assert.ok(centerY > 0);
    else assert.ok(centerY < 0);
    assert.ok(span(layer, 'x') > height * 1.6);
    assert.ok(span(layer, 'z') > width * 0.55);
    assert.ok(span(layer, 'y') > categoryGap * 0.65, `${kind} fills a substantial height band`);
    assert.ok(layer.every((point) => Math.abs(point.y - centers[kind]) < categoryGap * 0.45),
      `${kind} stays inside its own category band`);
    assert.equal(new Set(layer.map((point) => point.y)).size, layer.length, 'same-category nodes use distinct heights');
    assert.equal(new Set(layer.map((point) => point.z)).size, layer.length, 'same-category nodes do not form depth rows');
    const a = layer[0]; const b = layer[1];
    const u = { x: b.x - a.x, y: b.y - a.y, z: b.z - a.z };
    let normal = { x: 0, y: 0, z: 0 }; let normalSize = 0;
    for (const c of layer.slice(2)) {
      const v = { x: c.x - a.x, y: c.y - a.y, z: c.z - a.z };
      const candidate = { x: u.y * v.z - u.z * v.y, y: u.z * v.x - u.x * v.z, z: u.x * v.y - u.y * v.x };
      const size = Math.hypot(candidate.x, candidate.y, candidate.z);
      if (size > normalSize) { normal = candidate; normalSize = size; }
    }
    const volume = Math.max(...layer.map((point) => Math.abs(normal.x * (point.x - a.x) + normal.y * (point.y - a.y) + normal.z * (point.z - a.z))));
    assert.ok(volume > span(layer, 'x') * span(layer, 'y') * span(layer, 'z') * 0.01, `${kind} is not a tilted plane`);
  }
});

test('small category groups also stagger in height and four nodes occupy actual volume', () => {
  for (const count of [2, 3, 4]) {
    const nodes = Array.from({ length: count }, (_, index) => ({ id: `synthetic_small_${index}`, kind: 'object' }));
    const points = [...studioCategoryPositions(nodes).values()];
    assert.equal(new Set(points.map((point) => point.y)).size, count);
    assert.ok(Math.abs(points.reduce((sum, point) => sum + point.y, 0)) < 1e-8);
    if (count === 4) {
      const vectors = points.slice(1).map((p) => [p.x - points[0].x, p.y - points[0].y, p.z - points[0].z]);
      const [a, b, c] = vectors;
      const determinant = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0]);
      assert.ok(Math.abs(determinant) > 1e5, 'four same-category nodes must not lie in one plane');
    }
  }
});

test('missing categories keep object centered and balance remaining layers without adding nodes', () => {
  const nodes = ['object', 'operation', 'script', 'unknown_a', 'unknown_b'].map((kind) => ({ id: `synthetic_${kind}`, kind }));
  const positions = studioCategoryPositions(nodes);
  assert.equal(positions.size, nodes.length);
  assert.deepEqual(positions.get('synthetic_object'), { x: 0, y: 0, z: 0 });
  const remaining = [...positions].filter(([id]) => id !== 'synthetic_object').map(([, point]) => point);
  assert.equal(remaining.filter((point) => point.y < 0).length, 2);
  assert.equal(remaining.filter((point) => point.y > 0).length, 2);
  assert.deepEqual([...positions], [...studioCategoryPositions([...nodes].reverse())]);
  const sparse = studioCategoryPositions(nodes.slice(0, 3));
  assert.ok(sparse.get('synthetic_operation').y * sparse.get('synthetic_script').y < 0);
});

test('expand all keeps remaining clusters within the visible-node budget', () => {
  const cluster = (count) => ({ memberIds: Array.from({ length: count }, (_, index) => `synthetic_${index}`) });
  const clusters = { a: cluster(300), b: cluster(300), c: cluster(20) };
  const expanded = boundedStudioClusterExpansion(clusters, ['a', 'b', 'c', 'a', 'missing']);
  assert.deepEqual(expanded, ['a', 'c']);
  const rendered = Object.keys(clusters).length + expanded.reduce((count, id) => count + clusters[id].memberIds.length - 1, 0);
  assert.ok(rendered <= 500);
  assert.ok(!expanded.includes('b'), 'the remaining large cluster stays available for individual navigation');
  assert.deepEqual(boundedStudioClusterExpansion(clusters, ['b', ...expanded]), ['b', 'c']);
});

test('empty or small graphs need no special expansion and never produce invalid positions', () => {
  assert.deepEqual(boundedStudioClusterExpansion({}, []), []);
  assert.equal(studioCategoryPositions([]).size, 0);
  const position = studioCategoryPositions([{ id: 'synthetic_a', kind: 'object' }]).get('synthetic_a');
  assert.ok(Number.isFinite(position.x) && Number.isFinite(position.y) && Number.isFinite(position.z));
});
