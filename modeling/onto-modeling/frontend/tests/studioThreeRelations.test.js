import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as THREE from 'three';
import { ThreeGraphRuntime } from '../src/components/studio/legacy/webgl/StudioThreeGraphRuntime.js';

// Synthetic data; only DOM, rendering and Worker boundaries are substituted.
// Scene construction, edge geometry and force initialization use the real runtime.
function runtimeFor(t, edges) {
  const originalDocument = globalThis.document;
  globalThis.document = {
    hidden: false,
    createElement: () => ({
      dataset: {}, style: { setProperty() {} }, classList: { toggle() {} },
      setAttribute() {}, addEventListener() {}, append() {}, remove() {},
    }),
  };
  const runtime = new ThreeGraphRuntime();
  runtime.host = { clientWidth: 800, clientHeight: 600, dataset: {} };
  runtime.world = new THREE.Scene();
  runtime.camera = new THREE.PerspectiveCamera(42, 4 / 3, 1, 100000);
  runtime.controls = { target: new THREE.Vector3(), update() {} };
  runtime.grid = new THREE.Object3D();
  runtime.renderer = { render() {} };
  runtime.labelLayer = { appendChild() {} };
  runtime.arrowGeometry = new THREE.ConeGeometry(1, 2, 6);
  runtime.particleGeometry = new THREE.SphereGeometry(1, 6, 4);
  t.after(() => {
    clearTimeout(runtime.viewportTimer);
    runtime.stopForce(false);
    runtime.clearViews();
    runtime.iconLibrary.dispose();
    runtime.arrowGeometry.dispose();
    runtime.particleGeometry.dispose();
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  });
  runtime.setScene({
    nodes: ['synthetic_a', 'synthetic_b'].map((id, index) => ({
      id, kind: 'object', label: id, width: 48, height: 48,
      position: { x: index * 400, y: 0 },
    })), edges,
  });
  runtime.nodeViews.get('synthetic_a').mesh.position.set(-200, -20, 10);
  runtime.nodeViews.get('synthetic_b').mesh.position.set(220, 60, 180);
  runtime.updateEdgePositions();
  return runtime;
}

const relation = (id, source = 'synthetic_a', target = 'synthetic_b') => ({ id, source, target, kind: 'object_relation' });
const midpoint = (runtime, id) => runtime.edgeViews.get(id).curve.getPoint(0.5);

test('Three separates same-direction relationships in planar and depth-aligned scenes', (t) => {
  const runtime = runtimeFor(t, [relation('synthetic_first'), relation('synthetic_second')]);
  for (const target of [[220, 60, 180], [-200, -20, 410]]) {
    runtime.nodeViews.get('synthetic_b').mesh.position.set(...target);
    runtime.updateEdgePositions();
    assert.ok(midpoint(runtime, 'synthetic_first').distanceTo(midpoint(runtime, 'synthetic_second')) > 20);
    for (const view of runtime.edgeViews.values()) {
      assert.ok(view.curve.getPoints(20).every((point) => point.toArray().every(Number.isFinite)));
    }
  }
});

test('Three separates reciprocal relationships and keeps arrows pointing along each direction', (t) => {
  const runtime = runtimeFor(t, [relation('synthetic_out'), relation('synthetic_back', 'synthetic_b', 'synthetic_a')]);
  assert.ok(midpoint(runtime, 'synthetic_out').distanceTo(midpoint(runtime, 'synthetic_back')) > 20);
  for (const view of runtime.edgeViews.values()) {
    const tangent = view.curve.getTangent(1).normalize();
    const arrowDirection = new THREE.Vector3(0, 1, 0).applyQuaternion(view.arrow.quaternion);
    assert.ok(arrowDirection.dot(tangent) > 0.999999);
    assert.ok(view.arrow.position.distanceTo(view.curve.getPoint(1)) < 1e-9);
  }
});

