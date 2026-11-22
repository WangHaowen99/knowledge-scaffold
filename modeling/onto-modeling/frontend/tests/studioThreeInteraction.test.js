import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as THREE from 'three';
import { ThreeGraphRuntime } from '../src/components/studio/legacy/webgl/StudioThreeGraphRuntime.js';

function createRuntime(callbacks) {
  const runtime = new ThreeGraphRuntime(callbacks);
  runtime.host = { releasePointerCapture() {} };
  runtime.controls = { enabled: false };
  return runtime;
}

test('display labels shows only element names and follows the highlighted neighborhood', () => {
  const runtime = new ThreeGraphRuntime();
  runtime.host = { dataset: {}, clientWidth: 800, clientHeight: 600 };
  runtime.camera = {};
  runtime.labelLayer = { appendChild: () => assert.fail('relationships must not create label elements') };
  runtime.world = new THREE.Group();
  runtime.arrowGeometry = new THREE.ConeGeometry(1, 2, 6);
  runtime.particleGeometry = new THREE.SphereGeometry(1, 6, 4);
  runtime.project = () => ({ x: 200, y: 200, visible: true });
  runtime.updateEmphasis = () => {};
  const label = () => ({ hidden: false, style: {} });
  const nodes = ['object', 'operation', 'script', 'metric', 'rule', 'cluster', 'attribute', 'testcase', 'data-mock'].map((kind) => ({
    node: { id: `synthetic_${kind}`, kind, label: `Synthetic ${kind}` }, mesh: { position: {} }, label: label(),
  }));
  const edges = [
    { id: 'synthetic_edge_a', source: nodes[0].node.id, target: nodes[1].node.id },
    { id: 'synthetic_edge_b', source: nodes[1].node.id, target: nodes[2].node.id },
  ].map((edge) => ({ ...edge, label: 'Synthetic relationship' }));
  nodes.forEach((view) => {
    runtime.nodeViews.set(view.node.id, view);
    runtime.nodeById.set(view.node.id, view.node);
  });
  edges.forEach((edge) => runtime.createEdge(edge));
  assert.ok([...runtime.edgeViews.values()].every((view) => !view.label), 'edges keep their geometry without a text view');
  assert.equal(runtime.edgeViews.get(edges[0].id).edge.label, 'Synthetic relationship', 'relationship data remains available');
  runtime.interactionState = {
    ...runtime.interactionState, selectedId: nodes[0].node.id, hasHighlight: true,
    activeNodeIds: new Set(nodes.slice(0, 2).map((view) => view.node.id)),
    activeEdgeIds: new Set(edges.map((edge) => edge.id)),
  };
  runtime.hoveredNodeId = nodes[2].node.id;
  runtime.hoveredEdgeId = edges[1].id;
  const labels = nodes.map((view) => view.label);
  const visibleNodes = () => nodes.filter((view) => !view.label.hidden).map((view) => view.node.id);

  runtime.drawLabels();
  assert.ok(labels.every((item) => item.hidden), 'selection and hover never force persistent labels on');
  assert.equal(runtime.host.dataset.visibleNodeLabels, '0');

  runtime.setPresentation({ bloom: false, labels: true });
  assert.deepEqual(visibleNodes(), nodes.slice(0, 2).map((view) => view.node.id));
  assert.equal(runtime.host.dataset.visibleNodeLabels, '2');

  runtime.setInteractionState({ selectedId: nodes[2].node.id, hasHighlight: true });
  assert.deepEqual(visibleNodes(), [nodes[2].node.id], 'changing selection immediately removes the previous labels');
  assert.equal(runtime.host.dataset.visibleNodeLabels, '1');

  runtime.setInteractionState({ hasHighlight: true, activeNodeIds: new Set(nodes.slice(1).map((view) => view.node.id)) });
  assert.deepEqual(visibleNodes(), nodes.slice(1, 5).map((view) => view.node.id), 'chain highlighting excludes auxiliary element text');

  runtime.setInteractionState({});
  assert.deepEqual(visibleNodes(), nodes.slice(0, 5).map((view) => view.node.id), 'clearing highlight restores the five element kinds only');
  assert.equal(runtime.host.dataset.visibleNodeLabels, '5');

  runtime.renderer = { domElement: { style: {} } };
  runtime.nodeTooltip = {};
  runtime.setHover(nodes[0].node.id, null);
  assert.equal(runtime.nodeTooltip.textContent, nodes[0].node.label);
  assert.equal(runtime.nodeTooltip.hidden, false);
  runtime.setHover(null, edges[0].id);
  assert.equal(runtime.nodeTooltip.hidden, true, 'hovering an edge never displays its relationship label');
  assert.equal(runtime.nodeTooltip.textContent, '', 'moving from a node to an edge clears stale tooltip text');
  assert.equal(runtime.hoveredEdgeId, edges[0].id, 'edge hover highlighting remains available');
  nodes.slice(5).forEach((view) => {
    runtime.setHover(view.node.id, null);
    assert.equal(runtime.nodeTooltip.hidden, true, `${view.node.kind} is not one of the five named knowledge elements`);
  });

  runtime.setPresentation({ labels: false });
  assert.ok(labels.every((item) => item.hidden), 'closing the switch hides labels without scheduling a frame');
  assert.equal(runtime.host.dataset.visibleNodeLabels, '0');
  runtime.setInteractionState({ selectedId: nodes[0].node.id, hasHighlight: true });
  assert.ok(labels.every((item) => item.hidden), 'a later selection cannot override the disabled label switch');
  runtime.nodeViews.clear();
  runtime.clearViews();
  runtime.arrowGeometry.dispose();
  runtime.particleGeometry.dispose();
  runtime.iconLibrary.dispose();
});

