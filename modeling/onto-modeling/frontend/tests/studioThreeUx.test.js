import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { ThreeGraphRuntime } from '../src/components/studio/legacy/webgl/StudioThreeGraphRuntime.js';

function runtimeFixture() {
  const runtime = new ThreeGraphRuntime();
  runtime.host = { clientWidth: 800, clientHeight: 600, dataset: {} };
  runtime.world = new THREE.Scene();
  runtime.world.fog = new THREE.FogExp2('#ffffff', 0);
  runtime.camera = new THREE.PerspectiveCamera(42, 4 / 3, 1, 100000);
  runtime.camera.position.set(50, 100, 1400);
  runtime.controls = { target: new THREE.Vector3(), update: () => false, minDistance: 110, maxDistance: 40000 };
  runtime.grid = new THREE.Object3D();
  runtime.scene = {
    nodes: [
      { id: 'a', kind: 'object', label: '对象', position: { x: 0, y: 0 }, width: 48, height: 48 },
      { id: 'b', kind: 'operation', label: '操作', position: { x: 800, y: 300 }, width: 48, height: 48 },
    ], edges: [],
  };
  runtime.scene.nodes.forEach((node, index) => {
    const mesh = new THREE.Object3D(); mesh.position.set(index * 800, index * -300, index * 200);
    const material = new THREE.MeshStandardMaterial();
    runtime.nodeViews.set(node.id, { node, mesh, radius: 24, materials: [{ material, opacity: 1, emissiveIntensity: 0 }],
      label: { classList: { toggle() {} }, setAttribute() {}, remove() {} } });
    runtime.positions.set(node.id, { x: mesh.position.x, y: -mesh.position.y });
    runtime.basePositions.set(node.id, { x: index * 800, y: index * 300 });
    runtime.depthFactors.set(node.id, index * 0.5);
  });
  runtime.drawLabels = () => {};
  runtime.drawMinimap = () => {};
  runtime.presentStaticFrame = () => {};
  runtime.reportViewportChange = () => {};
  runtime.startForce = () => {};
  return runtime;
}

test('switching force/category layouts preserves displayed positions and finite depth in both directions', () => {
  const original = globalThis.document;
  globalThis.document = { hidden: false };
  try {
    const runtime = runtimeFixture();
    runtime.layoutMode = 'force';
    const saved = structuredClone(runtime.scene.nodes);
    const originalWorld = [...runtime.nodeViews.values()].map((view) => view.mesh.position.toArray());
    runtime.setLayoutMode('layered');
    assert.ok(runtime.layoutTween, 'visible graphs animate rather than teleport');
    assert.deepEqual([...runtime.nodeViews.values()].map((view) => view.mesh.position.toArray()), originalWorld);
    for (const points of [runtime.layoutTween.from, runtime.layoutTween.to]) {
      assert.ok([...points.values()].every((point) => Object.values(point).every(Number.isFinite)), 'missing force depth must never produce NaN');
    }
    runtime.completeLayoutTween();
    const categoryWorld = [...runtime.nodeViews.values()].map((view) => view.mesh.position.toArray());
    runtime.setLayoutMode('force');
    assert.ok(runtime.layoutTween, 'returning to force also interpolates');
    assert.deepEqual([...runtime.nodeViews.values()].map((view) => view.mesh.position.toArray()), categoryWorld);
    runtime.completeLayoutTween();
    assert.ok([...runtime.nodeViews.values()].every((view) => view.mesh.position.toArray().every(Number.isFinite)));
    assert.deepEqual(runtime.scene.nodes, saved, 'presentation changes do not persist altered model positions');
    runtime.iconLibrary.dispose();
  } finally { globalThis.document = original; }
});

