import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { FXAAShader } from 'three/addons/shaders/FXAAShader.js';
import { Line2 } from 'three/addons/lines/Line2.js';
import { LineGeometry } from 'three/addons/lines/LineGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { KIND_META } from '../studio-model.js';
import { studioParallelEdgeOffsets, studioSelfLoopIndexes } from '../studio-graph-geometry.js';
import { graphCameraDistance, graphDepthDistribution, graphDepthSpan, graphNodeRadius, graphWorldPosition, validCameraSnapshot } from './studio-three-geometry.js';
import { studioCategoryPositions, studioLabelNodeIds, studioNodeName, STUDIO_3D_PARTICLE_LIMIT } from './studio-three-presentation.js';
import { createSemanticIconLibrary, disposeIconInstance, ICON_TYPES, resolveIconKind } from './semantic-icons.js';
import { graphThemeMode, resolveGraphTheme } from './graph-theme.js';
import { createGraphLighting } from './graph-lighting.js';

const DEFAULT_STATE = { selectedId: '', activeNodeIds: new Set(), activeEdgeIds: new Set(), hasHighlight: false, previewKind: '', motionEnabled: true, reducedMotion: false };
const CLUSTER_META = { label: '关系簇', color: '#176b87', soft: '#b9e3ed' };
const ICON_META = new Map(ICON_TYPES.map((entry) => [entry.key, entry]));
const metaFor = (node, mode = 'light') => {
  const colors = resolveGraphTheme(mode).colors;
  if (node?.kind === 'cluster') return { ...CLUSTER_META, color: colors.cluster };
  const kind = resolveIconKind(node?.kind);
  const icon = ICON_META.get(kind) || ICON_META.get('object');
  return { ...KIND_META[kind], ...icon, color: colors[kind] || icon.color, soft: KIND_META[kind]?.soft || icon.color };
};
const vector = (value) => new THREE.Vector3(value.x, value.y, value.z);
const UP = new THREE.Vector3(0, 1, 0);
const nodeVisualRadius = (node) => node.kind === 'cluster' ? graphNodeRadius(node) : Math.max(24, graphNodeRadius(node) * 1.1);

export function studioNodeIdFromHit(object) {
  for (let current = object; current; current = current.parent) {
    if (current.userData?.nodeId) return current.userData.nodeId;
  }
  return undefined;
}

export function studioNodeMaterialStates(object) {
  const materials = new Set();
  object.traverse((part) => {
    if (part.material) (Array.isArray(part.material) ? part.material : [part.material]).forEach((material) => materials.add(material));
  });
  return [...materials].map((material) => {
    // Independent instance materials keep highlights local to one knowledge node.
    return { material, opacity: material.opacity, emissiveIntensity: material.emissiveIntensity || 0 };
  });
}

export function setStudioNodeMaterialEmphasis(states, { muted = false, selected = false, active = false } = {}) {
  states.forEach(({ material, opacity, emissiveIntensity }) => {
    const base = material.userData.semanticIconBase;
    material.opacity = (base?.opacity ?? opacity) * (muted ? 0.2 : 1);
    if (material.transparent !== muted) { material.transparent = muted; material.needsUpdate = true; }
    material.depthWrite = !muted;
    if (material.emissive) material.emissiveIntensity = (base?.emissiveIntensity ?? emissiveIntensity)
      + (selected ? base?.emphasis ?? 0.055 : active ? (base?.emphasis ?? 0.055) * 0.4 : 0);
  });
}

// Keeps the Studio editing/selection API and its force worker; only presentation is 3D.
export class ThreeGraphRuntime {
  constructor(callbacks = {}) {
    this.callbacks = callbacks;
    this.scene = { nodes: [], edges: [] };
    this.positions = new Map();
    this.basePositions = new Map();
    this.depthFactors = new Map();
    this.depthOverrides = new Map();
    this.depthSpan = 360;
    this.nodeViews = new Map();
    this.graphTheme = resolveGraphTheme();
    this.iconLibrary = createSemanticIconLibrary();
    this.iconTilt = new THREE.Quaternion().setFromEuler(new THREE.Euler(-0.1, 0.14, 0));
    this.edgeViews = new Map();
    this.parallelEdgeOffsets = new Map();
    this.selfLoopIndexes = new Map();
    this.nodeById = new Map();
    this.forceTargets = new Map();
    this.pinnedNodeIds = new Set();
    this.interactionState = { ...DEFAULT_STATE };
    this.layoutMode = 'layered';
    this.forceGeneration = 0;
    this.forceStatus = 'idle';
    this.attributeViews = [];
    this.hoveredEdgeId = '';
    this.hoveredNodeId = '';
    this.lastClick = { id: '', time: 0 };
    this.autoRotate = false;
    this.presentation = { bloom: true, labels: false, particles: false };
    this.dirty = true;
    this.visible = true;
    this.destroyed = false;
    this.lastTime = 0;
    this.lastMinimapTime = 0;
    this.lastMinimapDrawTime = -Infinity;
    this.lastLabelTime = 0;
    this.lastHoverTime = 0;
    this.controlsActive = false;
    this.layoutTween = null;
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.projected = new THREE.Vector3();
    this.listeners = [];
    this.tick = this.tick.bind(this);
  }

  async mount(host, minimap) {
    this.host = host;
    this.host.dataset.iconStyle = 'semantic';
    this.host.dataset.objectIcon = 'entity';
    this.minimap = minimap;
    this.world = new THREE.Scene();
    // Distance fog is a depth cue only: its color always matches the app surface and
    // its density is recomputed from the fitted camera distance in fitView().
    this.world.fog = new THREE.FogExp2(0xffffff, 0);
    this.camera = new THREE.PerspectiveCamera(42, 1, 1, 100000);
    this.camera.position.set(0, 220, 1250);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.NoToneMapping;
    this.renderer.toneMappingExposure = 1;
    this.renderer.domElement.className = 'studio-three-canvas';
    this.renderer.domElement.dataset.iconStyle = 'semantic';
    this.renderer.domElement.dataset.objectIcon = 'entity';
    this.renderer.domElement.tabIndex = 0;
    this.renderer.domElement.setAttribute('aria-label', '三维知识图谱。拖动空白旋转，右键拖动平移，滚轮缩放，拖动节点调整位置，双击节点编辑。');
    host.appendChild(this.renderer.domElement);
    this.labelLayer = document.createElement('div');
    this.labelLayer.className = 'studio-three-label-layer';
    host.appendChild(this.labelLayer);
    this.nodeTooltip = document.createElement('div');
    this.nodeTooltip.className = 'studio-webgl-node-tooltip';
    this.nodeTooltip.hidden = true;
    host.appendChild(this.nodeTooltip);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.075;
    this.controls.rotateSpeed = 0.65;
    this.controls.minDistance = 110;
    this.controls.maxDistance = 40000;
    this.controls.autoRotateSpeed = 0.45;
    this.controls.addEventListener('start', () => {
      this.cancelPendingFocus();
      this.cameraTween = null;
      this.forceFitPending = false;
      this.controlsActive = true;
      this.requestRender();
    });
    this.controls.addEventListener('end', () => {
      this.controlsActive = false;
      this.requestRender();
      this.reportViewportChange();
    });
    this.controls.addEventListener('change', () => this.requestRender());
    this.lighting = createGraphLighting(this.graphTheme.mode);
    this.world.add(this.lighting.rig);
    this.geometries = {
      cluster: new THREE.IcosahedronGeometry(1.15, 1),
    };
    this.arrowGeometry = new THREE.ConeGeometry(3.6, 11, 7);
    this.particleGeometry = new THREE.SphereGeometry(2.1, 8, 6);
    this.grid = new THREE.GridHelper(3000, 32);
    this.grid.material.vertexColors = false;
    this.grid.rotation.x = Math.PI / 2;
    this.grid.position.z = -280;
    this.grid.material.transparent = true;
    this.grid.material.opacity = 0.18;
    this.world.add(this.grid);
    this.selectionRing = new THREE.Mesh(new THREE.RingGeometry(0.94, 1.02, 64), new THREE.MeshBasicMaterial({
      color: 0x3979aa, transparent: true, opacity: 0.86, depthWrite: false,
      depthTest: false, side: THREE.DoubleSide, toneMapped: false,
    }));
    this.selectionRing.renderOrder = 3;
    this.selectionRing.visible = false;
    this.world.add(this.selectionRing);
    this.syncTheme(false);
    this.bindEvents();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.resize();
    this.setPresentation(this.presentation);
    if (typeof IntersectionObserver !== 'undefined') {
      this.intersectionObserver = new IntersectionObserver(([entry]) => {
        this.visible = entry.isIntersecting;
        if (this.visible) this.requestRender();
      });
      this.intersectionObserver.observe(host);
    }
    this.requestRender();
    this.callbacks.onReady?.('3D · WebGL 2');
  }

