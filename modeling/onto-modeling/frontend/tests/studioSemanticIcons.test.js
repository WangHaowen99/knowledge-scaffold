import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as THREE from 'three';
import { createSemanticIconLibrary, disposeIconInstance, resolveIconKind } from '../src/components/studio/legacy/webgl/semantic-icons.js';
import { ThreeGraphRuntime, setStudioNodeMaterialEmphasis, studioNodeIdFromHit, studioNodeMaterialStates } from '../src/components/studio/legacy/webgl/StudioThreeGraphRuntime.js';
import { semanticIconRoleColors } from '../src/components/studio/legacy/webgl/semantic-icon-colors.js';
import { resolveGraphTheme } from '../src/components/studio/legacy/webgl/graph-theme.js';
import { createGraphLighting } from '../src/components/studio/legacy/webgl/graph-lighting.js';

// Synthetic nodes only; these checks require no browser or WebGL context.
test('ray hits on semantic icon descendants resolve to the original editable node', () => {
  const library = createSemanticIconLibrary();
  const icon = library.createIcon('object');
  try {
    icon.userData.nodeId = 'synthetic_object_a';
    icon.updateMatrixWorld(true);
    const ray = new THREE.Raycaster(new THREE.Vector3(0, 0, 5), new THREE.Vector3(0, 0, -1));
    const hit = ray.intersectObjects([icon], true)[0];
    assert.ok(hit?.object.isMesh, 'the icon exposes actual geometry for pointer selection');
    assert.notEqual(hit.object, icon);
    assert.equal(studioNodeIdFromHit(hit.object), 'synthetic_object_a');
    assert.equal(studioNodeIdFromHit(undefined), undefined);
    assert.equal(resolveIconKind('entity'), 'object');
    assert.equal(resolveIconKind('logic'), 'operation');
    assert.equal(resolveIconKind('scripts'), 'script');
  } finally {
    disposeIconInstance(icon);
    library.dispose();
  }
});

test('selecting or muting one icon preserves another instance and its material color hierarchy', () => {
  const library = createSemanticIconLibrary();
  const first = library.createIcon('script');
  const second = library.createIcon('script');
  try {
    const firstStates = studioNodeMaterialStates(first);
    const secondStates = studioNodeMaterialStates(second);
    const colors = firstStates.map(({ material }) => material.color.getHex());
    assert.ok(new Set(colors).size > 1, 'body, folds and code retain the approved distinct shades');
    const secondMaterials = new Set(secondStates.map(({ material }) => material));
    assert.ok(firstStates.every(({ material }) => !secondMaterials.has(material)));
    setStudioNodeMaterialEmphasis(firstStates, { muted: true, selected: true });
    assert.ok(firstStates.every(({ material, opacity }) => material.opacity === opacity * 0.2));
    assert.ok(secondStates.every(({ material, opacity, emissiveIntensity }) => material.opacity === opacity && material.emissiveIntensity === emissiveIntensity));
    assert.deepEqual(firstStates.map(({ material }) => material.color.getHex()), colors);
    setStudioNodeMaterialEmphasis(firstStates);
    assert.ok(firstStates.every(({ material, opacity, emissiveIntensity }) => material.opacity === opacity && material.emissiveIntensity === emissiveIntensity));
  } finally {
    disposeIconInstance(first);
    disposeIconInstance(second);
    library.dispose();
  }
});

test('removing a node leaves shared geometry alive until its runtime library is released', () => {
  const library = createSemanticIconLibrary();
  const first = library.createIcon('rule');
  const second = library.createIcon('rule');
  const firstGeometries = new Set();
  const secondGeometries = new Set();
  first.traverse((part) => { if (part.geometry) firstGeometries.add(part.geometry); });
  second.traverse((part) => { if (part.geometry) secondGeometries.add(part.geometry); });
  let geometryDisposals = 0;
  firstGeometries.forEach((geometry) => geometry.addEventListener('dispose', () => { geometryDisposals += 1; }));
  assert.deepEqual(firstGeometries, secondGeometries);
  disposeIconInstance(first);
  assert.equal(geometryDisposals, 0);
  disposeIconInstance(second);
  assert.equal(geometryDisposals, 0);
  library.dispose();
  assert.equal(geometryDisposals, firstGeometries.size);
});

test('camera-facing icon orientation keeps 3D tilt, positions and aggregate orientation intact', () => {
  const runtime = new ThreeGraphRuntime();
  const icon = runtime.iconLibrary.createIcon('rule');
  const cluster = new THREE.Group();
  runtime.camera = new THREE.PerspectiveCamera();
  runtime.camera.position.set(500, 100, 100);
  runtime.camera.lookAt(0, 0, 0);
  icon.position.set(40, 50, 60);
  runtime.nodeViews.set('synthetic_rule', { mesh: icon, semanticIcon: true });
  runtime.nodeViews.set('synthetic_cluster', { mesh: cluster, semanticIcon: false });
  const clusterRotation = cluster.quaternion.clone();
  runtime.orientNodeIcons();
  const relative = runtime.camera.quaternion.clone().invert().multiply(icon.quaternion);
  assert.ok(relative.angleTo(runtime.iconTilt) < 1e-6);
  assert.deepEqual(icon.position.toArray(), [40, 50, 60]);
  assert.ok(cluster.quaternion.angleTo(clusterRotation) < 1e-6);
  disposeIconInstance(icon);
  runtime.iconLibrary.dispose();
});