test('reduced motion starts without orbit and still snaps camera and layout', () => {
  const original = globalThis.document;
  globalThis.document = { hidden: false };
  try {
    const runtime = runtimeFixture();
    runtime.moveCamera(new THREE.Vector3(0, 0, 900), new THREE.Vector3(10, 20, 0));
    assert.equal(runtime.cameraTween.duration, 400);
    runtime.setInteractionState({ reducedMotion: true });
    assert.equal(runtime.cameraTween, null);
    assert.deepEqual(runtime.camera.position.toArray(), [0, 0, 900]);
    assert.equal(runtime.autoRotate, false);
    runtime.layoutMode = 'force'; runtime.setLayoutMode('layered');
    assert.equal(runtime.layoutTween, null);
    assert.equal(runtime.controls.enableDamping, false);
    runtime.iconLibrary.dispose();
  } finally { globalThis.document = original; }
});

test('explicit orbit works under reduced motion and survives selection updates until stopped', () => {
  const runtime = runtimeFixture();
  const changes = [];
  runtime.callbacks.onAutoRotateChange = (rotating) => changes.push(rotating);
  runtime.setInteractionState({ reducedMotion: true });
  assert.equal(runtime.setAutoRotate(true), true);
  assert.equal(runtime.controls.autoRotate, true);
  runtime.setInteractionState({ reducedMotion: true, selectedId: 'b' });
  runtime.setInteractionState({ reducedMotion: true, selectedId: 'b', previewKind: 'object' });
  assert.equal(runtime.autoRotate, true, 'ordinary graph updates preserve the explicit user choice');
  assert.equal(runtime.controls.autoRotate, true);
  assert.equal(runtime.controls.enableDamping, false, 'manual orbit does not enable other reduced-motion effects');
  assert.equal(runtime.setAutoRotate(false), false);
  assert.equal(runtime.controls.autoRotate, false, 'stopping takes effect without waiting for the next frame');
  assert.deepEqual(changes, [true, false]);
  runtime.iconLibrary.dispose();
});

test('entering reduced motion stops orbit and reports it, but a new explicit request can restart it', () => {
  const runtime = runtimeFixture();
  const changes = [];
  runtime.callbacks.onAutoRotateChange = (rotating) => changes.push(rotating);
  runtime.setAutoRotate(true);
  runtime.setInteractionState({ reducedMotion: true });
  assert.equal(runtime.autoRotate, false);
  assert.equal(runtime.controls.autoRotate, false);
  assert.deepEqual(changes, [true, false]);
  assert.equal(runtime.setAutoRotate(true), true);
  runtime.setInteractionState({ reducedMotion: true, motionEnabled: false });
  assert.equal(runtime.autoRotate, false);
  assert.equal(runtime.controls.autoRotate, false);
  assert.equal(runtime.setAutoRotate(true), false, 'motionEnabled remains an explicit prohibition');
  runtime.setInteractionState({ reducedMotion: true, motionEnabled: true });
  assert.equal(runtime.autoRotate, false, 'restoring permission never starts orbit automatically');
  runtime.destroyed = true;
  assert.equal(runtime.setAutoRotate(true), false, 'an unmounted runtime cannot restart');
  assert.deepEqual(changes, [true, false, true, false]);
  runtime.iconLibrary.dispose();
});

test('explicit reduced-motion orbit schedules no further frames while the page is hidden', () => {
  const originalDocument = globalThis.document;
  const originalRaf = globalThis.requestAnimationFrame;
  globalThis.document = { hidden: false };
  const scheduled = [];
  globalThis.requestAnimationFrame = (callback) => { scheduled.push(callback); return scheduled.length; };
  try {
    const runtime = runtimeFixture();
    let draws = 0;
    runtime.renderer = { render: () => { draws += 1; } };
    runtime.setInteractionState({ reducedMotion: true });
    runtime.setAutoRotate(true);
    runtime.tick(1000);
    assert.equal(draws, 1);
    const activeFrameCount = scheduled.length;
    globalThis.document.hidden = true;
    runtime.tick(1040);
    assert.equal(draws, 1);
    assert.equal(scheduled.length, activeFrameCount, 'hidden orbit must not keep requesting animation frames');
    assert.equal(runtime.frame, null);
    globalThis.document.hidden = false;
    runtime.tick(1080);
    assert.equal(draws, 2);
    assert.equal(runtime.controls.autoRotate, true, 'returning to the page preserves the explicit orbit choice');
    runtime.setAutoRotate(false);
    const stoppingFrameCount = scheduled.length;
    runtime.tick(1120);
    assert.equal(scheduled.length, stoppingFrameCount, 'stopping returns to an idle graph');
    runtime.iconLibrary.dispose();
  } finally {
    globalThis.document = originalDocument;
    globalThis.requestAnimationFrame = originalRaf;
  }
});