test('selecting an object keeps attributes in metadata and never creates extra graph nodes', () => {
  const runtime = new ThreeGraphRuntime();
  runtime.host = { dataset: {} };
  runtime.world = new THREE.Group();
  const object = {
    id: 'synthetic_object', kind: 'object',
    attributes: [{ key: 'synthetic_field', label: 'Synthetic field', type: 'string' }],
  };
  const original = structuredClone(object);
  runtime.nodeById.set(object.id, object);
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(), new THREE.MeshStandardMaterial());
  const line = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial());
  let disposed = 0;
  [mesh.geometry, mesh.material, line.geometry, line.material].forEach((resource) => {
    resource.addEventListener('dispose', () => { disposed += 1; });
  });
  let labelRemoved = false;
  runtime.world.add(mesh, line);
  runtime.attributeViews = [{ mesh, line, label: { remove: () => { labelRemoved = true; } } }];
  let highlightsUpdated = 0;
  runtime.updateEmphasis = () => { highlightsUpdated += 1; };
  runtime.syncFocusedObjectPin = () => {};

  runtime.setInteractionState({ selectedId: object.id });
  runtime.updateAttributes();

  assert.equal(runtime.interactionState.selectedId, object.id);
  assert.equal(highlightsUpdated, 1, 'selection still updates graph highlighting');
  assert.equal(runtime.host.dataset.attributeOrbitNodeCount, '0');
  assert.deepEqual(runtime.attributeViews, []);
  assert.equal(runtime.world.children.length, 0);
  assert.equal(disposed, 4, 'existing attribute geometry and materials are released');
  assert.equal(labelRemoved, true);
  assert.deepEqual(object, original, 'summary and editor can still read the full attributes');
});

test('node click selects and a second click opens the existing editor callback', () => {
  const selected = [];
  const opened = [];
  const runtime = createRuntime({ onNodeClick: (id) => selected.push(id), onNodeDoubleClick: (id) => opened.push(id) });
  for (let count = 0; count < 2; count += 1) {
    runtime.pointerAction = { nodeId: 'synthetic_node_a', pointerId: 1, moved: false };
    runtime.pointerUp({ pointerId: 1, button: 0 });
  }
  assert.deepEqual(selected, ['synthetic_node_a', 'synthetic_node_a']);
  assert.deepEqual(opened, ['synthetic_node_a']);
  assert.equal(runtime.controls.enabled, true);
});

test('node dragging retains planar persistence and does not trigger selection or editing', () => {
  const positions = [];
  let selected = false;
  const runtime = createRuntime({ onNodeDragEnd: (id, position) => positions.push({ id, position }), onNodeClick: () => { selected = true; } });
  runtime.positions.set('synthetic_node_a', { x: 132, y: -45, z: 700 });
  runtime.depthOverrides.set('synthetic_node_a', 480);
  runtime.pointerAction = { nodeId: 'synthetic_node_a', pointerId: 1, moved: true };
  runtime.pointerUp({ pointerId: 1, button: 0 });
  assert.deepEqual(positions, [{ id: 'synthetic_node_a', position: { x: 132, y: -45 } }]);
  assert.equal(runtime.depthOverrides.get('synthetic_node_a'), 480);
  assert.equal(selected, false);
  assert.equal(runtime.isPinned('synthetic_node_a'), true);
});

