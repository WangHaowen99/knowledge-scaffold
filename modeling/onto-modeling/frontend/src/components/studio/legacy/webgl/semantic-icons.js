// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { resolveGraphTheme } from './graph-theme.js';
import { semanticIconRoleColors } from './semantic-icon-colors.js';

// Geometry and type colors preserve the approved semantic icon designs.
// Each library owns its geometry cache. Instances share geometry, but never mutable materials.
export const ICON_TYPES = Object.freeze([
  { key: 'object', label: '对象', color: '#3A739D', meaning: '业务实体' },
  { key: 'operation', label: '操作', color: '#417FAE', meaning: '执行业务动作' },
  { key: 'rule', label: '规则', color: '#7DAACB', meaning: '条件判断与约束' },
  { key: 'script', label: '脚本', color: '#AD5727', meaning: '代码实现' },
  { key: 'metric', label: '指标', color: '#E1A057', meaning: '数值度量' },
  { key: 'testcase', label: '测试用例', color: '#aebfd0', meaning: '预期与验证' },
  { key: 'data-mock', label: '测试数据', color: '#63bfc7', meaning: '模拟输入与数据样本', auxiliary: true },
  { key: 'diagrams', label: '流程图', color: '#9cb2e6', meaning: '业务流程与关系图示', auxiliary: true },
  { key: 'diagrams-summary', label: '流程摘要', color: '#a2a9c7', meaning: '流程说明与要点', auxiliary: true },
  { key: 'excel', label: '数据表', color: '#8cc49a', meaning: '结构化行列数据', auxiliary: true },
]);

const KIND_ALIASES = {
  objects: 'object', entity: 'object', entities: 'object', concept: 'object', concepts: 'object',
  class: 'object', classes: 'object', 'object-type': 'object', object_type: 'object',
  operations: 'operation', logic: 'operation', action: 'operation', actions: 'operation',
  rules: 'rule', scripts: 'script', metrics: 'metric',
  test: 'testcase', tests: 'testcase', testcases: 'testcase', 'test-case': 'testcase', test_case: 'testcase',
  data: 'data-mock', data_mock: 'data-mock', mock: 'data-mock',
  diagram: 'diagrams', 'diagram-summary': 'diagrams-summary', diagrams_summary: 'diagrams-summary',
  xls: 'excel', xlsx: 'excel', spreadsheet: 'excel', spreadsheets: 'excel',
};
const VALID_KINDS = new Set(ICON_TYPES.map((entry) => entry.key));
const disposedInstanceMaterials = new WeakSet();

export function resolveIconKind(kind, fallback = 'object') {
  const normalized = String(kind || '').trim().toLowerCase();
  const resolved = KIND_ALIASES[normalized] || normalized;
  return VALID_KINDS.has(resolved) ? resolved : fallback;
}

function disposeInstanceMaterial(material) {
  if (material && !disposedInstanceMaterials.has(material)) {
    disposedInstanceMaterials.add(material);
    material.dispose();
  }
}

/** Disposes only the instance-owned materials; pooled geometry remains usable by other nodes. */
export function disposeIconInstance(instance) {
  instance?.traverse((part) => {
    const materials = Array.isArray(part.material) ? part.material : [part.material];
    materials.filter((item) => item?.userData.semanticIconOwned).forEach(disposeInstanceMaterial);
  });
}

// Bake original part transforms, then combine only parts with the same material role.
// Silhouettes and dark/light details stay unchanged while each type uses just a few draw calls.
function mergeTemplateParts(template, kind, cache) {
  const batches = new Map();
  template.updateMatrixWorld(true);
  template.traverse((part) => {
    if (!part.isMesh) { return; }
    if (!batches.has(part.material)) { batches.set(part.material, []); }
    const copy = part.geometry.index ? part.geometry.toNonIndexed() : part.geometry.clone();
    copy.applyMatrix4(part.matrixWorld);
    copy.clearGroups();
    batches.get(part.material).push(copy);
  });
  const mergedTemplate = new THREE.Group();
  for (const [surface, buffers] of batches) {
    const merged = mergeGeometries(buffers, false);
    buffers.forEach((buffer) => buffer.dispose());
    if (!merged) { throw new Error('Semantic icon geometry attributes do not match'); }
    cache.set(`merged:${kind}:${surface.uuid}`, merged);
    const part = new THREE.Mesh(merged, surface);
    part.userData.iconKind = kind;
    mergedTemplate.add(part);
  }
  mergedTemplate.name = `icon-${kind}`;
  mergedTemplate.userData.iconKind = kind;
  return mergedTemplate;
}