test('stopping orbit clears real OrbitControls inertia without moving the current view or disabling drag damping', () => {
  const runtime = runtimeFixture();
  runtime.controls = new OrbitControls(runtime.camera);
  runtime.controls.enableDamping = true;
  runtime.controls.dampingFactor = 0.075;
  runtime.controls.autoRotateSpeed = 0.45;
  runtime.setAutoRotate(true);
  for (let frame = 0; frame < 12; frame += 1) runtime.controls.update(1 / 60);
  const stoppedPosition = runtime.camera.position.clone();
  const stoppedTarget = runtime.controls.target.clone();
  runtime.setAutoRotate(false);
  assert.ok(runtime.camera.position.distanceTo(stoppedPosition) < 1e-6, 'stopping must not apply the remaining rotation as a jump');
  for (let frame = 0; frame < 24; frame += 1) runtime.controls.update(1 / 60);
  assert.ok(runtime.camera.position.distanceTo(stoppedPosition) < 1e-6, 'low frame rates must not prolong orbit after an explicit stop');
  assert.ok(runtime.controls.target.distanceTo(stoppedTarget) < 1e-6);
  assert.equal(runtime.controls.enableDamping, true);
  runtime.controls.rotateLeft(0.2);
  const draggedPosition = runtime.camera.position.clone();
  runtime.setAutoRotate(false);
  runtime.controls.update(1 / 60);
  assert.ok(runtime.camera.position.distanceTo(draggedPosition) > 1, 'an already stopped orbit must not cancel ordinary drag inertia');
  runtime.iconLibrary.dispose();
});

test('search focus waits for final layout positions and manual fit cancels a pending focus', () => {
  const original = globalThis.document;
  globalThis.document = { hidden: false };
  try {
    const runtime = runtimeFixture();
    runtime.layoutMode = 'force';
    runtime.setLayoutMode('layered');
    runtime.focusNode('a');
    assert.equal(runtime.pendingFocusId, 'a');
    runtime.completeLayoutTween();
    assert.equal(runtime.pendingFocusId, '');
    const firstTarget = runtime.cameraTween.targetTo.toArray();
    assert.ok(runtime.cameraTween.targetTo.distanceTo(runtime.nodeViews.get('a').mesh.position) < 1e-8);
    runtime.fitView(false);
    runtime.focusNode('a');
    assert.deepEqual(runtime.cameraTween.targetTo.toArray(), firstTarget, 'repeating focus after fit uses the same settled node');
    runtime.layoutMode = 'force'; runtime.setLayoutMode('layered');
    runtime.startLayoutTween(new Map([['a', { x: 250, y: -30, z: 60 }], ['b', { x: 400, y: 50, z: 90 }]]));
    runtime.focusNode('a');
    assert.equal(runtime.pendingFocusId, 'a');
    runtime.fitView(false);
    runtime.completeLayoutTween();
    assert.equal(runtime.pendingFocusId, '');
    assert.equal(runtime.focusTrackingId, '');
    assert.equal(runtime.cameraTween, null, 'completing a layout cannot undo explicit fit navigation');
    runtime.iconLibrary.dispose();
  } finally { globalThis.document = original; }
});