test('category world positions use explicit depth while force mode keeps its separate depth layout', () => {
  const runtime = createRuntime({});
  const node = { id: 'synthetic_node_a' };
  runtime.positions.set(node.id, { x: 40, y: -60, z: 300 });
  runtime.depthFactors.set(node.id, 0.5);
  runtime.depthSpan = 2000;
  runtime.layoutMode = 'layered';
  assert.deepEqual(runtime.worldPositionFor(node), { x: 40, y: 60, z: 300 });
  runtime.depthOverrides.set(node.id, 420);
  assert.equal(runtime.worldPositionFor(node).z, 420);
  runtime.depthOverrides.clear();
  runtime.layoutMode = 'force';
  assert.deepEqual(runtime.worldPositionFor(node), { x: 40, y: 60, z: 1000 });
});

test('a background layout switch presents one static frame without waiting for animation', () => {
  const previousDocument = globalThis.document;
  globalThis.document = { hidden: true };
  try {
    const runtime = createRuntime({});
    runtime.layoutMode = 'force';
    runtime.scene.nodes = [{ id: 'synthetic_object', kind: 'object' }];
    runtime.stopForce = () => {};
    runtime.applyPositions = () => {};
    let animated;
    let frames = 0;
    runtime.fitView = (value) => { animated = value; };
    runtime.presentStaticFrame = () => { frames += 1; };
    runtime.setLayoutMode('layered');
    assert.equal(animated, false);
    assert.equal(frames, 1);
    runtime.setLayoutMode('layered');
    assert.equal(frames, 1);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test('orbit gestures preserve selection while a stationary background click clears it', () => {
  let cleared = 0;
  const runtime = createRuntime({ onBackgroundClick: () => { cleared += 1; } });
  runtime.pointerAction = { pointerId: 1, moved: true };
  runtime.pointerUp({ pointerId: 1, button: 0 });
  assert.equal(cleared, 0);
  runtime.pointerAction = { pointerId: 1, moved: false };
  runtime.pointerUp({ pointerId: 1, button: 0 });
  assert.equal(cleared, 1);
});

test('focus fits distant first-hop neighbors while retaining direction and selection return view', () => {
  const runtime = createRuntime({});
  runtime.camera = new THREE.PerspectiveCamera(42, 1.6, 1, 100000);
  runtime.camera.position.set(900, 200, 1500);
  runtime.controls.target = new THREE.Vector3();
  runtime.camera.lookAt(runtime.controls.target);
  runtime.fitDistance = 2100;
  runtime.scene.edges = [
    { source: 'synthetic_node_a', target: 'synthetic_node_b' },
    { source: 'synthetic_node_c', target: 'synthetic_node_a' },
  ];
  runtime.nodeViews = new Map([
    ['synthetic_node_a', { mesh: { position: new THREE.Vector3(0, 0, 0) }, radius: 20 }],
    ['synthetic_node_b', { mesh: { position: new THREE.Vector3(1200, 0, 50) }, radius: 20 }],
    ['synthetic_node_c', { mesh: { position: new THREE.Vector3(600, 600, -50) }, radius: 20 }],
    ['synthetic_unrelated', { mesh: { position: new THREE.Vector3(6000, 6000, 0) }, radius: 20 }],
  ]);
  const snapshot = { position: [900, 200, 1500], target: [0, 0, 0] };
  runtime.selectionCameraSnapshot = snapshot;
  let move;
  runtime.moveCamera = (position, target) => { move = { position, target }; };
  const originalDirection = runtime.camera.position.clone().normalize();
  runtime.focusNode('synthetic_node_a');
  const focusedOffset = move.position.clone().sub(move.target);
  assert.ok(focusedOffset.length() > 680);
  assert.ok(focusedOffset.length() <= runtime.fitDistance + 0.001);
  assert.ok(focusedOffset.normalize().distanceTo(originalDirection) < 0.00001);
  assert.equal(move.target.x, 600);
  assert.equal(move.target.y, 300);
  assert.equal(runtime.selectionCameraSnapshot, snapshot);
  runtime.restoreSelectionView();
  assert.deepEqual(move.position.toArray(), snapshot.position);
  assert.deepEqual(move.target.toArray(), snapshot.target);
  assert.equal(runtime.selectionCameraSnapshot, null);
});