test('Three nests self-relations independently per node with boundary endpoints and returning arrows', (t) => {
  const runtime = runtimeFor(t, [
    relation('synthetic_inner', 'synthetic_a', 'synthetic_a'),
    relation('synthetic_outer', 'synthetic_a', 'synthetic_a'),
    relation('synthetic_other', 'synthetic_b', 'synthetic_b'),
  ]);
  const relativeTop = (id) => {
    const view = runtime.edgeViews.get(id);
    const node = runtime.nodeViews.get(view.edge.source);
    return Math.max(...view.curve.getPoints(20).map((point) => point.y - node.mesh.position.y));
  };
  assert.ok(relativeTop('synthetic_outer') > relativeTop('synthetic_inner') + 15);
  assert.ok(Math.abs(relativeTop('synthetic_other') - relativeTop('synthetic_inner')) < 1e-9);
  for (const view of runtime.edgeViews.values()) {
    const node = runtime.nodeViews.get(view.edge.source);
    const start = view.curve.getPoint(0);
    const end = view.curve.getPoint(1);
    assert.ok(Math.abs(start.distanceTo(node.mesh.position) - node.radius) < 1e-9);
    assert.ok(Math.abs(end.distanceTo(node.mesh.position) - node.radius) < 1e-9);
    assert.ok(start.y > node.mesh.position.y && end.y > node.mesh.position.y);
    const inward = node.mesh.position.clone().sub(end).normalize();
    const arrowDirection = new THREE.Vector3(0, 1, 0).applyQuaternion(view.arrow.quaternion);
    assert.ok(arrowDirection.dot(inward) > 0.5, 'the terminal arrow returns into its own node');
    assert.ok(arrowDirection.dot(view.curve.getTangent(1).normalize()) > 0.999999);
    assert.ok(view.arrow.position.distanceTo(end) < 1e-9);
  }
  const before = midpoint(runtime, 'synthetic_outer');
  const movement = new THREE.Vector3(35, -70, 120);
  runtime.nodeViews.get('synthetic_a').mesh.position.add(movement);
  runtime.updateEdgePositions();
  assert.ok(midpoint(runtime, 'synthetic_outer').sub(before).distanceTo(movement) < 1e-9);
});

test('Three force initialization excludes self-relations while keeping their rendered curves', (t) => {
  const runtime = runtimeFor(t, [
    relation('synthetic_self', 'synthetic_a', 'synthetic_a'),
    relation('synthetic_link'), relation('synthetic_parallel'),
    relation('synthetic_reverse', 'synthetic_b', 'synthetic_a'),
  ]);
  const originalWorker = globalThis.Worker;
  const messages = [];
  globalThis.Worker = class {
    postMessage(message) { messages.push(message); }
    terminate() {}
  };
  t.after(() => {
    if (originalWorker === undefined) delete globalThis.Worker;
    else globalThis.Worker = originalWorker;
  });
  runtime.startForce();
  const init = messages.find((message) => message.type === 'init');
  assert.deepEqual(init.links, [{ source: 'synthetic_a', target: 'synthetic_b', kind: 'object_relation' }]);
  assert.equal(runtime.edgeViews.size, 4);
  assert.ok(runtime.edgeViews.get('synthetic_self').curve.getLength() > 0);
});

test('Three preserves the existing single-edge depth arc and boundary clearance', (t) => {
  const runtime = runtimeFor(t, [relation('synthetic_single')]);
  const source = runtime.nodeViews.get('synthetic_a');
  const target = runtime.nodeViews.get('synthetic_b');
  const delta = target.mesh.position.clone().sub(source.mesh.position);
  const direction = delta.clone().normalize();
  const start = source.mesh.position.clone().addScaledVector(direction, source.radius + 2);
  const end = target.mesh.position.clone().addScaledVector(direction, -target.radius - 5);
  const control = start.clone().lerp(end, 0.5);
  control.z += Math.min(60, delta.length() * 0.12);
  const expected = new THREE.QuadraticBezierCurve3(start, control, end);
  const actual = runtime.edgeViews.get('synthetic_single').curve;
  for (const t of [0, 0.25, 0.5, 0.75, 1]) assert.ok(actual.getPoint(t).distanceTo(expected.getPoint(t)) < 1e-9);
});