test('asynchronous planar scene coordinates cannot overwrite category positions or change repeated focus', () => {
  const runtime = runtimeFixture();
  runtime.createNode = (node) => {
    const mesh = new THREE.Object3D();
    const position = runtime.worldPositionFor(node);
    mesh.position.set(position.x, position.y, position.z);
    runtime.nodeViews.set(node.id, { node, mesh, radius: 24, materials: [], title: {},
      label: { classList: { toggle() {} }, setAttribute() {}, remove() {} } });
  };
  const scene = structuredClone(runtime.scene);
  runtime.setScene(scene);
  runtime.focusNode('a');
  const firstTarget = runtime.cameraTween.targetTo.toArray();
  const displayed = [...runtime.nodeViews.values()].map((view) => view.mesh.position.toArray());
  runtime.setScene({ ...scene, nodes: scene.nodes.map((node, index) => ({ ...node, position: { x: index * 1200 + 900, y: index * 700 + 500 } })) });
  assert.deepEqual([...runtime.nodeViews.values()].map((view) => view.mesh.position.toArray()), displayed);
  assert.equal(runtime.basePositions.get('a').x, 924, 'the new planar layout remains available for force mode');
  runtime.fitView(false); runtime.focusNode('a');
  assert.deepEqual(runtime.cameraTween.targetTo.toArray(), firstTarget);
  runtime.iconLibrary.dispose();
});

test('focus after force updates uses settled positions and explicit pan cancels the deferred request', () => {
  const runtime = runtimeFixture();
  runtime.forceStatus = 'running';
  runtime.focusNode('a');
  assert.equal(runtime.pendingFocusId, 'a');
  runtime.nodeViews.get('a').mesh.position.set(350, -200, 90);
  runtime.forceStatus = 'settled';
  runtime.flushPendingFocus();
  assert.deepEqual(runtime.cameraTween.targetTo.toArray(), [350, -200, 90]);
  runtime.forceStatus = 'running'; runtime.focusNode('a');
  runtime.setCenter(0, 0);
  runtime.forceStatus = 'settled'; runtime.flushPendingFocus();
  assert.equal(runtime.pendingFocusId, '');
  assert.equal(runtime.cameraTween, null);
  runtime.iconLibrary.dispose();
});

test('highlighted edges use a reusable CSS-pixel wide overlay without rebuilding ordinary geometry', () => {
  const runtime = runtimeFixture();
  runtime.arrowGeometry = new THREE.ConeGeometry(3.6, 11, 7);
  runtime.particleGeometry = new THREE.SphereGeometry(1, 6, 4);
  runtime.createEdge({ id: 'ab', source: 'a', target: 'b' });
  runtime.updateEdgePositions();
  const view = runtime.edgeViews.get('ab');
  const ordinaryGeometry = view.line.geometry;
  runtime.setInteractionState({ activeEdgeIds: new Set(['ab']), hasHighlight: true });
  const wide = view.emphasisLine;
  assert.equal(wide.material.worldUnits, false);
  assert.equal(wide.material.linewidth, 2.5);
  assert.deepEqual(wide.material.resolution.toArray(), [800, 600]);
  assert.equal(wide.visible, true);
  assert.equal(view.line.visible, false);
  runtime.setInteractionState({});
  assert.equal(wide.visible, false);
  assert.equal(view.line.visible, true);
  runtime.setInteractionState({ activeEdgeIds: new Set(['ab']) });
  assert.equal(view.emphasisLine, wide);
  assert.equal(view.line.geometry, ordinaryGeometry);
  runtime.nodeViews.clear(); runtime.clearViews();
  runtime.arrowGeometry.dispose(); runtime.particleGeometry.dispose(); runtime.iconLibrary.dispose();
});