export function createSemanticIconLibrary(mode = 'light') {
  let theme = resolveGraphTheme(mode);
  const specs = new Map(ICON_TYPES.map((entry) => [entry.key, entry]));
  const geometryCache = new Map();
  const materialCache = new Map();
  const templateCache = new Map();

  function geometry(key, factory) {
    if (!geometryCache.has(key)) geometryCache.set(key, factory());
    return geometryCache.get(key);
  }

  function applyMaterialTheme(surface) {
    const { semanticIconKind: kind, semanticIconRole: role } = surface.userData;
    const colors = semanticIconRoleColors(kind, theme.mode, specs.get(kind).color);
    const color = new THREE.Color(colors[role] || colors.body);
    surface.color.copy(color);
    surface.emissive.copy(color);
    Object.assign(surface, theme.material);
    if (kind === 'object') {
      // A satin sphere keeps a visible highlight without flattening its shaded side.
      Object.assign(surface, { roughness: 0.34, metalness: 0.015, clearcoat: 0.22, clearcoatRoughness: 0.34 });
    }
    if (role === 'dark') {
      surface.roughness = 0.55; surface.metalness = 0; surface.clearcoat = 0;
    }
    surface.userData.semanticIconBase = {
      color: color.toArray(), emissive: color.toArray(),
      emissiveIntensity: theme.material.emissiveIntensity, opacity: 1,
      emphasis: theme.material.emphasis,
    };
  }

  function material(kind, role = 'body') {
    const key = `${kind}:${role}`;
    if (!materialCache.has(key)) {
      const surface = new THREE.MeshPhysicalMaterial();
      surface.userData.semanticIconKind = kind;
      surface.userData.semanticIconRole = role;
      applyMaterialTheme(surface);
      materialCache.set(key, surface);
    }
    return materialCache.get(key);
  }

  function mesh(group, shape, surface, x = 0, y = 0, z = 0) {
    const part = new THREE.Mesh(shape, surface);
    part.position.set(x, y, z);
    group.add(part);
    return part;
  }

  function polygon(points) {
    const shape = new THREE.Shape();
    shape.moveTo(...points[0]);
    for (const point of points.slice(1)) shape.lineTo(...point);
    shape.closePath();
    return shape;
  }

  function extruded(key, shape, depth = 0.32, bevel = 0.04) {
    return geometry(key, () => {
      const result = new THREE.ExtrudeGeometry(shape, {
        depth, bevelEnabled: bevel > 0, bevelThickness: bevel,
        bevelSize: bevel, bevelSegments: 3, steps: 1, curveSegments: 28,
      });
      result.translate(0, 0, -depth / 2);
      return result;
    });
  }

  function rounded(key, width, height, depth, radius = 0.05) {
    return geometry(key, () => new RoundedBoxGeometry(width, height, depth, 3, radius));
  }

  function sphere(radius) {
    return geometry(`sphere:${radius}`, () => new THREE.SphereGeometry(radius, 16, 12));
  }

  function disc(key, radius, depth, bevel = 0.025) {
    const shape = new THREE.Shape();
    shape.absarc(0, 0, radius, 0, Math.PI * 2, false);
    return extruded(key, shape, depth, bevel);
  }

  // Rounded stroke with distinct joints rather than angular, pixelated line textures.
  function stroke(group, key, points, radius, surface, z) {
    const vertices = points.map(([x, y]) => new THREE.Vector3(x, y, z));
    vertices.forEach((point) => {
      mesh(group, sphere(radius), surface, point.x, point.y, point.z);
    });
    for (let index = 1; index < vertices.length; index += 1) {
      const start = vertices[index - 1];
      const end = vertices[index];
      const delta = end.clone().sub(start);
      const length = delta.length();
      const rod = mesh(group,
        geometry(`${key}:${index}`, () => new THREE.CylinderGeometry(radius, radius, length, 12)),
        surface,
      );
      rod.position.copy(start).add(end).multiplyScalar(0.5);
      rod.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), delta.normalize());
    }
  }

  function makeEntityObject() {
    const group = new THREE.Group();
    const body = material('object');
    // A filled entity with three attached properties: no cube, person or document.
    for (const [index, [x, y]] of [[-0.73, 0.40], [0.73, 0.40], [0, -0.73]].entries()) {
      const end = new THREE.Vector3(x, y, 0);
      const start = end.clone().multiplyScalar(0.46);
      const direction = end.clone().sub(start);
      const rod = mesh(group,
        geometry(`object:entity:connector:${index}`, () => new THREE.CylinderGeometry(0.065, 0.065, direction.length(), 12)),
        body,
      );
      rod.position.copy(start).add(end).multiplyScalar(0.5);
      rod.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
      mesh(group, sphere(0.145), material('object', 'light'), x, y, 0.02);
    }
    mesh(group,
      // 24x16 matches the icon's screen size; finer segments are invisible below deep zoom.
      geometry('object:entity:center', () => new THREE.SphereGeometry(0.5, 24, 16)),
      body, 0, 0.04, 0,
    );
    return group;
  }

  function makeOperation() {
    const group = new THREE.Group();
    const points = [];
    for (let tooth = 0; tooth < 6; tooth += 1) {
      for (const [offset, radius] of [[-0.5, 0.65], [-0.34, 0.65], [-0.24, 0.87], [0.24, 0.87], [0.34, 0.65]]) {
        const angle = ((tooth + offset) / 6) * Math.PI * 2 + Math.PI / 2;
        points.push([Math.cos(angle) * radius, Math.sin(angle) * radius]);
      }
    }
    mesh(group, extruded('operation:gear', polygon(points), 0.36, 0.045), material('operation'));
    const arrow = polygon([
      [-0.45, -0.12], [0.06, -0.12], [0.06, -0.32], [0.46, 0],
      [0.06, 0.32], [0.06, 0.12], [-0.45, 0.12],
    ]);
    mesh(group, extruded('operation:arrow', arrow, 0.025, 0.018), material('operation', 'dark'), 0, 0, 0.238);
    return group;
  }

  function makeRule() {
    const group = new THREE.Group();
    const surface = material('rule');
    // A decision node and two outcomes, without a checkmark that could imply tests.
    for (const side of [-1, 1]) {
      const curve = new THREE.CatmullRomCurve3([
        new THREE.Vector3(side * 0.1, -0.22, 0),
        new THREE.Vector3(side * 0.2, -0.43, 0),
        new THREE.Vector3(side * 0.5, -0.46, 0),
        new THREE.Vector3(side * 0.62, -0.59, 0),
        new THREE.Vector3(side * 0.62, -0.77, 0),
      ], false, 'centripetal');
      mesh(group,
        geometry(`rule:branch:${side}`, () => new THREE.TubeGeometry(curve, 20, 0.065, 10, false)),
        surface,
      );
      mesh(group, disc('rule:outcome', 0.155, 0.22, 0.027), material('rule', 'light'), side * 0.62, -0.79, 0.025);
    }
    mesh(group, extruded('rule:diamond', polygon([[0, 0.93], [0.64, 0.30], [0, -0.33], [-0.64, 0.30]]), 0.34, 0.06), surface);
    return group;
  }

  function makeScript() {
    const group = new THREE.Group();
    const outline = polygon([[-0.65, -0.77], [0.65, -0.77], [0.65, 0.41], [0.28, 0.80], [-0.65, 0.80]]);
    mesh(group, extruded('script:page', outline, 0.27, 0.065), material('script'));
    const fold = polygon([[0.28, 0.77], [0.28, 0.42], [0.61, 0.42]]);
    mesh(group, extruded('script:fold', fold, 0.045, 0.028), material('script', 'light'), 0, 0, 0.223);
    const ink = material('script', 'dark');
    stroke(group, 'script:left-code', [[-0.27, 0.18], [-0.47, -0.04], [-0.27, -0.26]], 0.048, ink, 0.224);
    stroke(group, 'script:slash', [[-0.10, -0.32], [0.09, 0.28]], 0.049, ink, 0.224);
    stroke(group, 'script:right-code', [[0.27, 0.18], [0.47, -0.04], [0.27, -0.26]], 0.048, ink, 0.224);
    return group;
  }

  function makeMetric() {
    const group = new THREE.Group();
    const centerY = -0.45;
    const face = new THREE.Shape();
    face.moveTo(0.88, centerY);
    face.absarc(0, centerY, 0.88, 0, Math.PI, false);
    face.lineTo(0.88, centerY);
    face.closePath();
    mesh(group, extruded('metric:face', face, 0.27, 0.035), material('metric', 'inset'));
    const rim = new THREE.Shape();
    rim.moveTo(0.88, centerY);
    rim.absarc(0, centerY, 0.88, 0, Math.PI, false);
    rim.lineTo(-0.69, centerY);
    rim.absarc(0, centerY, 0.69, Math.PI, 0, true);
    rim.lineTo(0.88, centerY);
    rim.closePath();
    mesh(group, extruded('metric:rim', rim, 0.115, 0.04), material('metric'), 0, 0, 0.17);
    for (let index = 0; index < 3; index += 1) {
      const angle = Math.PI * [0.18, 0.5, 0.82][index];
      stroke(group, `metric:tick:${index}`, [0.53, 0.64].map((radius) => [Math.cos(angle) * radius, centerY + Math.sin(angle) * radius]), 0.036, material('metric', 'light'), 0.194);
    }
    const needleShape = polygon([[-0.075, -0.08], [-0.057, 0.16], [-0.022, 0.65], [0.018, 0.70], [0.05, 0.16], [0.075, -0.08]]);
    const needle = mesh(group, extruded('metric:needle', needleShape, 0.045, 0.015), material('metric', 'light'), 0, centerY, 0.246);
    needle.rotation.z = -Math.PI / 4;
    mesh(group, disc('metric:hub', 0.135, 0.105, 0.025), material('metric', 'light'), 0, centerY, 0.286);
    // Bake offset into children, so all exported groups retain a clean origin.
    group.children.forEach((part) => { part.position.y += 0.11; });
    return group;
  }

  function makeTestcase() {
    const group = new THREE.Group();
    const body = material('testcase');
    const dark = material('testcase', 'dark');
    const white = material('testcase', 'white');
    mesh(group, rounded('testcase:board', 1.37, 1.57, 0.30, 0.12), body, 0, -0.08, 0);
    mesh(group, rounded('testcase:paper', 1.17, 1.37, 0.06, 0.075), material('testcase', 'paper'), 0, -0.08, 0.184);
    mesh(group, rounded('testcase:clip', 0.72, 0.28, 0.16, 0.06), body, 0, 0.65, 0.24);
    mesh(group, disc('testcase:clip-head', 0.18, 0.14, 0.027), body, 0, 0.79, 0.24);
    mesh(group, disc('testcase:clip-hole', 0.057, 0.018, 0.006), dark, 0, 0.80, 0.337);
    mesh(group, rounded('testcase:checkbox', 0.27, 0.27, 0.04, 0.035), dark, -0.30, 0.20, 0.238);
    stroke(group, 'testcase:check', [[-0.375, 0.20], [-0.316, 0.139], [-0.217, 0.264]], 0.027, white, 0.278);
    mesh(group, rounded('testcase:checkbox', 0.27, 0.27, 0.04, 0.035), dark, -0.30, -0.28, 0.238);
    mesh(group, rounded('testcase:empty', 0.17, 0.17, 0.018, 0.015), material('testcase', 'paper'), -0.30, -0.28, 0.267);
    for (const y of [0.20, -0.28]) {
      mesh(group, rounded('testcase:line', 0.43, 0.065, 0.035, 0.025), dark, 0.20, y, 0.246);
    }
    return group;
  }

  function makeDataMock() {
    const group = new THREE.Group();
    const body = material('data-mock');
    const top = material('data-mock', 'light');
    // Three distinct rounded database cylinders; the gaps read even at node size.
    for (const y of [-0.46, 0, 0.46]) {
      const tier = mesh(group, disc('data-mock:tier', 0.65, 0.36, 0.035), body, 0, y, 0);
      tier.rotation.x = Math.PI / 2;
      tier.scale.set(1, 0.64, 1);
      const cap = mesh(group, disc('data-mock:cap', 0.615, 0.018, 0.012), top, 0, y + 0.208, 0);
      cap.rotation.x = Math.PI / 2;
      cap.scale.set(1, 0.64, 1);
    }
    return group;
  }

  function makeDiagrams() {
    const group = new THREE.Group();
    const body = material('diagrams');
    const connector = material('diagrams', 'inset');
    stroke(group, 'diagrams:trunk', [[0, 0.42], [0, 0.02]], 0.046, connector, 0);
    stroke(group, 'diagrams:branch', [[-0.49, -0.37], [-0.49, 0.02], [0.49, 0.02], [0.49, -0.37]], 0.046, connector, 0);
    mesh(group, rounded('diagrams:root', 0.68, 0.43, 0.29, 0.065), material('diagrams', 'light'), 0, 0.58, 0.02);
    for (const x of [-0.49, 0.49]) {
      mesh(group, rounded('diagrams:step', 0.64, 0.44, 0.29, 0.065), body, x, -0.49, 0.02);
      mesh(group, rounded('diagrams:step-mark', 0.29, 0.047, 0.027, 0.018), material('diagrams', 'paper'), x, -0.49, 0.183);
    }
    return group;
  }

  function makeDiagramsSummary() {
    const group = new THREE.Group();
    // A plain summary document has neither a code glyph nor a test checkbox.
    mesh(group, rounded('diagrams-summary:page', 1.26, 1.58, 0.29, 0.09), material('diagrams-summary'));
    mesh(group, rounded('diagrams-summary:heading', 0.40, 0.095, 0.03, 0.035), material('diagrams-summary', 'light'), -0.20, 0.49, 0.174);
    const widths = [0.79, 0.79, 0.53];
    for (let index = 0; index < widths.length; index += 1) {
      mesh(group,
        rounded(`diagrams-summary:line:${index}`, widths[index], 0.075, 0.032, 0.025),
        material('diagrams-summary', 'dark'),
        -0.40 + widths[index] / 2, 0.20 - index * 0.27, 0.174,
      );
    }
    return group;
  }

  function makeExcel() {
    const group = new THREE.Group();
    const body = material('excel');
    const ink = material('excel', 'inset');
    mesh(group, rounded('excel:frame', 1.57, 1.36, 0.30, 0.085), body);
    mesh(group, rounded('excel:sheet', 1.35, 1.14, 0.045, 0.035), material('excel', 'paper'), 0, 0, 0.174);
    mesh(group, rounded('excel:header', 1.34, 0.26, 0.025, 0.025), body, 0, 0.432, 0.214);
    // Three columns and four rows form a table silhouette without an Excel logo.
    for (const x of [-0.225, 0.225]) {
      mesh(group, rounded('excel:column', 0.035, 1.12, 0.025, 0.012), ink, x, 0, 0.231);
    }
    for (const y of [0.296, 0.007, -0.282]) {
      mesh(group, rounded('excel:row', 1.34, 0.035, 0.025, 0.012), ink, 0, y, 0.231);
    }
    return group;
  }

  const builders = {
    object: makeEntityObject, operation: makeOperation, rule: makeRule,
    script: makeScript, metric: makeMetric, testcase: makeTestcase,
    'data-mock': makeDataMock, diagrams: makeDiagrams,
    'diagrams-summary': makeDiagramsSummary, excel: makeExcel,
  };

  let disposed = false;
  const instanceMaterials = new Set();
  function createIcon(kind, { size = 1 } = {}) {
    if (disposed) { throw new Error('Semantic icon library has been disposed'); }
    const resolvedKind = resolveIconKind(kind);
    if (!templateCache.has(resolvedKind)) {
      templateCache.set(resolvedKind, mergeTemplateParts(builders[resolvedKind](), resolvedKind, geometryCache));
    }
    const instance = templateCache.get(resolvedKind).clone(true);
    const ownMaterials = new Map();
    instance.traverse((part) => {
      if (!part.isMesh) { return; }
      const original = part.material;
      if (!ownMaterials.has(original)) {
        const owned = original.clone();
        owned.transparent = false;
        owned.userData.semanticIconOwned = true;
        owned.userData.semanticIconBase = {
          color: owned.color.toArray(), emissive: owned.emissive.toArray(),
          emissiveIntensity: owned.emissiveIntensity, opacity: owned.opacity,
          emphasis: theme.material.emphasis,
        };
        ownMaterials.set(original, owned);
        instanceMaterials.add(owned);
        owned.addEventListener('dispose', () => instanceMaterials.delete(owned));
      }
      part.material = ownMaterials.get(original);
      part.castShadow = false;
      part.receiveShadow = false;
      part.userData.iconKind = resolvedKind;
    });
    instance.scale.setScalar(Number.isFinite(size) && size > 0 ? size : 1);
    return instance;
  }

  function setTheme(nextMode) {
    if (disposed) { return; }
    const next = resolveGraphTheme(nextMode);
    if (next === theme) { return; }
    theme = next;
    materialCache.forEach(applyMaterialTheme);
    instanceMaterials.forEach(applyMaterialTheme);
  }

  function dispose() {
    if (disposed) { return; }
    disposed = true;
    instanceMaterials.forEach(disposeInstanceMaterial);
    new Set(geometryCache.values()).forEach((value) => value.dispose());
    new Set(materialCache.values()).forEach((value) => value.dispose());
    instanceMaterials.clear();
    geometryCache.clear();
    materialCache.clear();
    templateCache.clear();
  }
  return { createIcon, setTheme, dispose };
}