test('theme switching preserves geometry and refreshes emphasis baselines for existing and new icons', () => {
  const library = createSemanticIconLibrary();
  const icon = library.createIcon('object');
  const states = studioNodeMaterialStates(icon);
  const geometry = icon.children.map((part) => part.geometry);
  const colors = states.map(({ material }) => material.color.getHex());
  let next;
  try {
    setStudioNodeMaterialEmphasis(states, { muted: true });
    library.setTheme('dark');
    setStudioNodeMaterialEmphasis(states, { selected: true });
    assert.deepEqual(icon.children.map((part) => part.geometry), geometry);
    assert.notDeepEqual(states.map(({ material }) => material.color.getHex()), colors);
    assert.ok(states.every(({ material }) => material.opacity === 1 && !material.transparent && material.depthWrite));
    assert.ok(states.every(({ material }) => material.emissiveIntensity > material.userData.semanticIconBase.emissiveIntensity));
    next = library.createIcon('object');
    assert.deepEqual(studioNodeMaterialStates(next).map(({ material }) => material.color.getHex()),
      states.map(({ material }) => material.color.getHex()));
    library.setTheme('light');
    setStudioNodeMaterialEmphasis(states);
    assert.deepEqual(states.map(({ material }) => material.color.getHex()), colors);
    assert.ok(states.every(({ material }) => material.emissiveIntensity === material.userData.semanticIconBase.emissiveIntensity));
  } finally {
    disposeIconInstance(icon);
    disposeIconInstance(next);
    library.dispose();
  }
});

test('all five icon materials share the opaque legend role palette through theme changes', () => {
  const library = createSemanticIconLibrary();
  const kinds = ['object', 'operation', 'rule', 'script', 'metric'];
  const icons = kinds.map((kind) => library.createIcon(kind));
  try {
    for (const mode of ['light', 'dark', 'light']) {
      library.setTheme(mode);
      for (const [index, icon] of icons.entries()) {
        const roles = semanticIconRoleColors(kinds[index], mode);
        assert.equal(roles.body, resolveGraphTheme(mode).colors[kinds[index]]);
        const body = new THREE.Color(roles.body);
        const expectedLight = body.clone().lerp(new THREE.Color('#ffffff'), 0.22);
        assert.equal(roles.light, `#${expectedLight.getHexString()}`);
        for (const { material } of studioNodeMaterialStates(icon)) {
          const role = material.userData.semanticIconRole;
          assert.equal(material.color.getHexString(), roles[role].slice(1).toLowerCase());
          assert.equal(material.emissiveIntensity, resolveGraphTheme(mode).material.emissiveIntensity);
          assert.equal(material.opacity, 1);
        }
      }
    }
  } finally {
    icons.forEach(disposeIconInstance);
    library.dispose();
  }
});

test('neutral lighting keeps view-relative directions stable through orbit, pan and theme changes', () => {
  const lighting = createGraphLighting();
  const camera = new THREE.PerspectiveCamera();
  const directions = () => Object.entries(lighting.lights).filter(([, light]) => light.isDirectionalLight)
    .map(([, light]) => light.getWorldPosition(new THREE.Vector3())
      .sub(light.target.getWorldPosition(new THREE.Vector3())).normalize()
      .applyQuaternion(camera.quaternion.clone().invert()));
  camera.position.set(0, 0, 100);
  camera.lookAt(0, 0, 0);
  lighting.followCamera(camera);
  lighting.rig.updateMatrixWorld(true);
  const initial = directions();
  for (const [position, target] of [
    [[100, 0, 0], [0, 0, 0]], [[0, 0, -100], [0, 0, 0]], [[340, -70, 20], [240, -70, 20]],
  ]) {
    camera.position.set(...position);
    camera.lookAt(...target);
    lighting.followCamera(camera);
    lighting.rig.updateMatrixWorld(true);
    directions().forEach((direction, index) => assert.ok(direction.distanceTo(initial[index]) < 1e-10));
    assert.deepEqual(lighting.rig.position.toArray(), [0, 0, 0]);
  }
  for (const mode of ['dark', 'light']) {
    lighting.setTheme(mode);
    for (const [role, light] of Object.entries(lighting.lights)) {
      assert.equal(light.color.getHexString(), 'ffffff');
      assert.equal(light.intensity, resolveGraphTheme(mode).lights[role]);
    }
  }
});