test('legend preview dims other kinds without replacing selection and restores their materials', () => {
  const runtime = runtimeFixture();
  runtime.setInteractionState({ previewKind: 'object' });
  const object = runtime.nodeViews.get('a').materials[0].material;
  const operation = runtime.nodeViews.get('b').materials[0].material;
  assert.equal(object.opacity, 1);
  assert.equal(operation.opacity, 0.2);
  runtime.setInteractionState({});
  assert.equal(operation.opacity, 1);
  runtime.iconLibrary.dispose();
});

test('minimap hover reports the nearest node only within a useful hit radius', () => {
  const names = [];
  const runtime = new ThreeGraphRuntime({ onMinimapHover: (name) => names.push(name) });
  runtime.minimap = { getBoundingClientRect: () => ({ left: 100, top: 200 }) };
  runtime.minimapNodes = [{ x: 20, y: 30, label: '订单' }, { x: 25, y: 32, label: '支付' }];
  runtime.handleMinimapHover({ clientX: 124, clientY: 232 });
  runtime.handleMinimapHover({ clientX: 200, clientY: 270 });
  assert.deepEqual(names, ['支付', '']);
  runtime.iconLibrary.dispose();
});

test('restoring a blank or unusably distant viewport fits the graph while keeping a modest user pan', () => {
  const runtime = runtimeFixture();
  runtime.controls.update = () => { runtime.camera.lookAt(runtime.controls.target); return false; };
  let fits = 0;
  const fit = runtime.fitView.bind(runtime);
  runtime.fitView = (...args) => { fits += 1; return fit(...args); };
  runtime.setViewport({ camera3d: { position: [300, 100, 1600], target: [300, 100, 0] } });
  assert.equal(fits, 0, 'a normal offset view remains exactly where the user left it');
  assert.deepEqual(runtime.camera.position.toArray(), [300, 100, 1600]);
  runtime.setViewport({ camera3d: { position: [80000, 80000, 90000], target: [80000, 80000, 0] } });
  assert.equal(fits, 1, 'a cached view with no visible nodes recovers automatically');
  assert.equal(runtime.host.dataset.viewportRecovered, 'true');
  runtime.setViewport({ camera3d: { position: [400, 0, 90000], target: [400, 0, 0] } });
  assert.equal(fits, 2, 'an entire graph shrunk to a few indistinguishable pixels also recovers');
  runtime.nodeViews.clear();
  runtime.setViewport({ camera3d: { position: [80000, 80000, 90000], target: [80000, 80000, 0] } });
  assert.equal(fits, 2, 'empty initial scenes are not interpreted as invalid saved views');
  assert.equal(runtime.restoredViewportPending, true);
  runtime.iconLibrary.dispose();
});

test('idle runtime renders one dirty frame and stops scheduling frames; moving labels update each rendered frame', () => {
  const oldDocument = globalThis.document;
  const oldRaf = globalThis.requestAnimationFrame;
  globalThis.document = { hidden: false };
  const scheduled = [];
  globalThis.requestAnimationFrame = (callback) => { scheduled.push(callback); return scheduled.length; };
  try {
    const runtime = runtimeFixture();
    let draws = 0; let labels = 0;
    runtime.renderer = { render: () => { draws += 1; } };
    runtime.drawLabels = () => { labels += 1; };
    runtime.tick(1000);
    assert.equal(draws, 1);
    assert.equal(scheduled.length, 0, 'a static graph must not keep a render loop alive');
    runtime.tick(1040);
    assert.equal(draws, 1, 'an unchanged frame must not draw');
    runtime.controlsActive = true;
    runtime.controls.update = () => true;
    runtime.tick(1060); runtime.tick(1080);
    assert.equal(draws, 3);
    assert.equal(labels, 3, 'both camera-motion frames update labels synchronously');
    assert.ok(scheduled.length >= 2);
    runtime.iconLibrary.dispose();
  } finally { globalThis.document = oldDocument; globalThis.requestAnimationFrame = oldRaf; }
});