  listen(target, type, callback, options) {
    target.addEventListener(type, callback, options);
    this.listeners.push(() => target.removeEventListener(type, callback, options));
  }

  requestRender() {
    this.dirty = true;
    if (!this.destroyed && this.renderer && !this.frame && typeof requestAnimationFrame === 'function') {
      this.frame = requestAnimationFrame(this.tick);
    }
  }

  bindEvents() {
    this.listen(this.host, 'pointerdown', (event) => this.pointerDown(event), true);
    this.listen(window, 'pointermove', (event) => this.pointerMove(event));
    this.listen(window, 'pointerup', (event) => this.pointerUp(event));
    this.listen(window, 'pointercancel', () => this.cancelPointer());
    this.listen(window, 'onto-theme-changed', () => this.syncTheme());
    this.listen(this.host, 'pointerleave', () => {
      if (!this.pointerAction) this.setHover(null, null);
    });
    this.listen(this.renderer.domElement, 'webglcontextlost', (event) => {
      event.preventDefault();
      if (!this.destroyed) this.callbacks.onContextLost?.();
    });
    this.listen(this.renderer.domElement, 'keydown', (event) => {
      if (event.key === 'Home') { event.preventDefault(); this.fitView(true); }
      if (event.key === 'Escape') this.callbacks.onBackgroundClick?.();
      if (event.key === '+' || event.key === '=') this.zoomBy(1.2);
      if (event.key === '-') this.zoomBy(1 / 1.2);
    });
    if (this.minimap) {
      this.listen(this.minimap, 'pointerdown', (event) => this.handleMinimapPointer(event));
      this.listen(this.minimap, 'pointermove', (event) => this.handleMinimapHover(event));
      this.listen(this.minimap, 'pointerleave', () => this.callbacks.onMinimapHover?.(''));
      this.minimapResizeObserver = new ResizeObserver(() => {
        this.lastMinimapDrawTime = -Infinity;
        this.drawMinimap();
      });
      this.minimapResizeObserver.observe(this.minimap);
    }
    this.listen(document, 'visibilitychange', () => {
      if (document.hidden) {
        this.forceWasRunning = this.forceStatus === 'running' || this.forceStatus === 'initializing';
        if (this.forceWasRunning) this.pauseForce();
      } else {
        this.lastTime = 0;
        this.requestRender();
        if (this.forceWasRunning) this.resumeForce();
        this.forceWasRunning = false;
      }
    });
  }

  setScene(scene) {
    const focusId = this.focusTrackingId;
    const nextSignature = scene.nodes.map((node) => `${node.id}:${node.kind}`).sort().join('|')
      + ':' + scene.edges.map((edge) => `${edge.id}:${edge.source}:${edge.target}:${edge.kind}`).sort().join('|');
    const sameStructure = nextSignature === this.sceneSignature;
    this.sceneSignature = nextSignature;
    this.nodeById = new Map(scene.nodes.map((node) => [node.id, node]));
    this.scene = { ...scene, edges: scene.edges.filter((edge) => this.nodeById.has(edge.source) && this.nodeById.has(edge.target)) };
    this.parallelEdgeOffsets = studioParallelEdgeOffsets(this.scene.edges);
    this.selfLoopIndexes = studioSelfLoopIndexes(this.scene.edges);
    if (sameStructure && this.nodeViews.size) {
      let moved = false;
      this.nodeViews.forEach((view, id) => {
        const node = this.nodeById.get(id);
        if (node.position.x !== view.node.position.x || node.position.y !== view.node.position.y
          || node.width !== view.node.width || node.height !== view.node.height) {
          const position = { x: node.position.x + node.width / 2, y: node.position.y + node.height / 2 };
          this.basePositions.set(id, { ...position });
          // Async planar layout results are the seed for force mode. They must
          // never overwrite the displayed category layout or its transition.
          if (this.layoutMode === 'force' && !this.layoutTween) {
            this.positions.set(id, { ...this.positions.get(id), ...position });
            this.forceTargets.delete(id);
            this.forceWorker?.postMessage({ type: 'pin', nodeId: id, ...position });
            moved = true;
          }
        }
        view.node = node;
        view.radius = nodeVisualRadius(node);
        view.title.textContent = node.label;
        view.label.setAttribute('aria-label', `${metaFor(node, this.graphTheme.mode).label}：${node.label}`);
      });
      this.scene.edges.forEach((edge) => {
        const view = this.edgeViews.get(edge.id);
        if (view) view.edge = edge;
      });
      this.updateAttributes();
      this.applyPositions();
      this.updateEmphasis();
      if (moved && focusId) this.focusNode(focusId);
      return;
    }
    this.stopForce(false);
    this.clearViews();
    this.layoutTween = null;
    this.positions = new Map(scene.nodes.map((node) => [node.id, { x: node.position.x + node.width / 2, y: node.position.y + node.height / 2 }]));
    this.basePositions = new Map([...this.positions].map(([id, position]) => [id, { ...position }]));
    if (this.layoutMode === 'layered') this.positions = studioCategoryPositions(scene.nodes);
    this.depthFactors = graphDepthDistribution(scene.nodes);
    this.depthOverrides = new Map([...this.depthOverrides].filter(([id]) => this.nodeById.has(id)));
    this.depthSpan = graphDepthSpan(this.positions, this.layoutMode);
    this.host.dataset.depthSpan = this.depthSpan.toFixed(2);
    this.pinnedNodeIds = new Set([...this.pinnedNodeIds].filter((id) => this.nodeById.has(id)));
    this.scene.nodes.forEach((node) => this.createNode(node));
    this.reportDepthSpan();
    this.scene.edges.forEach((edge) => this.createEdge(edge));
    this.host.dataset.sceneNodeCount = String(scene.nodes.length);
    this.host.dataset.sceneEdgeCount = String(this.scene.edges.length);
    this.host.dataset.renderer = 'three';
    this.host.dataset.graphDimension = '3';
    this.selectionCameraSnapshot = null;
    this.updateAttributes();
    this.updateEdgePositions();
    this.updateEmphasis();
    this.fitView(false, undefined, { preserveFocus: true });
    // Present one complete static scene even in a background tab. Background
    // requestAnimationFrame callbacks may never run until the tab is activated.
    this.presentStaticFrame();
    this.callbacks.onScenePresented?.();
    if (this.layoutMode === 'force') this.startForce();
    if (focusId && this.nodeViews.has(focusId)) this.focusNode(focusId);
    else if (focusId) this.cancelPendingFocus();
  }

  createNode(node) {
    const meta = metaFor(node, this.graphTheme.mode);
    const radius = nodeVisualRadius(node);
    const semanticIcon = node.kind !== 'cluster';
    const mesh = semanticIcon
      ? this.iconLibrary.createIcon(resolveIconKind(node.kind))
      : new THREE.Mesh(this.geometries.cluster, new THREE.MeshStandardMaterial({
        color: meta.color, roughness: 0.28, metalness: 0.12,
        emissive: meta.color, emissiveIntensity: 0.025, transparent: true,
      }));
    if (semanticIcon) mesh.quaternion.copy(this.camera.quaternion).multiply(this.iconTilt);
    mesh.position.copy(vector(this.worldPositionFor(node)));
    mesh.scale.setScalar(radius);
    mesh.traverse((part) => { part.userData.nodeId = node.id; });
    const materials = studioNodeMaterialStates(mesh);
    this.world.add(mesh);
    const label = document.createElement('button');
    label.type = 'button';
    label.className = 'studio-three-node-label';
    label.hidden = true;
    label.dataset.nodeId = node.id;
    label.style.setProperty('--node-color', meta.color);
    // Accessible names keep the element kind so screen readers can tell same-named
    // object and operation nodes apart; the visible label stays name-only.
    label.setAttribute('aria-label', `${meta.label}：${node.label}`);
    const title = document.createElement('span');
    title.textContent = node.label;
    label.append(title);
    label.addEventListener('click', (event) => {
      if (event.detail === 0) this.callbacks.onNodeClick?.(node.id);
    });
    label.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && event.shiftKey) {
        event.preventDefault();
        this.callbacks.onNodeDoubleClick?.(node.id);
      }
    });
    this.labelLayer.appendChild(label);
    this.nodeViews.set(node.id, { node, mesh, label, title, radius, materials, semanticIcon });
  }

  createEdge(edge) {
    const color = this.graphTheme.edge;
    const line = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color, transparent: true, opacity: this.graphTheme.edgeOpacity, depthWrite: false }));
    line.userData.edgeId = edge.id;
    const arrow = new THREE.Mesh(this.arrowGeometry, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.5 }));
    const particle = this.edgeViews.size < STUDIO_3D_PARTICLE_LIMIT
      ? new THREE.Mesh(this.particleGeometry, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9 })) : null;
    if (particle) particle.visible = false;
    this.world.add(line, arrow);
    if (particle) this.world.add(particle);
    this.edgeViews.set(edge.id, { edge, line, arrow, particle, color });
  }

  updateEdgePositions() {
    this.edgeViews.forEach((view) => {
      const source = this.nodeViews.get(view.edge.source);
      const target = this.nodeViews.get(view.edge.target);
      if (view.edge.source === view.edge.target) {
        // Each node owns its loop indexes; both endpoints stay on its boundary.
        const reach = source.radius * 2 + (this.selfLoopIndexes.get(view.edge.id) || 0) * 30;
        const start = source.mesh.position.clone().add(new THREE.Vector3(-source.radius * 0.6, source.radius * 0.8, 0));
        const end = source.mesh.position.clone().add(new THREE.Vector3(source.radius * 0.6, source.radius * 0.8, 0));
        view.curve = new THREE.CubicBezierCurve3(start,
          start.clone().add(new THREE.Vector3(-reach * 0.6, reach, 0)),
          end.clone().add(new THREE.Vector3(reach * 0.6, reach, 0)), end);
      } else {
        const delta = target.mesh.position.clone().sub(source.mesh.position);
        const direction = delta.clone().normalize();
        const start = source.mesh.position.clone().addScaledVector(direction, source.radius + 2);
        const end = target.mesh.position.clone().addScaledVector(direction, -target.radius - 5);
        const middle = start.clone().lerp(end, 0.5);
        middle.z += Math.min(60, delta.length() * 0.12);
        const offset = this.parallelEdgeOffsets.get(view.edge.id) || 0;
        if (offset) {
          // Reversing an edge reverses this normal, placing reciprocal arcs on
          // opposite sides. Depth-aligned edges use a finite perpendicular too.
          const normal = new THREE.Vector3(-direction.y, direction.x, 0);
          if (normal.lengthSq() < 1e-8) normal.set(0, -direction.z, direction.y);
          middle.addScaledVector(normal.normalize(), offset);
        }
        view.curve = new THREE.QuadraticBezierCurve3(start, middle, end);
      }
      view.line.geometry.setFromPoints(view.curve.getPoints(20));
      if (view.emphasisLine) this.updateWideEdgeGeometry(view);
      view.arrow.position.copy(view.curve.getPoint(1));
      view.arrow.quaternion.setFromUnitVectors(UP, view.curve.getTangent(1).normalize());
    });
  }

  updateWideEdgeGeometry(view) {
    if (!view.curve) return;
    view.emphasisLine.geometry.setPositions(view.curve.getPoints(20).flatMap((point) => point.toArray()));
    view.emphasisLine.computeLineDistances();
  }

  setWideEdgeVisible(view, active) {
    if (active && !view.emphasisLine) {
      const material = new LineMaterial({
        color: this.graphTheme.selection, linewidth: 2.5, worldUnits: false,
        transparent: true, opacity: this.graphTheme.edgeActiveOpacity, depthWrite: false, fog: true,
      });
      material.resolution.set(this.host?.clientWidth || 1, this.host?.clientHeight || 1);
      view.emphasisLine = new Line2(new LineGeometry(), material);
      // Keep the width in CSS pixels with and without a reduced-resolution bloom
      // target; Line2's default hook otherwise uses that target's pixel viewport.
      view.emphasisLine.onBeforeRender = () => material.resolution.set(this.host.clientWidth, this.host.clientHeight);
      view.emphasisLine.renderOrder = 1;
      this.world.add(view.emphasisLine);
      this.updateWideEdgeGeometry(view);
    }
    if (view.emphasisLine) {
      view.emphasisLine.visible = active;
      view.emphasisLine.material.color.set(this.graphTheme.selection);
      view.emphasisLine.material.opacity = this.graphTheme.edgeActiveOpacity;
    }
    view.line.visible = !active;
  }

  setInteractionState(state) {
    const previous = this.interactionState.selectedId;
    const previouslyReducedMotion = this.interactionState.reducedMotion;
    this.interactionState = { ...DEFAULT_STATE, ...state };
    if (previous !== state.selectedId) {
      this.selectionPulseUntil = performance.now() + 1800;
      this.updateAttributes();
      this.syncFocusedObjectPin();
    }
    if (!this.interactionState.motionEnabled || (this.interactionState.reducedMotion && !previouslyReducedMotion)) {
      this.setAutoRotate(false);
    }
    if (this.interactionState.reducedMotion || !this.interactionState.motionEnabled) {
      if (this.controls) this.controls.enableDamping = false;
      if (this.cameraTween) this.moveCamera(this.cameraTween.to, this.cameraTween.targetTo, false);
      if (this.layoutTween) this.completeLayoutTween();
    } else if (this.controls) this.controls.enableDamping = true;
    this.updateEmphasis();
    this.requestRender();
    if (this.camera && this.labelLayer) this.drawLabels();
  }

  updateEmphasis() {
    const state = this.interactionState;
    const previewing = Boolean(state.previewKind);
    this.nodeViews.forEach((view, id) => {
      const selected = id === state.selectedId;
      const active = previewing ? view.node.kind === state.previewKind
        : selected || state.activeNodeIds.has(id) || id === this.hoveredNodeId;
      const muted = (state.hasHighlight || previewing) && !active && !selected;
      setStudioNodeMaterialEmphasis(view.materials, { muted, selected, active });
      view.mesh.scale.setScalar(view.radius * (selected ? 1.16 : 1));
      view.label.classList.toggle('is-selected', selected);
      view.label.classList.toggle('is-muted', muted);
      view.label.setAttribute('aria-pressed', String(selected));
    });
    const edgeIsActive = (view) => state.activeEdgeIds.has(view.edge.id) || view.edge.id === this.hoveredEdgeId;
    const particles = [];
    this.edgeViews.forEach((view) => {
      if (view.particle) { view.particle.visible = false; particles.push(view.particle); }
      view.particle = null;
    });
    // Reuse the bounded particle pool for the currently highlighted relationships.
    [...this.edgeViews.values()].sort((a, b) => Number(edgeIsActive(b)) - Number(edgeIsActive(a)))
      .slice(0, particles.length).forEach((view, index) => {
        view.particle = particles[index];
        view.particle.material.color.set(view.color);
      });
    let animated = 0;
    this.edgeViews.forEach((view, id) => {
      const active = state.activeEdgeIds.has(id) || id === this.hoveredEdgeId;
      const { edgeOpacity, edgeMutedOpacity, edgeActiveOpacity } = this.graphTheme;
      const color = active ? this.graphTheme.selection : this.graphTheme.edge;
      view.line.material.color.set(color);
      view.arrow.material.color.set(color);
      view.line.material.opacity = active ? edgeActiveOpacity : state.hasHighlight ? edgeMutedOpacity : edgeOpacity;
      view.arrow.material.opacity = state.hasHighlight && !active ? edgeMutedOpacity : active ? edgeActiveOpacity : 0.65;
      this.setWideEdgeVisible(view, active);
      view.animate = Boolean(view.particle) && (!state.hasHighlight || active) && animated++ < STUDIO_3D_PARTICLE_LIMIT;
    });
    this.updateSelectionRing(performance.now());
    this.requestRender();
  }

  updateAttributes() {
    this.attributeViews.forEach((view) => {
      this.world.remove(view.mesh, view.line);
      view.mesh.geometry.dispose(); view.mesh.material.dispose();
      view.line.geometry.dispose(); view.line.material.dispose();
      view.label.remove();
    });
    this.attributeViews = [];
    if (this.host) this.host.dataset.attributeOrbitNodeCount = '0';
    // Attributes remain available in the object summary/editor, not as extra graph nodes.
  }

  setLayoutMode(mode) {
    if (mode !== 'force' && mode !== 'layered') return;
    if (mode === this.layoutMode) return;
    // Capture displayed world coordinates before changing the depth policy. Force
    // positions have no z; interpolating that missing value produced NaN meshes.
    const from = new Map([...this.nodeViews].map(([id, view]) => [id, {
      x: view.mesh.position.x, y: -view.mesh.position.y, z: view.mesh.position.z,
    }]));
    this.layoutMode = mode;
    if (this.focusTrackingId) this.pendingFocusId = this.focusTrackingId;
    this.depthOverrides.clear();
    this.stopForce();
    this.layoutTween = null;
    const target = mode === 'layered' ? studioCategoryPositions(this.scene.nodes)
      : new Map([...this.basePositions].map(([id, position]) => [id, {
        ...position, z: (this.depthFactors.get(id) || 0) * graphDepthSpan(this.basePositions, 'force'),
      }]));
    this.positions = from.size ? from : new Map([...target].map(([id, position]) => [id, { ...position }]));
    this.startLayoutTween(target);
    // Fit the destination bounds while positions interpolate, not the old layout.
    const bounds = new THREE.Box3();
    target.forEach((position, id) => {
      const radius = (this.nodeViews.get(id)?.radius || 24) * 2;
      const point = new THREE.Vector3(position.x, -position.y, position.z || 0);
      bounds.expandByPoint(point.clone().addScalar(radius));
      bounds.expandByPoint(point.clone().addScalar(-radius));
    });
    this.fitView(!document.hidden, bounds.isEmpty() ? undefined : bounds, { preserveFocus: true });
    if (mode === 'force' && !this.layoutTween) this.startForce();
    this.presentStaticFrame();
    this.requestRender();
  }

  startLayoutTween(target) {
    const animate = !document.hidden && !this.interactionState.reducedMotion && this.interactionState.motionEnabled && [...this.positions].some(([id]) => {
      const next = target.get(id);
      const current = this.positions.get(id);
      return next && current && (next.x !== current.x || next.y !== current.y || next.z !== current.z);
    });
    if (!animate) {
      this.positions = new Map([...this.positions].map(([id, position]) => {
        const next = target.get(id);
        return [id, next ? { ...next } : { ...position }];
      }));
      this.applyPositions();
      return;
    }
    this.layoutTween = {
      from: new Map([...this.positions].map(([id, position]) => [id, { ...position, z: Number.isFinite(position.z) ? position.z : (this.depthFactors.get(id) || 0) * this.depthSpan }])),
      to: new Map([...target].map(([id, position]) => [id, { ...position, z: Number.isFinite(position.z) ? position.z : 0 }])),
      start: performance.now(),
      duration: 380,
    };
    this.requestRender();
  }

  completeLayoutTween() {
    if (!this.layoutTween) return;
    this.positions = new Map([...this.layoutTween.to].map(([id, position]) => [id, { ...position }]));
    this.layoutTween = null;
    this.applyPositions();
    if (this.restoredViewportPending) this.ensureViewportVisible();
    if (this.layoutMode === 'force') this.startForce();
    this.flushPendingFocus();
  }

  presentStaticFrame() {
    this.orientNodeIcons();
    this.updateSelectionRing(performance.now());
    if (this.presentation.bloom && this.composer) this.composer.render(0);
    else this.renderer.render(this.world, this.camera);
    this.drawLabels();
    this.lastMinimapDrawTime = -Infinity;
    this.drawMinimap();
  }

  orientNodeIcons() {
    if (!this.camera) return;
    this.lighting?.followCamera(this.camera);
    // Follow the existing camera while retaining a slight solid-geometry tilt.
    // This uses the renderer's existing capped loop, never a separate animation.
    this.nodeViews.forEach((view) => {
      if (view.semanticIcon) view.mesh.quaternion.copy(this.camera.quaternion).multiply(this.iconTilt);
    });
  }

  setPresentation(options = {}) {
    this.presentation = { ...this.presentation, ...options };
    if (this.presentation.bloom && !this.composer && this.renderer) {
      // Canvas antialiasing does not apply to postprocessing render targets.
      // Multisample the scene before bloom while retaining the pixel budget.
      const gl = this.renderer.getContext();
      const supportedSamples = gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA16F, gl.SAMPLES);
      const samples = Math.max(0, ...Array.from(supportedSamples || []).filter((value) => value > 1 && value <= 4));
      this.host.dataset.postprocessingAntialias = samples ? `msaa-${samples}` : 'fxaa';
      const renderTarget = new THREE.WebGLRenderTarget(1, 1, {
        type: THREE.HalfFloatType,
        samples,
      });
      this.composer = new EffectComposer(this.renderer, renderTarget);
      this.composer.setPixelRatio(1);
      const width = Math.max(1, this.host.clientWidth);
      const height = Math.max(1, this.host.clientHeight);
      this.renderPass = new RenderPass(this.world, this.camera);
      // Keep the shared light background below the bloom threshold.
      const { strength, radius, threshold } = this.graphTheme.bloom;
      this.bloomPass = new UnrealBloomPass(new THREE.Vector2(1, 1), strength, radius, threshold);
      this.outputPass = new OutputPass();
      this.composer.addPass(this.renderPass);
      this.composer.addPass(this.bloomPass);
      this.composer.addPass(this.outputPass);
      // Some GPUs cannot multisample HDR targets. Smooth their display-space
      // result with FXAA instead of requesting an unsupported sample count.
      this.antialiasPass = samples ? null : new ShaderPass(FXAAShader);
      if (this.antialiasPass) this.composer.addPass(this.antialiasPass);
      this.resizePostprocessing(width, height);
    }
    this.updateEmphasis();
    // Apply the switch without waiting for animation frames in a hidden tab.
    if (this.camera && this.labelLayer) this.drawLabels();
    if (this.host) {
      this.host.dataset.bloom = String(this.presentation.bloom);
      this.host.dataset.allLabels = String(this.presentation.labels);
      this.host.dataset.particles = String(this.presentation.particles);
    }
  }

  setMotionEnabled(value) { this.setInteractionState({ ...this.interactionState, motionEnabled: Boolean(value) }); }
  setAutoRotate(value) {
    if (this.destroyed) return false;
    // This setter represents an explicit user choice; reduced motion still
    // suppresses incidental effects and stops orbit when the preference changes.
    const rotating = Boolean(value) && this.interactionState.motionEnabled;
    if (rotating) this.cancelPendingFocus();
    const changed = rotating !== this.autoRotate;
    this.autoRotate = rotating;
    if (this.controls) this.controls.autoRotate = rotating && !this.pointerAction;
    if (changed && !rotating && this.camera && this.controls?.enableDamping) {
      // Clear the orbit's residual inertia through the public controls API,
      // retaining the exact view instead of applying the remaining rotation.
      const position = this.camera.position.clone();
      const target = this.controls.target.clone();
      this.controls.enableDamping = false;
      this.controls.update(0);
      this.camera.position.copy(position);
      this.controls.target.copy(target);
      this.controls.update(0);
      this.controls.enableDamping = true;
      this.controlsMoving = false;
    }
    if (changed) this.callbacks.onAutoRotateChange?.(rotating);
    this.requestRender();
    return this.autoRotate;
  }

  startForce() {
    if (!this.scene.nodes.length) return;
    this.stopForce(false);
    const generation = ++this.forceGeneration;
    this.forceFitPending = !this.interactionState.selectedId && !this.focusTrackingId;
    this.setForceStatus('initializing');
    this.forceWorker = new Worker(new URL('./studio-force-layout.worker.js', import.meta.url), { type: 'module' });
    this.forceWorker.onmessage = ({ data }) => {
      if (this.destroyed || data.generation !== this.forceGeneration || this.layoutMode !== 'force') return;
      if (data.type === 'status') {
        this.setForceStatus(data.status);
        if (data.status === 'settled' && this.forceFitPending) this.fitAfterForce = true;
        this.requestRender();
        return;
      }
      if (data.type !== 'positions' && data.type !== 'settled') return;
      data.nodeIds.forEach((id, index) => {
        if (this.pointerAction?.nodeId !== id && !this.pinnedNodeIds.has(id)) this.forceTargets.set(id, { x: data.positions[index * 2], y: data.positions[index * 2 + 1] });
      });
      if (data.type === 'settled') this.fitAfterForce = this.forceFitPending;
      this.requestRender();
    };
    this.forceWorker.onerror = () => { this.setForceStatus('error'); this.callbacks.onForceError?.(); };
    const seen = new Set();
    const links = this.scene.edges.filter((edge) => {
      if (edge.source === edge.target) return false;
      const key = [edge.source, edge.target].sort().join(':');
      if (seen.has(key)) return false;
      seen.add(key); return true;
    }).map(({ source, target, kind }) => ({ source, target, kind }));
    this.forceWorker.postMessage({ type: 'init', generation, seed: 'studio-three-force-v1', nodes: this.scene.nodes.map((node) => ({ id: node.id, ...this.positions.get(node.id), width: node.width, height: node.height })), links, viewport: { width: this.host.clientWidth, height: this.host.clientHeight } });
    this.pinnedNodeIds.forEach((nodeId) => this.forceWorker.postMessage({ type: 'pin', nodeId, ...this.positions.get(nodeId) }));
    this.syncFocusedObjectPin();
    if (document.hidden) { this.forceWasRunning = true; this.pauseForce(); }
  }

  stopForce(report = true) {
    this.forceGeneration += 1;
    this.forceWorker?.terminate();
    this.forceWorker = null;
    this.forceTargets.clear();
    this.fitAfterForce = false;
    if (report) this.setForceStatus('idle');
  }
  pauseForce() { this.forceWorker?.postMessage({ type: 'pause' }); }
  resumeForce() { this.forceWorker?.postMessage({ type: 'resume' }); }
  reheatForce() { this.forceWorker?.postMessage({ type: 'reheat', alpha: 0.76 }); }
  setForceStatus(status) {
    this.forceStatus = status;
    if ((status === 'running' || status === 'initializing') && this.focusTrackingId) this.pendingFocusId = this.focusTrackingId;
    if (this.host) this.host.dataset.forceStatus = status;
    this.callbacks.onForceStatusChange?.(status);
  }
  isPinned(id) { return this.pinnedNodeIds.has(id); }
  togglePin(nodeId) {
    if (!this.positions.has(nodeId)) return false;
    const next = !this.pinnedNodeIds.has(nodeId);
    if (next) this.pinnedNodeIds.add(nodeId); else this.pinnedNodeIds.delete(nodeId);
    this.forceWorker?.postMessage({ type: next ? 'pin' : 'unpin', nodeId, ...this.positions.get(nodeId) });
    this.callbacks.onPinChange?.(nodeId, next);
    return next;
  }
  syncFocusedObjectPin() {
    const selected = this.nodeById.get(this.interactionState.selectedId);
    if (this.focusPinnedNodeId && this.focusPinnedNodeId !== selected?.id && !this.pinnedNodeIds.has(this.focusPinnedNodeId)) this.forceWorker?.postMessage({ type: 'unpin', nodeId: this.focusPinnedNodeId });
    this.focusPinnedNodeId = selected?.kind === 'object' ? selected.id : '';
    if (this.focusPinnedNodeId) this.forceWorker?.postMessage({ type: 'pin', nodeId: selected.id, ...this.positions.get(selected.id) });
  }

  rayAt(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    return this.raycaster;
  }

  pointerDown(event) {
    if (event.button !== 0) return;
    this.cancelPendingFocus();
    const labelId = event.target.closest?.('[data-node-id]')?.dataset.nodeId;
    const hit = labelId || studioNodeIdFromHit(this.rayAt(event).intersectObjects([...this.nodeViews.values()].map((view) => view.mesh), true)[0]?.object);
    this.pointerAction = { nodeId: hit, x: event.clientX, y: event.clientY, pointerId: event.pointerId, moved: false };
    this.cameraTween = null;
    this.forceFitPending = false;
    if (!hit) return;
    event.stopPropagation();
    event.preventDefault();
    this.controls.enabled = false;
    this.host.setPointerCapture?.(event.pointerId);
    const position = this.nodeViews.get(hit).mesh.position.clone();
    this.pointerAction.origin = position;
    this.pointerAction.plane = new THREE.Plane().setFromNormalAndCoplanarPoint(this.camera.getWorldDirection(new THREE.Vector3()), position);
    this.pointerAction.start = this.rayAt(event).ray.intersectPlane(this.pointerAction.plane, new THREE.Vector3());
    this.forceTargets.delete(hit);
  }

  pointerMove(event) {
    const action = this.pointerAction;
    if (action) {
      if (event.pointerId !== action.pointerId) return;
      if (Math.hypot(event.clientX - action.x, event.clientY - action.y) > 6) action.moved = true;
      if (!action.nodeId || !action.moved || !action.start) return;
      const current = this.rayAt(event).ray.intersectPlane(action.plane, new THREE.Vector3());
      if (!current) return;
      const next = action.origin.clone().add(current.sub(action.start));
      const position = { x: next.x, y: -next.y };
      this.positions.set(action.nodeId, { ...this.positions.get(action.nodeId), ...position });
      // Camera-plane dragging may change z when the graph is viewed sideways.
      // Keep that component in this display only; the saved API remains x/y.
      this.depthOverrides.set(action.nodeId, next.z);
      this.forceTargets.delete(action.nodeId);
      this.forceWorker?.postMessage({ type: 'drag', nodeId: action.nodeId, ...position });
      this.applyPositions();
      return;
    }
    if (!this.host.contains(event.target)) return;
    const time = performance.now();
    if (time - this.lastHoverTime < 100) return;
    this.lastHoverTime = time;
    const hit = this.rayAt(event).intersectObjects([...this.nodeViews.values()].map((view) => view.mesh), true)[0];
    this.raycaster.params.Line.threshold = this.camera.position.distanceTo(this.controls.target) / Math.max(1, this.host.clientHeight) * 4;
    const edgeHit = hit ? null : this.raycaster.intersectObjects([...this.edgeViews.values()].map((view) => view.line))[0];
    this.setHover(studioNodeIdFromHit(hit?.object), edgeHit?.object.userData.edgeId, event);
  }

  pointerUp(event) {
    const action = this.pointerAction;
    if (!action || event.pointerId !== action.pointerId) return;
    this.cancelPointer();
    if (!action.nodeId) { if (!action.moved && event.button === 0) this.callbacks.onBackgroundClick?.(); return; }
    if (action.moved) {
      this.pinnedNodeIds.add(action.nodeId);
      this.callbacks.onPinChange?.(action.nodeId, true);
      const position = this.positions.get(action.nodeId);
      this.forceWorker?.postMessage({ type: 'pin', nodeId: action.nodeId, ...position });
      this.callbacks.onNodeDragEnd?.(action.nodeId, { x: position.x, y: position.y });
      this.lastClick = { id: '', time: 0 };
      return;
    }
    const time = performance.now();
    this.callbacks.onNodeClick?.(action.nodeId);
    if (this.lastClick.id === action.nodeId && time - this.lastClick.time < 340) {
      this.callbacks.onNodeDoubleClick?.(action.nodeId);
      this.lastClick = { id: '', time: 0 };
    } else this.lastClick = { id: action.nodeId, time };
  }

  cancelPointer() {
    if (this.pointerAction) {
      try { this.host.releasePointerCapture?.(this.pointerAction.pointerId); } catch { /* The browser may already have released it. */ }
    }
    this.pointerAction = null;
    if (this.controls) this.controls.enabled = true;
  }

  setHover(nodeId, edgeId, event) {
    const changed = this.hoveredNodeId !== (nodeId || '') || this.hoveredEdgeId !== (edgeId || '');
    this.hoveredNodeId = nodeId || '';
    if (this.hoveredEdgeId !== (edgeId || '')) this.callbacks.onEdgeHover?.(edgeId || null);
    this.hoveredEdgeId = edgeId || '';
    this.renderer.domElement.style.cursor = nodeId ? 'grab' : 'default';
    const text = studioNodeName(this.nodeById.get(nodeId));
    this.nodeTooltip.hidden = !text;
    this.nodeTooltip.textContent = text;
    if (text && event) {
      const rect = this.host.getBoundingClientRect();
      this.nodeTooltip.style.left = `${Math.max(8, Math.min(this.host.clientWidth - this.nodeTooltip.offsetWidth - 8, event.clientX - rect.left + 14))}px`;
      this.nodeTooltip.style.top = `${Math.max(8, Math.min(this.host.clientHeight - this.nodeTooltip.offsetHeight - 8, event.clientY - rect.top + 16))}px`;
    }
    if (changed) this.updateEmphasis();
  }

  worldPositionFor(node) {
    const position = this.positions.get(node.id);
    const layoutDepth = (this.layoutMode === 'layered' || this.layoutTween) && Number.isFinite(position.z)
      ? position.z : (this.depthFactors.get(node.id) || 0) * this.depthSpan;
    const depth = this.depthOverrides.get(node.id) ?? layoutDepth;
    return graphWorldPosition(node, position, depth);
  }

  applyPositions() {
    this.depthSpan = graphDepthSpan(this.positions, this.layoutMode);
    this.nodeViews.forEach((view) => view.mesh.position.copy(vector(this.worldPositionFor(view.node))));
    this.reportDepthSpan();
    this.updateEdgePositions();
    this.requestRender();
  }

  reportDepthSpan() {
    let min = Infinity; let max = -Infinity;
    this.nodeViews.forEach((view) => {
      min = Math.min(min, view.mesh.position.z);
      max = Math.max(max, view.mesh.position.z);
    });
    this.host.dataset.depthSpan = (Number.isFinite(min) ? max - min : 0).toFixed(2);
  }

  graphBounds() {
    const box = new THREE.Box3();
    this.nodeViews.forEach((view) => {
      const radius = view.radius * 2;
      box.expandByPoint(view.mesh.position.clone().addScalar(radius));
      box.expandByPoint(view.mesh.position.clone().addScalar(-radius));
    });
    if (box.isEmpty()) box.set(new THREE.Vector3(-100, -100, -100), new THREE.Vector3(100, 100, 100));
    return box;
  }

  moveCamera(position, target, animate = true) {
    this.requestRender();
    if (!animate || this.interactionState.reducedMotion || !this.interactionState.motionEnabled) {
      this.cameraTween = null;
      this.camera.position.copy(position);
      this.controls.target.copy(target);
      this.controls.update();
      this.lastLabelTime = -Infinity;
      this.reportViewportChange();
      return;
    }
    this.cameraTween = { from: this.camera.position.clone(), to: position.clone(), targetFrom: this.controls.target.clone(), targetTo: target.clone(), start: performance.now(), duration: 400 };
  }

  fitView(animate = true, targetBounds, { preserveFocus = false } = {}) {
    if (!this.camera || !this.nodeViews.size) return;
    if (!preserveFocus) this.cancelPendingFocus();
    const box = targetBounds || this.graphBounds();
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const distance = graphCameraDistance(size, this.camera.aspect);
    this.fitDistance = distance;
    if (this.world.fog) this.world.fog.density = 0.3 / Math.max(300, distance);
    this.grid.position.set(center.x, center.y, box.min.z - 140);
    this.grid.scale.setScalar(Math.max(1, Math.max(size.x, size.y) / 1900));
    this.moveCamera(center.clone().add(new THREE.Vector3(distance * 0.1, distance * 0.15, distance)), center, animate);
  }

  getViewport() {
    if (!this.camera) return null;
    const distance = this.camera.position.distanceTo(this.controls.target);
    const zoom = this.host.clientHeight / (2 * Math.tan(this.camera.fov * Math.PI / 360) * Math.max(1, distance));
    return { x: this.host.clientWidth / 2 - this.controls.target.x * zoom, y: this.host.clientHeight / 2 + this.controls.target.y * zoom, zoom, camera3d: { position: this.camera.position.toArray(), target: this.controls.target.toArray() } };
  }

  setViewport(viewport) {
    if (!this.camera || !viewport) return;
    this.cancelPendingFocus();
    this.forceFitPending = false;
    if (validCameraSnapshot(viewport.camera3d)) {
      this.moveCamera(new THREE.Vector3(...viewport.camera3d.position), new THREE.Vector3(...viewport.camera3d.target), false);
    } else if (Number.isFinite(viewport.x) && Number.isFinite(viewport.y) && Number(viewport.zoom) > 0) {
      this.setCenter((this.host.clientWidth / 2 - viewport.x) / viewport.zoom, (this.host.clientHeight / 2 - viewport.y) / viewport.zoom, { zoom: viewport.zoom });
    }
    this.restoredViewportPending = true;
    if (!this.layoutTween) this.ensureViewportVisible();
  }

  ensureViewportVisible() {
    if (!this.nodeViews.size || !this.camera || this.host.clientWidth < 32 || this.host.clientHeight < 32) return;
    this.camera.updateMatrixWorld();
    const frustum = new THREE.Frustum().setFromProjectionMatrix(new THREE.Matrix4().multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse));
    const visible = [...this.nodeViews.values()].filter((view) => frustum.intersectsSphere(new THREE.Sphere(view.mesh.position, view.radius * 1.8)));
    let barelyVisible = false;
    if (visible.length) {
      const points = visible.map((view) => view.mesh.position.clone().project(this.camera));
      const spanX = (Math.max(...points.map((point) => point.x)) - Math.min(...points.map((point) => point.x))) * this.host.clientWidth / 2;
      const spanY = (Math.max(...points.map((point) => point.y)) - Math.min(...points.map((point) => point.y))) * this.host.clientHeight / 2;
      const maxRadiusPixels = Math.max(...visible.map((view) => view.radius * this.host.clientHeight
        / (2 * Math.tan(this.camera.fov * Math.PI / 360) * Math.max(1, view.mesh.position.distanceTo(this.camera.position)))));
      barelyVisible = maxRadiusPixels < 1 && Math.max(spanX, spanY) < 24;
    }
    this.restoredViewportPending = false;
    if (!visible.length || barelyVisible) {
      this.selectionCameraSnapshot = null;
      this.fitView(false, undefined, { preserveFocus: true });
      this.host.dataset.viewportRecovered = 'true';
    }
  }

  setCenter(x, y, options = {}) {
    if (!this.camera || !Number.isFinite(x) || !Number.isFinite(y)) return;
    this.cancelPendingFocus();
    const target = new THREE.Vector3(x, -y, 0);
    const offset = this.camera.position.clone().sub(this.controls.target);
    if (Number(options.zoom) > 0) offset.setLength(this.host.clientHeight / (2 * Math.tan(this.camera.fov * Math.PI / 360) * options.zoom));
    this.moveCamera(target.clone().add(offset), target, Boolean(options.duration));
  }

  zoomBy(factor) {
    if (!this.camera || !(factor > 0)) return;
    this.cancelPendingFocus();
    const offset = this.camera.position.clone().sub(this.controls.target);
    offset.setLength(THREE.MathUtils.clamp(offset.length() / factor, this.controls.minDistance, this.controls.maxDistance));
    this.moveCamera(this.controls.target.clone().add(offset), this.controls.target, true);
  }

  setViewPreset(preset) {
    const directions = {
      front: new THREE.Vector3(0.12, 0.14, 1),
      top: new THREE.Vector3(0.02, 1, 0.14),
      side: new THREE.Vector3(1, 0.14, 0.3),
    };
    const direction = directions[preset];
    if (!this.camera || !this.nodeViews.size || !direction) return;
    this.cancelPendingFocus();
    const box = this.graphBounds();
    const center = box.getCenter(new THREE.Vector3());
    const distance = graphCameraDistance(box.getSize(new THREE.Vector3()), this.camera.aspect);
    this.fitDistance = distance;
    if (this.world.fog) this.world.fog.density = 0.3 / Math.max(300, distance);
    this.forceFitPending = false;
    this.moveCamera(center.clone().addScaledVector(direction.normalize(), distance), center, true);
  }

  focusNode(id) {
    const view = this.nodeViews.get(id);
    if (!view || !this.camera) return;
    this.focusTrackingId = id;
    this.forceFitPending = false;
    this.fitAfterForce = false;
    if (this.layoutTween || this.forceTargets.size || this.forceStatus === 'running' || this.forceStatus === 'initializing') {
      this.pendingFocusId = id;
      if (this.host?.dataset) this.host.dataset.focusPending = id;
      this.requestRender();
      return;
    }
    this.pendingFocusId = '';
    if (this.host?.dataset) this.host.dataset.focusPending = '';
    if (!this.selectionCameraSnapshot) this.selectionCameraSnapshot = this.getViewport().camera3d;
    const neighborIds = new Set([id]);
    this.scene.edges.forEach((edge) => {
      if (edge.source === id) neighborIds.add(edge.target);
      if (edge.target === id) neighborIds.add(edge.source);
    });
    const neighbors = [...neighborIds].map((nodeId) => this.nodeViews.get(nodeId)).filter(Boolean);
    const bounds = new THREE.Box3();
    neighbors.forEach((neighbor) => {
      bounds.expandByPoint(neighbor.mesh.position.clone().addScalar(neighbor.radius * 1.9));
      bounds.expandByPoint(neighbor.mesh.position.clone().addScalar(-neighbor.radius * 1.9));
    });
    const target = bounds.getCenter(new THREE.Vector3());
    // Fit the neighborhood in the current viewing plane, so orbiting does not change
    // which nearby nodes are included when selecting a node.
    const inverseRotation = this.camera.quaternion.clone().invert();
    const projectedBounds = new THREE.Box3();
    neighbors.forEach((neighbor) => {
      const position = neighbor.mesh.position.clone().sub(target).applyQuaternion(inverseRotation);
      projectedBounds.expandByPoint(position.clone().addScalar(neighbor.radius * 1.9));
      projectedBounds.expandByPoint(position.clone().addScalar(-neighbor.radius * 1.9));
    });
    const neighborhoodDistance = graphCameraDistance(projectedBounds.getSize(new THREE.Vector3()), this.camera.aspect);
    const distance = Math.min(this.fitDistance || Infinity, neighborhoodDistance);
    const offset = this.camera.position.clone().sub(this.controls.target).normalize().multiplyScalar(distance);
    this.forceFitPending = false;
    this.moveCamera(target.clone().add(offset), target, true);
  }

  cancelPendingFocus() {
    this.pendingFocusId = '';
    this.focusTrackingId = '';
    this.forceFitPending = false;
    this.fitAfterForce = false;
    if (this.host?.dataset) this.host.dataset.focusPending = '';
  }

  flushPendingFocus() {
    if (this.pendingFocusId && !this.layoutTween && !this.forceTargets.size
      && this.forceStatus !== 'running' && this.forceStatus !== 'initializing') this.focusNode(this.pendingFocusId);
  }

  restoreSelectionView() {
    this.cancelPendingFocus();
    if (!this.selectionCameraSnapshot || !this.camera) return;
    const snapshot = this.selectionCameraSnapshot;
    this.selectionCameraSnapshot = null;
    this.moveCamera(new THREE.Vector3(...snapshot.position), new THREE.Vector3(...snapshot.target), true);
  }

  reportViewportChange() {
    clearTimeout(this.viewportTimer);
    this.viewportTimer = setTimeout(() => { if (!this.destroyed) this.callbacks.onViewportChange?.(this.getViewport()); }, 160);
  }

  project(position) {
    this.projected.copy(position).project(this.camera);
    return { x: (this.projected.x * 0.5 + 0.5) * this.host.clientWidth, y: (-this.projected.y * 0.5 + 0.5) * this.host.clientHeight, visible: this.projected.z > -1 && this.projected.z < 1 };
  }

  drawLabels() {
    if (!this.presentation.labels) {
      this.nodeViews.forEach((view) => { view.label.hidden = true; });
      this.host.dataset.visibleNodeLabels = '0';
      return;
    }
    const labelNodeIds = studioLabelNodeIds([...this.nodeViews.values()].map((view) => view.node), true, this.interactionState);
    let visibleCount = 0;
    for (const view of this.nodeViews.values()) {
      const point = this.project(view.mesh.position);
      const width = Math.min(180, String(view.node.label).length * 11 + 18);
      const x = point.x - width / 2;
      const y = point.y + 18;
      const outside = !point.visible || x + width < 0 || x > this.host.clientWidth || y < 0 || y > this.host.clientHeight - 18;
      const visible = !outside && labelNodeIds.has(view.node.id);
      view.label.hidden = !visible;
      if (visible) {
        visibleCount += 1;
        view.label.style.transform = `translate3d(${point.x}px, ${y}px, 0) translateX(-50%)`;
      }
    }
    this.host.dataset.visibleNodeLabels = String(visibleCount);
  }

  drawMinimap() {
    const time = performance.now();
    if (time - this.lastMinimapDrawTime < 100) return;
    const canvas = this.minimap;
    if (!canvas || !canvas.clientWidth || !canvas.clientHeight) return;
    this.lastMinimapDrawTime = time;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) { canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr); }
    const context = canvas.getContext('2d');
    if (!context) return;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    if (this.backgroundColor) { context.fillStyle = this.backgroundColor; context.fillRect(0, 0, width, height); }
    const box = this.graphBounds();
    const size = box.getSize(new THREE.Vector3());
    const scale = Math.min((width - 18) / Math.max(1, size.x), (height - 18) / Math.max(1, size.y));
    const toMini = (point) => ({ x: 9 + (point.x - box.min.x) * scale, y: 9 + (box.max.y - point.y) * scale });
    this.minimapNodes = [];
    context.strokeStyle = this.themeColors.line; context.lineWidth = 0.6;
    this.edgeViews.forEach((view) => {
      const a = toMini(this.nodeViews.get(view.edge.source).mesh.position);
      const b = toMini(this.nodeViews.get(view.edge.target).mesh.position);
      context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y); context.stroke();
    });
    this.nodeViews.forEach((view) => {
      const point = toMini(view.mesh.position);
      this.minimapNodes.push({ ...point, id: view.node.id, label: view.node.label || view.node.id });
      context.fillStyle = metaFor(view.node, this.graphTheme.mode).color;
      const selected = view.node.id === this.interactionState.selectedId;
      context.beginPath(); context.arc(point.x, point.y, selected ? 3.4 : 2.1, 0, Math.PI * 2); context.fill();
      if (selected) {
        // Ring the selected node so it can be found on the minimap at a glance.
        context.strokeStyle = this.graphTheme.selection; context.lineWidth = 1.2;
        context.beginPath(); context.arc(point.x, point.y, 5.4, 0, Math.PI * 2); context.stroke();
      }
    });
    const center = toMini(this.controls.target);
    const distance = this.camera.position.distanceTo(this.controls.target);
    const visibleHeight = 2 * distance * Math.tan(this.camera.fov * Math.PI / 360);
    context.strokeStyle = this.themeColors.accent; context.fillStyle = this.themeColors.accent; context.lineWidth = 1.3;
    const h = visibleHeight * scale; const w = h * this.camera.aspect;
    context.globalAlpha = 0.08;
    context.fillRect(center.x - w / 2, center.y - h / 2, w, h);
    context.globalAlpha = 1;
    context.strokeRect(center.x - w / 2, center.y - h / 2, w, h);
    context.fillStyle = this.themeColors.accent; context.beginPath(); context.arc(center.x, center.y, 2, 0, Math.PI * 2); context.fill();
    this.minimapTransform = { minX: box.min.x, maxY: box.max.y, scale };
    canvas.dataset.nodeCount = String(this.nodeViews.size);
  }

  handleMinimapPointer(event) {
    if (!this.minimapTransform) return;
    event.preventDefault(); event.stopPropagation();
    const rect = this.minimap.getBoundingClientRect();
    const { minX, maxY, scale } = this.minimapTransform;
    this.setCenter(minX + (event.clientX - rect.left - 9) / scale, -(maxY - (event.clientY - rect.top - 9) / scale), { duration: 400 });
    this.forceFitPending = false;
  }

  handleMinimapHover(event) {
    const rect = this.minimap.getBoundingClientRect();
    const x = event.clientX - rect.left; const y = event.clientY - rect.top;
    let nearest; let distance = 14;
    for (const node of this.minimapNodes || []) {
      const candidate = Math.hypot(node.x - x, node.y - y);
      if (candidate < distance) { nearest = node; distance = candidate; }
    }
    this.minimap.title = nearest?.label || '点击小地图移动视角';
    this.callbacks.onMinimapHover?.(nearest?.label || '');
  }

  syncTheme(render = true) {
    if (this.destroyed || !this.host || !this.world) return;
    const css = getComputedStyle(this.host);
    let surface = this.host;
    let background = '';
    while (surface) {
      const candidate = getComputedStyle(surface).backgroundColor;
      const transparent = !candidate || candidate === 'transparent'
        || /^rgba\([^)]*,\s*0(?:\.0+)?\)$/.test(candidate)
        || /^rgb\([^)]*\/\s*0(?:\.0+)?\)$/.test(candidate);
      if (!transparent) { background = candidate; break; }
      surface = surface.parentElement;
    }
    this.backgroundColor = background;
    this.world.background = background ? new THREE.Color(background) : null;
    if (this.world.fog) this.world.fog.color.set(background || '#ffffff');
    this.graphTheme = resolveGraphTheme(graphThemeMode(background, document.documentElement.dataset.theme));
    this.host.style?.setProperty('--studio-graph-selection', this.graphTheme.selection);
    this.iconLibrary.setTheme(this.graphTheme.mode);
    this.lighting?.setTheme(this.graphTheme.mode);
    if (this.bloomPass) Object.assign(this.bloomPass, this.graphTheme.bloom);
    this.nodeViews.forEach((view) => {
      const meta = metaFor(view.node, this.graphTheme.mode);
      view.label.style.setProperty('--node-color', meta.color);
      if (!view.semanticIcon) { view.mesh.material.color.set(meta.color); view.mesh.material.emissive.set(meta.color); }
    });
    this.edgeViews.forEach((view) => {
      view.color = this.graphTheme.edge;
      view.particle?.material.color.set(this.graphTheme.selection);
    });
    this.themeColors = {
      line: this.graphTheme.edge,
      accent: css.getPropertyValue('--color-primary').trim() || css.color,
    };
    this.grid?.material.color.set(this.themeColors.line);
    if (this.grid) this.grid.material.opacity = this.graphTheme.gridOpacity;
    this.updateEmphasis();
    this.host.dataset.backgroundColor = background;
    this.host.dataset.graphTheme = this.graphTheme.mode;
    if (render && this.renderer && this.camera) {
      // Theme changes repaint once even in a background tab; no polling or
      // additional animation loop is introduced.
      this.orientNodeIcons();
      if (this.presentation.bloom && this.composer) this.composer.render(0);
      else this.renderer.render(this.world, this.camera);
      this.lastMinimapDrawTime = -Infinity;
      this.drawMinimap();
    }
    this.requestRender();
  }

  resizePostprocessing(width, height) {
    if (!this.composer) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 1.5, Math.sqrt(1600000 / (width * height)));
    const effectWidth = Math.max(1, Math.floor(width * ratio));
    const effectHeight = Math.max(1, Math.floor(height * ratio));
    this.host.dataset.postprocessingPixelRatio = ratio.toFixed(3);
    this.composer.setSize(effectWidth, effectHeight);
    this.antialiasPass?.uniforms.resolution.value.set(1 / effectWidth, 1 / effectHeight);
  }

  resize() {
    if (!this.renderer || this.destroyed) return;
    const width = Math.max(1, this.host.clientWidth);
    const height = Math.max(1, this.host.clientHeight);
    const ratio = Math.min(window.devicePixelRatio || 1, 1.5, Math.sqrt(4000000 / (width * height)));
    this.host.dataset.pixelRatio = ratio.toFixed(3);
    // Update dimensions and DPR together to avoid transient oversized buffers.
    this.renderer.setDrawingBufferSize(width, height, ratio);
    this.resizePostprocessing(width, height);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    if (!this.layoutTween) this.ensureViewportVisible();
    this.lastLabelTime = -Infinity;
    this.edgeViews.forEach((view) => view.emphasisLine?.material.resolution.set(width, height));
    this.drawMinimap();
    this.requestRender();
  }

  updateSelectionRing(time) {
    if (!this.selectionRing) return false;
    const selected = this.nodeViews.get(this.interactionState.selectedId);
    this.selectionRing.visible = Boolean(selected);
    if (!selected) return false;
    const pulsing = !this.interactionState.reducedMotion && this.interactionState.motionEnabled && time < this.selectionPulseUntil;
    const pulse = pulsing ? 1 + Math.sin(time / 180) * 0.045 : 1;
    this.selectionRing.position.copy(selected.mesh.position);
    this.selectionRing.quaternion.copy(this.camera.quaternion);
    this.selectionRing.scale.setScalar(selected.radius * 1.65 * pulse);
    this.selectionRing.material.color.set(this.graphTheme.selection);
    return pulsing;
  }

  tick(time) {
    this.frame = null;
    if (this.destroyed) return;
    if (document.hidden || !this.visible) return;
    const interacting = this.controlsActive || this.cameraTween || this.layoutTween || this.pointerAction || this.autoRotate || this.controlsMoving;
    if (time - this.lastTime < 1000 / (interacting ? 60 : 30) - 1) { this.requestRender(); return; }
    const dt = Math.min(0.05, (time - this.lastTime) / 1000 || 0.016);
    this.lastTime = time;
    let moving = false;
    const blend = this.interactionState.reducedMotion || !this.interactionState.motionEnabled ? 1 : 1 - Math.exp(-dt * 9);
    this.forceTargets.forEach((target, id) => {
      const current = this.positions.get(id);
      if (!current || this.pointerAction?.nodeId === id) return;
      current.x += (target.x - current.x) * blend; current.y += (target.y - current.y) * blend;
      moving = true;
      if (Math.hypot(target.x - current.x, target.y - current.y) < 0.3) this.forceTargets.delete(id);
    });
    if (this.layoutTween) {
      const tween = this.layoutTween;
      const progress = Math.min(1, (time - tween.start) / tween.duration);
      const eased = progress * progress * (3 - 2 * progress);
      tween.to.forEach((target, id) => {
        const from = tween.from.get(id);
        const current = this.positions.get(id);
        if (!from || !current || this.pointerAction?.nodeId === id) return;
        current.x = from.x + (target.x - from.x) * eased;
        current.y = from.y + (target.y - from.y) * eased;
        current.z = from.z + (target.z - from.z) * eased;
      });
      if (progress >= 1) this.completeLayoutTween();
      moving = true;
    }
    if (moving) this.applyPositions();
    this.flushPendingFocus();
    if (this.fitAfterForce && !this.forceTargets.size) { this.fitAfterForce = false; this.forceFitPending = false; this.fitView(true); }
    const cameraAnimating = Boolean(this.cameraTween);
    if (cameraAnimating) {
      const tween = this.cameraTween;
      const t = Math.min(1, (time - tween.start) / tween.duration);
      const eased = t * t * (3 - 2 * t);
      this.camera.position.lerpVectors(tween.from, tween.to, eased);
      this.controls.target.lerpVectors(tween.targetFrom, tween.targetTo, eased);
      if (t === 1) { this.cameraTween = null; this.reportViewportChange(); }
    }
    this.controls.autoRotate = this.autoRotate && this.interactionState.motionEnabled && !this.pointerAction;
    this.controlsMoving = this.controls.update(dt);
    const pulsing = this.updateSelectionRing(time);
    const renderNeeded = this.dirty || moving || cameraAnimating || this.controlsMoving || pulsing;
    if (!renderNeeded) return;
    this.dirty = false;
    this.orientNodeIcons();
    this.edgeViews.forEach((view, id) => {
      if (!view.particle) return;
      view.particle.visible = this.presentation.particles && view.animate && this.interactionState.motionEnabled && !this.interactionState.reducedMotion;
      if (view.particle.visible) view.particle.position.copy(view.curve.getPoint((time / 4500 + id.length * 0.131) % 1));
    });
    if (this.presentation.bloom && this.composer) this.composer.render(dt);
    else this.renderer.render(this.world, this.camera);
    // Labels track the camera at the render rate while moving so they do not lag
    // a full throttled step behind the nodes.
    if (interacting || moving || cameraAnimating || this.controlsMoving || time - this.lastLabelTime >= 1000 / 15) {
      this.drawLabels(); this.lastLabelTime = time;
    }
    if (time - this.lastMinimapTime > 100) {
      this.drawMinimap(); this.lastMinimapTime = time;
      const zoom = this.getViewport().zoom;
      const tier = zoom < 0.52 ? 'compact' : zoom < 0.86 ? 'standard' : 'detail';
      if (tier !== this.zoomTier) { this.zoomTier = tier; this.callbacks.onZoomTierChange?.(tier); }
      this.host.dataset.zoom = zoom.toFixed(3);
    }
    // Force messages and controls events wake this loop. Once damping and the
    // brief selection pulse settle, no animation frame or GPU draw is scheduled.
    if (moving || this.cameraTween || this.layoutTween || this.controlsMoving || this.controls.autoRotate || pulsing) this.requestRender();
  }

  clearViews() {
    this.nodeViews.forEach((view) => {
      this.world.remove(view.mesh);
      if (view.semanticIcon) disposeIconInstance(view.mesh);
      else view.materials.forEach(({ material }) => material.dispose());
      view.label.remove();
    });
    this.edgeViews.forEach((view) => {
      this.world.remove(view.line, view.arrow);
      if (view.emphasisLine) {
        this.world.remove(view.emphasisLine);
        view.emphasisLine.geometry.dispose(); view.emphasisLine.material.dispose();
      }
      if (view.particle) { this.world.remove(view.particle); view.particle.material.dispose(); }
      view.line.geometry.dispose(); view.line.material.dispose(); view.arrow.material.dispose();
    });
    this.nodeViews.clear(); this.edgeViews.clear();
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    cancelAnimationFrame(this.frame);
    clearTimeout(this.viewportTimer);
    this.stopForce(false);
    this.resizeObserver?.disconnect();
    this.minimapResizeObserver?.disconnect();
    this.intersectionObserver?.disconnect();
    this.listeners.forEach((remove) => remove());
    this.listeners = [];
    this.controls?.dispose();
    this.composer?.passes.forEach((pass) => pass.dispose?.());
    this.composer?.dispose();
    this.composer = null;
    this.antialiasPass = null;
    this.clearViews();
    this.iconLibrary.dispose();
    if (this.world) {
      const geometries = new Set(); const materials = new Set();
      this.world.traverse((object) => {
        if (object.geometry) geometries.add(object.geometry);
        if (object.material) (Array.isArray(object.material) ? object.material : [object.material]).forEach((material) => materials.add(material));
      });
      Object.values(this.geometries || {}).forEach((geometry) => geometries.add(geometry));
      if (this.arrowGeometry) geometries.add(this.arrowGeometry);
      if (this.particleGeometry) geometries.add(this.particleGeometry);
      geometries.forEach((geometry) => geometry.dispose()); materials.forEach((material) => material.dispose());
    }
    this.renderer?.dispose();
    this.renderer?.forceContextLoss();
    this.renderer?.domElement.remove();
    this.labelLayer?.remove();
    this.nodeTooltip?.remove();
    this.nodeViews.clear(); this.edgeViews.clear(); this.nodeById.clear();
    this.positions.clear(); this.basePositions.clear(); this.depthFactors.clear(); this.depthOverrides.clear(); this.attributeViews = [];
  }
}
