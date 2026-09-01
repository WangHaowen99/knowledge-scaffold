import { Application, Circle, Container, Graphics, Polygon, Rectangle, RoundedRectangle, Text } from 'pixi.js';
import { KIND_META } from '../studio-model.js';

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2.2;
const CLICK_DISTANCE = 6;
const DOUBLE_CLICK_MS = 320;
const MAX_PARTICLE_EDGES = 20;
const LARGE_GRAPH_NODE_LIMIT = 200;
const MINIMAP_FRAME_INTERVAL_MS = 100;
const FORCE_POSITION_FRAME_MS = 33;
const FORCE_SMOOTHING_MS = 150;
const AMBIENT_FRAME_INTERVAL_MS = 33;
const MIN_LABEL_SCREEN_SCALE = 0.9;
const MIN_TITLE_SCREEN_SCALE = 0.72;
const LABEL_GAP = 5;
const ATTRIBUTE_RADIUS = 20;
const ATTRIBUTE_GAP = 12;
const ATTRIBUTE_REVEAL_MS = 360;
const LABEL_TRACK_TRANSITION_RATE = 0.24;
const CLUSTER_META = {
  label: '关系簇', className: 'cluster', color: '#176b87', soft: '#b9e3ed', glow: 'rgba(23, 107, 135, .46)',
};

const EDGE_PALETTE = {
  rule: { color: 0x704100, label: 0x4a2b00, dash: [7, 5] },
  metric: { color: 0x48208e, label: 0x301361, dash: [4, 4] },
  script: { color: 0x8f1740, label: 0x65102d, dash: [3, 5] },
  object: { color: 0x0b4c91, label: 0x06366a },
  operation: { color: 0x07563d, label: 0x043c2b },
};

export function smoothGraphPosition(current, target, elapsedMS, smoothingMS = FORCE_SMOOTHING_MS) {
  const blend = 1 - Math.exp(-Math.max(0, elapsedMS) / Math.max(1, smoothingMS));
  return point(
    current.x + (target.x - current.x) * blend,
    current.y + (target.y - current.y) * blend,
  );
}

export function ambientNodeOffset(nodeId, componentKey, clock, zoom = 1) {
  const nodeHash = Math.abs(stableHash(nodeId));
  const componentHash = Math.abs(stableHash(componentKey));
  const nodePhase = (nodeHash % 6283) / 1000;
  const componentPhase = (componentHash % 6283) / 1000;
  const safeZoom = Math.max(MIN_ZOOM, zoom);
  const componentX = Math.sin(clock * 0.5 + componentPhase) * 1.8;
  const componentY = Math.cos(clock * 0.43 + componentPhase * 0.83) * 2.2;
  const nodeX = Math.sin(clock * (0.62 + (nodeHash % 5) * 0.025) + nodePhase)
    * (0.9 + (nodeHash % 4) * 0.15);
  const nodeY = Math.cos(clock * (0.56 + (nodeHash % 7) * 0.02) + nodePhase * 0.83)
    * (1.1 + (nodeHash % 5) * 0.15);
  return point((componentX + nodeX) / safeZoom, (componentY + nodeY) / safeZoom);
}

export class WebGLGraphRuntime {
  constructor(callbacks = {}) {
    this.callbacks = callbacks;
    this.app = null;
    this.host = null;
    this.minimap = null;
    this.nodeTooltip = null;
    this.world = null;
    this.layers = null;
    this.scene = { nodes: [], edges: [], routes: {} };
    this.nodeById = new Map();
    this.reciprocalEdgeKeys = new Set();
    this.nodeViews = new Map();
    this.positions = new Map();
    this.basePositions = new Map();
    this.ambientBasePositions = null;
    this.ambientNodeClock = 0;
    this.ambientNodeFrameElapsed = 0;
    this.ambientNodeMotionApplied = false;
    this.ambientComponentKeys = new Map();
    this.edgePaths = new Map();
    this.movedNodeIds = new Set();
    this.pinnedNodeIds = new Set();
    this.focusPinnedNodeId = '';
    this.attributeOrbitProgress = new Map();
    this.interactionState = emptyInteractionState();
    this.layoutMode = 'layered';
    this.forceStatus = 'idle';
    this.forceWorker = null;
    this.forceGeneration = 0;
    this.forcePositionsSettled = false;
    this.forceFitPending = false;
    this.forceTargetPositions = new Map();
    this.forceMotionActive = false;
    this.forceSettlePending = false;
    this.forceMotionFrameElapsed = 0;
    this.resizeObserver = null;
    this.pointerAction = null;
    this.hoveredEdgeId = '';
    this.lastClick = { id: '', time: 0 };
    this.selectionCameraSnapshot = null;
    this.selectionFocusActive = false;
    this.cameraAnimationGeneration = 0;
    this.viewportReportTimer = 0;
    this.lastMinimapDrawAt = 0;
    this.particles = [];
    this.particleClock = 0;
    this.labelPlacementState = new Map();
    this.labelViews = new Map();
    this.labelViewBuildCount = 0;
    this.labelSettlementEpoch = 0;
    this.labelTrackSwitchCount = 0;
    this.labelMaxFrameStep = 0;
    this.destroyed = false;
    this.sceneSignature = '';
    this.scenePresentationGeneration = 0;
    this.handleContextLost = this.handleContextLost.bind(this);
    this.handleWheel = this.handleWheel.bind(this);
    this.handleMinimapPointer = this.handleMinimapPointer.bind(this);
    this.tick = this.tick.bind(this);
  }

  async mount(host, minimap) {
    this.host = host;
    this.minimap = minimap;
    this.app = new Application();
    await this.app.init({
      preference: 'webgl',
      width: Math.max(1, host.clientWidth),
      height: Math.max(1, host.clientHeight),
      antialias: true,
      autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 2),
      background: '#f3f5f7',
      powerPreference: 'high-performance',
    });
    if (this.destroyed) {
      this.app.destroy(true, { children: true });
      return;
    }

    this.app.canvas.className = 'studio-pixi-webgl-canvas';
    this.app.canvas.setAttribute('aria-label', 'WebGL 知识图谱画布');
    host.appendChild(this.app.canvas);
    this.nodeTooltip = document.createElement('div');
    this.nodeTooltip.className = 'studio-webgl-node-tooltip';
    this.nodeTooltip.hidden = true;
    host.appendChild(this.nodeTooltip);
    this.createLayers();
    this.bindEvents();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.app.ticker.add(this.tick);
    this.resize();
    this.callbacks.onReady?.(rendererName(this.app));
  }

  createLayers() {
    const grid = new Graphics();
    const world = new Container();
    const edges = new Container();
    const particles = new Container();
    const nodes = new Container();
    const labels = new Container();
    nodes.sortableChildren = true;
    labels.sortableChildren = true;
    world.addChild(edges, particles, labels, nodes);
    this.app.stage.addChild(grid, world);
    this.world = world;
    this.layers = { grid, edges, particles, nodes, labels };
    if (this.host) {
      this.host.dataset.nodeLayerIndex = String(world.getChildIndex(nodes));
      this.host.dataset.labelLayerIndex = String(world.getChildIndex(labels));
    }
    this.app.stage.eventMode = 'static';
    this.app.stage.hitArea = new Rectangle(0, 0, this.app.screen.width, this.app.screen.height);
  }

  bindEvents() {
    this.app.stage.on('pointerdown', (event) => {
      if (event.target !== this.app.stage) return;
      this.pointerAction = {
        type: 'pan',
        start: point(event.global.x, event.global.y),
        origin: point(this.world.x, this.world.y),
        moved: false,
      };
    });
    this.app.stage.on('globalpointermove', (event) => this.onPointerMove(event));
    this.app.stage.on('pointerup', (event) => this.onPointerUp(event));
    this.app.stage.on('pointerupoutside', (event) => this.onPointerUp(event));
    this.app.canvas.addEventListener('wheel', this.handleWheel, { passive: false });
    this.app.canvas.addEventListener('webglcontextlost', this.handleContextLost);
    this.minimap?.addEventListener('pointerdown', this.handleMinimapPointer);
  }

  showNodeTooltip(text, event) {
    if (!this.nodeTooltip || !text) return;
    this.nodeTooltip.textContent = text;
    this.nodeTooltip.hidden = false;
    this.moveNodeTooltip(event);
  }

  moveNodeTooltip(event) {
    if (!this.nodeTooltip || this.nodeTooltip.hidden || !this.host) return;
    const offset = 12;
    const x = Math.max(8, Math.min(
      this.host.clientWidth - this.nodeTooltip.offsetWidth - 8,
      event.global.x + offset,
    ));
    const y = Math.max(8, Math.min(
      this.host.clientHeight - this.nodeTooltip.offsetHeight - 8,
      event.global.y + offset,
    ));
    this.nodeTooltip.style.left = `${x}px`;
    this.nodeTooltip.style.top = `${y}px`;
  }

  hideNodeTooltip() {
    if (this.nodeTooltip) this.nodeTooltip.hidden = true;
  }

  handleContextLost(event) {
    event.preventDefault();
    if (!this.destroyed) this.callbacks.onContextLost?.();
  }

  setScene(scene) {
    const signature = sceneSignature(scene);
    const preservePositions = signature === this.sceneSignature && this.positions.size > 0;
    const previousSelectedAttributeCount = this.nodeById.get(this.interactionState.selectedId)?.attributes?.length || 0;
    this.sceneSignature = signature;
    this.scene = scene;
    this.nodeById = new Map(scene.nodes.map((item) => [item.id, item]));
    this.ambientComponentKeys = ambientComponentKeys(scene.nodes, scene.edges);
    const directedEdges = new Set(scene.edges.map((item) => `${item.source}>${item.target}`));
    this.reciprocalEdgeKeys = new Set(scene.edges.filter((item) => (
      directedEdges.has(`${item.target}>${item.source}`)
    )).map((item) => `${item.source}>${item.target}`));
    if (this.host) {
      this.host.dataset.sceneNodeCount = String(scene.nodes.length);
      this.host.dataset.sceneEdgeCount = String(scene.edges.length);
      this.host.dataset.clusterNodeCount = String(scene.nodes.filter((item) => item.kind === 'cluster').length);
    }
    if (preservePositions) {
      const nextSelectedAttributeCount = this.nodeById.get(this.interactionState.selectedId)?.attributes?.length || 0;
      if (this.forceStatus === 'settled' && previousSelectedAttributeCount !== nextSelectedAttributeCount) {
        this.labelSettlementEpoch += 1;
      }
      this.drawNodes();
      this.drawEdges();
      this.drawMinimap();
      this.notifyScenePresented();
      return;
    }
    this.layers?.labels.removeChildren().forEach((child) => child.destroy({ children: true }));
    this.labelViews.clear();
    this.labelPlacementState.clear();
    this.labelViewBuildCount = 0;
    this.labelTrackSwitchCount = 0;
    this.labelMaxFrameStep = 0;
    this.basePositions = new Map(scene.nodes.map((item) => [
      item.id,
      point(item.position.x + item.width / 2, item.position.y + item.height / 2),
    ]));
    this.positions = new Map([...this.basePositions].map(([id, position]) => [id, { ...position }]));
    this.ambientBasePositions = null;
    this.ambientNodeClock = 0;
    this.ambientNodeFrameElapsed = 0;
    this.ambientNodeMotionApplied = false;
    this.forceTargetPositions.clear();
    this.forceMotionActive = false;
    this.forceSettlePending = false;
    this.forceMotionFrameElapsed = 0;
    this.movedNodeIds.clear();
    this.pinnedNodeIds.clear();
    this.hoveredEdgeId = '';
    this.selectionCameraSnapshot = null;
    this.selectionFocusActive = false;
    this.drawNodes();
    this.drawEdges();
    this.drawMinimap();
    if (this.layoutMode === 'force') {
      if (scene.nodes.length) this.startForce();
      else this.stopForce();
    } else this.fitView(false);
    this.notifyScenePresented();
  }

  notifyScenePresented() {
    const generation = this.scenePresentationGeneration + 1;
    this.scenePresentationGeneration = generation;
    this.app?.ticker.start();
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        if (!this.destroyed && generation === this.scenePresentationGeneration) {
          this.callbacks.onScenePresented?.();
        }
      });
    });
  }

  setInteractionState(state) {
    const previousSelectedId = this.interactionState.selectedId;
    this.interactionState = { ...emptyInteractionState(), ...state };
    this.syncFocusedObjectPin();
    if (this.forceStatus === 'settled' && previousSelectedId !== this.interactionState.selectedId) {
      this.labelSettlementEpoch += 1;
    }
    if (this.interactionState.motionEnabled) this.app?.ticker.start();
    if (!this.isAmbientNodeMotionActive()) this.restoreAmbientNodePositions(false);
    this.drawNodes();
    this.drawEdges();
  }

  setLayoutMode(mode) {
    if (mode === this.layoutMode) return;
    this.restoreAmbientNodePositions(false);
    this.layoutMode = mode;
    if (mode === 'force') {
      this.startForce();
      return;
    }
    this.stopForce();
    this.positions = new Map([...this.basePositions].map(([id, position]) => [id, { ...position }]));
    this.movedNodeIds.clear();
    this.pinnedNodeIds.clear();
    this.applyPositions();
    this.fitView(true);
  }

  setMotionEnabled(enabled) {
    this.interactionState.motionEnabled = enabled;
    if (!enabled) this.restoreAmbientNodePositions(false);
    this.drawNodes();
    this.drawEdges();
  }

  startForce() {
    if (!this.scene.nodes.length) return;
    this.restoreAmbientNodePositions(false);
    this.ambientBasePositions = null;
    this.stopForce(false);
    const generation = this.forceGeneration + 1;
    this.forceGeneration = generation;
    this.setForceStatus('initializing');
    this.forcePositionsSettled = false;
    this.forceFitPending = true;
    this.forceTargetPositions.clear();
    this.forceMotionActive = false;
    this.forceSettlePending = false;
    this.forceMotionFrameElapsed = 0;
    this.forceWorker = new Worker(new URL('./studio-force-layout.worker.js', import.meta.url), { type: 'module' });
    this.forceWorker.onmessage = ({ data }) => {
      if (data.generation !== this.forceGeneration || this.layoutMode !== 'force') return;
      if (data.type === 'status') {
        if (data.status !== 'settled') this.restoreAmbientNodePositions(false);
        this.setForceStatus(data.status);
        if (this.host && Number.isFinite(data.aspect)) {
          this.host.dataset.forceAspect = data.aspect.toFixed(3);
          this.host.dataset.forceTargetAspect = data.targetAspect.toFixed(3);
        }
        if (data.status === 'running') {
          this.ambientBasePositions = null;
          this.forcePositionsSettled = false;
        }
        if (['running', 'paused', 'settled'].includes(data.status)) this.drawEdges();
        return;
      }
      if (data.type !== 'positions' && data.type !== 'settled') return;
      if (data.type === 'positions' && this.forcePositionsSettled) return;
      if (!this.interactionState.motionEnabled && data.type !== 'settled') return;
      const targets = new Map();
      data.nodeIds.forEach((id, index) => {
        if (this.pointerAction?.type === 'node' && this.pointerAction.nodeId === id) return;
        targets.set(id, point(data.positions[index * 2], data.positions[index * 2 + 1]));
      });
      this.forceTargetPositions = targets;
      this.forceMotionActive = targets.size > 0;
      this.forceSettlePending = data.type === 'settled';
      this.app?.ticker.start();
      if (data.type === 'settled') {
        this.forcePositionsSettled = true;
      }
    };
    this.forceWorker.onerror = () => {
      this.setForceStatus('error');
      this.callbacks.onForceError?.();
    };
    this.forceWorker.postMessage({
      type: 'init',
      generation,
      seed: `studio-force-guide-v1:${this.scene.nodes.map((item) => item.id).sort().join('|')}`,
      nodes: this.scene.nodes.map((item) => ({
        id: item.id,
        x: this.positions.get(item.id)?.x || 0,
        y: this.positions.get(item.id)?.y || 0,
        width: item.width,
        height: item.height,
      })),
      links: uniqueForceLinks(this.scene.edges),
      viewport: {
        width: this.app?.screen.width || this.host?.clientWidth || 1,
        height: this.app?.screen.height || this.host?.clientHeight || 1,
      },
    });
    this.syncFocusedObjectPin();
  }

  stopForce(report = true) {
    this.restoreAmbientNodePositions(false);
    this.ambientBasePositions = null;
    this.forceGeneration += 1;
    this.forceTargetPositions.clear();
    this.forceMotionActive = false;
    this.forceSettlePending = false;
    this.forceMotionFrameElapsed = 0;
    if (this.forceWorker) {
      this.forceWorker.postMessage({ type: 'dispose' });
      this.forceWorker.terminate();
      this.forceWorker = null;
    }
    if (report) this.setForceStatus('idle');
  }

  pauseForce() {
    this.forceWorker?.postMessage({ type: 'pause' });
  }

  resumeForce() {
    this.forceWorker?.postMessage({ type: 'resume' });
  }

  reheatForce() {
    this.forceWorker?.postMessage({ type: 'reheat', alpha: 0.76 });
  }

  togglePin(nodeId) {
    const position = this.positions.get(nodeId);
    if (!position || this.layoutMode !== 'force') return false;
    if (this.pinnedNodeIds.has(nodeId)) {
      this.pinnedNodeIds.delete(nodeId);
      this.forceWorker?.postMessage({ type: 'unpin', nodeId });
      this.syncFocusedObjectPin();
      this.callbacks.onPinChange?.(nodeId, false);
      this.drawNodes();
      return false;
    }
    this.pinnedNodeIds.add(nodeId);
    this.forceWorker?.postMessage({ type: 'pin', nodeId, ...position });
    this.callbacks.onPinChange?.(nodeId, true);
    this.drawNodes();
    return true;
  }

  isPinned(nodeId) {
    return this.pinnedNodeIds.has(nodeId);
  }

  syncFocusedObjectPin() {
    const selected = this.nodeById.get(this.interactionState.selectedId);
    const nextNodeId = selected?.kind === 'object' ? selected.id : '';
    if (this.focusPinnedNodeId && this.focusPinnedNodeId !== nextNodeId
      && !this.pinnedNodeIds.has(this.focusPinnedNodeId)) {
      this.forceWorker?.postMessage({ type: 'unpin', nodeId: this.focusPinnedNodeId });
    }
    this.focusPinnedNodeId = nextNodeId;
    if (!nextNodeId || this.pinnedNodeIds.has(nextNodeId)) return;
    const position = this.positions.get(nextNodeId);
    if (position) this.forceWorker?.postMessage({ type: 'pin', nodeId: nextNodeId, ...position });
  }

  setForceStatus(status) {
    if (status === 'settled' && this.forceStatus !== 'settled') this.labelSettlementEpoch += 1;
    this.forceStatus = status;
    if (this.host) this.host.dataset.forceStatus = status;
    if (status === 'settled' || status === 'paused') this.updateSemanticVisibility();
    this.callbacks.onForceStatusChange?.(status);
  }

  drawNodes() {
    if (!this.layers) return;
    this.hideNodeTooltip();
    this.nodeViews.forEach((view, id) => {
      if (view.attributeOrbit) this.attributeOrbitProgress.set(id, view.attributeOrbit.progress);
    });
    this.layers.nodes.removeChildren().forEach((child) => child.destroy({ children: true }));
    this.nodeViews.clear();
    let expandedAttributeCount = 0;
    let truncatedNodeLabelCount = 0;
    let truncatedAttributeLabelCount = 0;
    let multiLineNodeLabelCount = 0;
    const largeGraph = this.scene.nodes.length > LARGE_GRAPH_NODE_LIMIT;
    const relationCount = new Map(this.scene.nodes.map((item) => [item.id, 0]));
    this.scene.edges.forEach((item) => {
      relationCount.set(item.source, (relationCount.get(item.source) || 0) + 1);
      relationCount.set(item.target, (relationCount.get(item.target) || 0) + 1);
    });

    this.scene.nodes.forEach((item) => {
      const meta = item.kind === 'cluster' ? CLUSTER_META : KIND_META[item.kind] || KIND_META.object;
      const color = cssColorToNumber(meta.color);
      const fill = cssColorToNumber(meta.soft);
      const selected = item.id === this.interactionState.selectedId;
      const highlighted = this.interactionState.activeNodeIds.has(item.id);
      const dimmed = this.interactionState.hasHighlight && !highlighted;
      const container = new Container();
      container.zIndex = selected ? 10 : highlighted ? 2 : 1;
      const position = this.positions.get(item.id) || this.basePositions.get(item.id) || point(0, 0);
      container.position.set(position.x, position.y);
      container.alpha = dimmed ? 0.18 : 1;
      container.eventMode = 'static';
      container.cursor = 'pointer';
      container.hitArea = nodeHitArea(item);
      container.on('pointerdown', (event) => this.beginNodePointer(item, event));

      const attributeOrbit = selected && item.kind === 'object'
        ? createObjectAttributeOrbit(item, color, fill, {
          show: (text, event) => this.showNodeTooltip(text, event),
          move: (event) => this.moveNodeTooltip(event),
          hide: () => this.hideNodeTooltip(),
        }, this.attributeOrbitProgress.get(item.id) ?? 0)
        : null;
      if (attributeOrbit) {
        this.attributeOrbitProgress.set(item.id, attributeOrbit.progress);
        expandedAttributeCount += attributeOrbit.items.length;
        truncatedAttributeLabelCount += attributeOrbit.truncatedLabelCount;
        const orbitRadius = attributeOrbit.items[0]?.targetRadius || attributeOrbit.nodeRadius;
        container.hitArea = new Circle(0, 0, orbitRadius + ATTRIBUTE_RADIUS);
        container.addChild(attributeOrbit.container);
      }

      const shape = new Graphics();
      drawNodeShape(shape, item, color, fill, selected, highlighted, this.pinnedNodeIds.has(item.id));
      container.addChild(shape);

      const texts = largeGraph ? null : createNodeTexts(item, meta, color, relationCount.get(item.id) || 0);
      if (texts) {
        container.addChild(texts.metaText, texts.title);
        if (texts.title.studioTruncated) truncatedNodeLabelCount += 1;
        if (texts.title.height > 18.5) multiLineNodeLabelCount += 1;
      }

      this.layers.nodes.addChild(container);
      this.nodeViews.set(item.id, {
        container,
        metaText: texts?.metaText || null,
        title: texts?.title || null,
        item,
        meta,
        color,
        relationCount: relationCount.get(item.id) || 0,
        attributeOrbit,
        titleBaseScale: texts?.title.scale.x || 1,
        titleBaseWidth: texts?.title.width || 0,
      });
    });
    if (this.host) {
      this.host.dataset.expandedAttributeCount = String(expandedAttributeCount);
      this.host.dataset.truncatedNodeLabelCount = String(truncatedNodeLabelCount);
      this.host.dataset.truncatedAttributeLabelCount = String(truncatedAttributeLabelCount);
      this.host.dataset.multiLineNodeLabelCount = String(multiLineNodeLabelCount);
      this.host.dataset.nodeTitleFontSize = '15';
      this.host.dataset.attributeLabelFontSize = '10';
    }
    this.reportAttributeOrbitState();
    this.updateSemanticVisibility();
  }

  attachNodeTexts(view) {
    if (view.title) return;
    const texts = createNodeTexts(view.item, view.meta, view.color, view.relationCount);
    view.container.addChild(texts.metaText, texts.title);
    view.metaText = texts.metaText;
    view.title = texts.title;
    view.titleBaseScale = texts.title.scale.x;
    view.titleBaseWidth = texts.title.width;
  }

  reportAttributeOrbitState() {
    if (!this.host) return;
    const progresses = [...this.nodeViews.values()]
      .map((view) => view.attributeOrbit?.progress)
      .filter(Number.isFinite);
    this.host.dataset.attributeOrbitMinProgress = progresses.length
      ? Math.min(...progresses).toFixed(3)
      : '0.000';
    this.host.dataset.focusPinnedNodeId = this.focusPinnedNodeId;
    const focusPosition = this.positions.get(this.focusPinnedNodeId);
    this.host.dataset.focusPinnedWorldX = focusPosition ? focusPosition.x.toFixed(2) : '';
    this.host.dataset.focusPinnedWorldY = focusPosition ? focusPosition.y.toFixed(2) : '';
    const orbit = [...this.nodeViews.values()].find((view) => view.attributeOrbit)?.attributeOrbit;
    this.host.dataset.attributeOrder = orbit
      ? orbit.items.map((item) => attributeStableKey(item.field)).join('|')
      : '';
  }

  drawEdges() {
    if (!this.layers) return;
    this.layers.edges.removeChildren().forEach((child) => child.destroy());
    this.layers.particles.removeChildren().forEach((child) => child.destroy());
    this.edgePaths.clear();
    this.particles = [];
    const labelEntries = [];
    const viewport = this.worldViewportBounds(160);
    const graphicsByStyle = new Map();
    const edgeBudget = this.edgeRenderBudget(this.world?.scale.x || 1);
    const edgeStride = Number.isFinite(edgeBudget)
      ? Math.max(1, Math.ceil(this.scene.edges.length / edgeBudget))
      : 1;

    this.scene.edges.forEach((item, edgeIndex) => {
      const active = this.interactionState.activeEdgeIds.has(item.id);
      const hovered = this.hoveredEdgeId === item.id;
      const priority = active || hovered || this.isSelectedNeighborEdge(item);
      if (!priority && edgeIndex % edgeStride !== 0) return;
      const points = this.edgePoints(item);
      if (points.length < 2) return;
      this.edgePaths.set(item.id, points);
      const style = edgeStyle(item.kind, this.nodeById.get(item.source)?.kind);
      if (!priority && !polylineIntersectsRect(points, viewport)) return;
      const dimmed = this.interactionState.hasHighlight && !active && !hovered;
      const edgeVisual = {
        color: style.color,
        width: active || hovered ? 2.7 : 1.45,
        alpha: dimmed ? 0.1 : active || hovered ? 1 : 0.8,
        dash: style.dash,
      };
      const styleKey = `${edgeVisual.color}|${edgeVisual.width}|${edgeVisual.alpha}|${edgeVisual.dash?.join(',') || ''}`;
      if (!graphicsByStyle.has(styleKey)) graphicsByStyle.set(styleKey, new Graphics());
      const graphics = graphicsByStyle.get(styleKey);
      drawEdgeLine(graphics, points, edgeVisual);
      drawArrow(graphics, points, style.color, dimmed ? 0.1 : active || hovered ? 1 : 0.95);

      if (item.label) labelEntries.push({ edge: item, points, style, state: { active, hovered, dimmed } });
      if (this.shouldAnimateEdge(item, active, hovered)) {
        const particleCount = active || this.isSelectedNeighborEdge(item) ? 2 : 1;
        for (let index = 0; index < particleCount && this.particles.length < MAX_PARTICLE_EDGES; index += 1) {
          this.createParticle(item.id, points, style.color, index, particleCount);
        }
      }
    });
    graphicsByStyle.forEach((graphics) => this.layers.edges.addChild(graphics));
    graphicsByStyle.forEach((graphics) => this.layers.edges.addChild(graphics));
    this.drawEdgeLabels(labelEntries);
    if (this.host) {
      this.host.dataset.visibleEdgeBatchCount = String(graphicsByStyle.size);
      this.host.dataset.visibleEdgeCount = String(this.edgePaths.size);
      this.host.dataset.edgeBudget = Number.isFinite(edgeBudget) ? String(edgeBudget) : 'all';
    }
  }

  edgeRenderBudget(zoom) {
    if (this.scene.edges.length <= 500) return Infinity;
    if (this.interactionState.selectedId) return 700;
    if (zoom < 0.62) return 360;
    if (zoom < 0.9) return 700;
    return 1200;
  }

  worldViewportBounds(marginScreen = 0) {
    if (!this.app || !this.world) return { left: -Infinity, top: -Infinity, right: Infinity, bottom: Infinity };
    const topLeft = this.screenToWorld(-marginScreen, -marginScreen);
    const bottomRight = this.screenToWorld(
      this.app.screen.width + marginScreen,
      this.app.screen.height + marginScreen,
    );
    return { left: topLeft.x, top: topLeft.y, right: bottomRight.x, bottom: bottomRight.y };
  }

  edgePoints(edge) {
    const source = this.positions.get(edge.source);
    const target = this.positions.get(edge.target);
    const sourceNode = this.nodeById.get(edge.source);
    const targetNode = this.nodeById.get(edge.target);
    if (!source || !target || !sourceNode || !targetNode) return [];

    const useDynamicPath = this.layoutMode === 'force'
      || this.movedNodeIds.has(edge.source)
      || this.movedNodeIds.has(edge.target);
    if (!useDynamicPath && this.scene.routes[edge.id]?.length >= 2) return this.scene.routes[edge.id];

    const sourceAnchor = shapeAnchor(source, sourceNode, target);
    const targetAnchor = shapeAnchor(target, targetNode, source);
    const reciprocal = this.reciprocalEdgeKeys.has(`${edge.source}>${edge.target}`);
    if (!reciprocal) return [sourceAnchor, targetAnchor];
    return quadraticPoints(sourceAnchor, targetAnchor, 28, 18);
  }

  drawEdgeLabels(entries) {
    const zoom = this.world?.scale.x || 1;
    const clustered = this.scene.nodes.some((node) => node.kind === 'cluster');
    const labelScale = Math.max(1, MIN_LABEL_SCREEN_SCALE / zoom);
    const viewport = this.worldViewportBounds(120);
    const nodeObstacles = this.scene.nodes.filter((item) => {
      const center = this.positions.get(item.id) || point(0, 0);
      const radius = Math.max(item.width, item.height) / 2 + 5;
      return circleIntersectsRect(center, radius, viewport);
    }).map((item) => ({
      center: this.positions.get(item.id) || point(0, 0),
      radius: Math.max(item.width, item.height) / 2 + 5,
    })).concat(this.attributeObstacles());
    const placed = [];
    const visibleLabelIds = new Set();
    const sorted = [...entries].sort((left, right) => {
      const leftPriority = Number(left.state.active) * 2 + Number(left.state.hovered);
      const rightPriority = Number(right.state.active) * 2 + Number(right.state.hovered);
      return rightPriority - leftPriority || left.edge.id.localeCompare(right.edge.id);
    });
    const labelBudget = this.edgeLabelBudget(zoom);
    const budgeted = sorted.filter((entry, index) => (
      entry.state.active || entry.state.hovered || index < labelBudget
    ));

    budgeted.forEach(({ edge, points, style, state }) => {
      const emphasized = state.active || state.hovered;
      const viewSignature = `${edge.label}|${style.label}|${Number(emphasized)}`;
      let view = this.labelViews.get(edge.id);
      if (!view || view.signature !== viewSignature) {
        if (view) {
          this.layers.labels.removeChild(view.container);
          view.container.destroy({ children: true });
        }
        view = createEdgeLabelView(edge.label, style.label, emphasized);
        this.labelViewBuildCount += 1;
        this.labelViews.set(edge.id, view);
        this.layers.labels.addChild(view.container);
      }
      const { container, label } = view;
      const width = (label.width + 16) * labelScale;
      const height = (label.height + 8) * labelScale;
      const previous = this.labelPlacementState.get(edge.id);
      let target = null;
      let blockedFrames = 0;
      if (previous) {
        const preferred = labelCandidateForTrack(
          points,
          previous.fraction,
          0,
          width,
          height,
          placed,
          nodeObstacles,
        );
        blockedFrames = preferred.blocked ? previous.blockedFrames + 1 : 0;
        const maySettleOnce = this.forceStatus === 'settled'
          && !this.pointerAction
          && previous.settlementEpoch !== this.labelSettlementEpoch;
        if (!maySettleOnce || !preferred.blocked) target = preferred;
      }
      if (!target) {
        target = findLabelPlacement({ points, width, height, placed, nodeObstacles });
        blockedFrames = target.blocked ? blockedFrames : 0;
      }
      const trackChanged = Boolean(previous) && previous.fraction !== target.fraction;
      if (trackChanged) this.labelTrackSwitchCount += 1;
      const transitioning = trackChanged || previous?.transitioning;
      const previousFraction = previous?.displayFraction ?? previous?.fraction ?? target.fraction;
      const displayFraction = transitioning && previous
        ? previousFraction + (target.fraction - previousFraction) * LABEL_TRACK_TRANSITION_RATE
        : target.fraction;
      const placement = labelCandidateForTrack(
        points,
        displayFraction,
        0,
        width,
        height,
        placed,
        nodeObstacles,
      );
      if (placement.blocked && !emphasized) {
        container.visible = false;
        visibleLabelIds.add(edge.id);
        return;
      }
      container.visible = true;
      visibleLabelIds.add(edge.id);
      const frameStep = previous ? Math.hypot(placement.x - previous.x, placement.y - previous.y) : 0;
      this.labelMaxFrameStep = Math.max(this.labelMaxFrameStep, frameStep);
      this.labelPlacementState.set(edge.id, {
        fraction: target.fraction,
        displayFraction,
        offset: 0,
        x: placement.x,
        y: placement.y,
        blockedFrames,
        settlementEpoch: this.forceStatus === 'settled'
          ? this.labelSettlementEpoch
          : previous?.settlementEpoch ?? -1,
        transitioning: transitioning && Math.abs(target.fraction - displayFraction) > 0.002,
      });
      container.position.set(placement.x, placement.y);
      container.scale.set(labelScale);
      container.alpha = 1;
      label.alpha = state.dimmed ? 0.58 : emphasized ? 1 : 0.94;
      container.zIndex = emphasized ? 2 : 1;
      placed.push({ edgeId: edge.id, rect: placement.rect });
    });

    this.labelViews.forEach((view, edgeId) => {
      if (visibleLabelIds.has(edgeId)) return;
      this.layers.labels.removeChild(view.container);
      view.container.destroy({ children: true });
      this.labelViews.delete(edgeId);
      this.labelPlacementState.delete(edgeId);
    });

    if (this.host) {
      this.host.dataset.labelCount = String(placed.length);
      this.host.dataset.labelBudget = Number.isFinite(labelBudget) ? String(labelBudget) : 'all';
      this.host.dataset.labelCollisionCount = String(countLabelCollisions(placed, nodeObstacles));
      this.host.dataset.minLabelScreenFont = (10 * labelScale * zoom).toFixed(1);
      this.host.dataset.labelTrackSwitchCount = String(this.labelTrackSwitchCount);
      this.host.dataset.labelMaxFrameStep = this.labelMaxFrameStep.toFixed(2);
      this.host.dataset.labelViewCount = String(this.labelViews.size);
      this.host.dataset.labelViewBuildCount = String(this.labelViewBuildCount);
      this.host.dataset.labelMaxNormalOffset = Math.max(
        0,
        ...[...this.labelPlacementState.values()].map((item) => Math.abs(item.offset || 0)),
      ).toFixed(2);
    }
  }

  edgeLabelBudget(zoom) {
    const clustered = this.scene.nodes.some((node) => node.kind === 'cluster');
    if (clustered && !this.interactionState.selectedId) {
      if (this.forceStatus === 'initializing' || this.forceStatus === 'running') return 8;
      if (zoom < 0.9) return 10;
      return 16;
    }
    const edgeCount = this.scene.edges.length;
    if (this.forceStatus === 'initializing' || this.forceStatus === 'running') {
      return Math.min(edgeCount, 40, Math.max(18, Math.round(edgeCount * 0.15)));
    }
    if (this.interactionState.selectedId) return Math.min(edgeCount, 260);
    const compact = zoom < 0.62;
    const standard = !compact && zoom < 0.9;
    const factor = compact ? 0.24 : standard ? 0.4 : 0.64;
    const floor = compact ? 24 : standard ? 36 : 48;
    const ceiling = compact ? 80 : standard ? 160 : 320;
    return Math.min(edgeCount, ceiling, Math.max(floor, Math.round(edgeCount * factor)));
  }

  attributeObstacles() {
    const selected = this.nodeById.get(this.interactionState.selectedId);
    const center = selected ? this.positions.get(selected.id) : null;
    if (!selected || selected.kind !== 'object' || !center) return [];
    return objectAttributeLayout(selected).map((attribute) => ({
      center: point(center.x + attribute.x, center.y + attribute.y),
      radius: ATTRIBUTE_RADIUS + 5,
    }));
  }

  tickAttributeExpansion(deltaMS) {
    this.nodeViews.forEach((view) => {
      const orbit = view.attributeOrbit;
      if (!orbit || orbit.progress >= 1) return;
      orbit.progress = Math.min(1, orbit.progress + deltaMS / ATTRIBUTE_REVEAL_MS);
      this.attributeOrbitProgress.set(view.item.id, orbit.progress);
      applyAttributeOrbitProgress(orbit);
    });
    this.reportAttributeOrbitState();
  }

  shouldAnimateEdge(edge, active, hovered) {
    const forceAllowsParticles = this.layoutMode !== 'force'
      || this.forceStatus === 'paused'
      || this.forceStatus === 'settled';
    const selectedNeighbor = this.isSelectedNeighborEdge(edge);
    if (this.layoutMode === 'force') {
      return this.interactionState.motionEnabled
        && forceAllowsParticles
        && selectedNeighbor
        && this.particles.length < MAX_PARTICLE_EDGES;
    }
    return this.interactionState.motionEnabled
      && forceAllowsParticles
      && this.particles.length < MAX_PARTICLE_EDGES
      && (active || hovered || selectedNeighbor);
  }

  isSelectedNeighborEdge(edge) {
    return Boolean(this.interactionState.selectedId)
      && (edge.source === this.interactionState.selectedId || edge.target === this.interactionState.selectedId);
  }

  createParticle(edgeId, points, color, index, count) {
    const graphics = new Graphics()
      .circle(0, 0, 7).fill({ color, alpha: 0.18 })
      .circle(0, 0, 4).fill({ color });
    this.layers.particles.addChild(graphics);
    this.particles.push({
      edgeId,
      graphics,
      points,
      offset: index / count + stableParticleOffset(edgeId),
      speed: 0.3 + index * 0.035,
      baseAlpha: 0.82,
      alphaRange: 0.18,
    });
  }

  applyPositions() {
    this.nodeViews.forEach((view, id) => {
      const position = this.positions.get(id);
      if (position) view.container.position.set(position.x, position.y);
    });
    this.updateSemanticVisibility();
    this.drawEdges();
    this.drawMinimap();
    this.reportAttributeOrbitState();
  }

  beginNodePointer(node, event) {
    event.stopPropagation();
    this.hideNodeTooltip();
    this.restoreAmbientNodePositions(true);
    const position = this.positions.get(node.id);
    if (!position) return;
    this.forceTargetPositions.delete(node.id);
    this.pointerAction = {
      type: 'node',
      nodeId: node.id,
      start: point(event.global.x, event.global.y),
      origin: { ...position },
      moved: false,
    };
  }

  onPointerMove(event) {
    if (!this.pointerAction) {
      this.updateHoveredEdge(event.global);
      return;
    }
    const dx = event.global.x - this.pointerAction.start.x;
    const dy = event.global.y - this.pointerAction.start.y;
    if (Math.hypot(dx, dy) >= CLICK_DISTANCE) this.pointerAction.moved = true;
    if (this.pointerAction.type === 'pan') {
      this.world.position.set(this.pointerAction.origin.x + dx, this.pointerAction.origin.y + dy);
      this.updateSemanticVisibility();
      this.drawMinimap();
      return;
    }
    if (!this.pointerAction.moved) return;
    if (!this.pointerAction.dragStarted) {
      this.pointerAction.dragStarted = true;
      this.lastClick = { id: '', time: 0 };
    }
    const next = point(
      this.pointerAction.origin.x + dx / this.world.scale.x,
      this.pointerAction.origin.y + dy / this.world.scale.y,
    );
    this.positions.set(this.pointerAction.nodeId, next);
    this.movedNodeIds.add(this.pointerAction.nodeId);
    if (!this.pinnedNodeIds.has(this.pointerAction.nodeId)) {
      this.pinnedNodeIds.add(this.pointerAction.nodeId);
      this.callbacks.onPinChange?.(this.pointerAction.nodeId, true);
    }
    this.forceWorker?.postMessage({ type: 'drag', nodeId: this.pointerAction.nodeId, ...next });
    this.applyPositions();
  }

  onPointerUp() {
    const action = this.pointerAction;
    this.pointerAction = null;
    if (!action) return;
    if (action.type === 'pan') {
      if (!action.moved) this.callbacks.onBackgroundClick?.();
      else this.reportViewportChange();
      return;
    }
    if (action.moved) {
      const position = this.positions.get(action.nodeId);
      if (position && this.layoutMode === 'force') {
        this.forceWorker?.postMessage({ type: 'pin', nodeId: action.nodeId, ...position });
      }
      if (position) this.callbacks.onNodeDragEnd?.(action.nodeId, { ...position });
      this.drawNodes();
      return;
    }
    const now = performance.now();
    const isDoubleClick = this.lastClick.id === action.nodeId && now - this.lastClick.time <= DOUBLE_CLICK_MS;
    this.callbacks.onNodeClick?.(action.nodeId);
    if (isDoubleClick) {
      this.lastClick = { id: '', time: 0 };
      this.callbacks.onNodeDoubleClick?.(action.nodeId);
    } else {
      this.lastClick = { id: action.nodeId, time: now };
    }
  }

  updateHoveredEdge(globalPoint) {
    if (!this.world || !this.edgePaths.size) return;
    const worldPoint = this.screenToWorld(globalPoint.x, globalPoint.y);
    const threshold = 8 / this.world.scale.x;
    let nearest = '';
    let nearestDistance = threshold;
    this.edgePaths.forEach((points, edgeId) => {
      const distance = pointToPolylineDistance(worldPoint, points);
      if (distance >= nearestDistance) return;
      nearest = edgeId;
      nearestDistance = distance;
    });
    if (nearest === this.hoveredEdgeId) return;
    this.hoveredEdgeId = nearest;
    this.callbacks.onEdgeHover?.(nearest || null);
    this.drawEdges();
  }

  handleWheel(event) {
    event.preventDefault();
    const bounds = this.app.canvas.getBoundingClientRect();
    const screenX = event.clientX - bounds.left;
    const screenY = event.clientY - bounds.top;
    const factor = Math.exp(-event.deltaY * 0.0012);
    this.setZoom(this.world.scale.x * factor, screenX, screenY);
  }

  setZoom(value, screenX = this.app.screen.width / 2, screenY = this.app.screen.height / 2) {
    const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
    const before = this.screenToWorld(screenX, screenY);
    this.world.scale.set(zoom);
    this.world.position.set(screenX - before.x * zoom, screenY - before.y * zoom);
    this.updateSemanticVisibility();
    this.drawEdges();
    this.drawMinimap();
    this.reportViewportChange();
  }

  zoomBy(factor) {
    this.setZoom(this.world.scale.x * factor);
  }

  getViewport() {
    return { x: this.world.x, y: this.world.y, zoom: this.world.scale.x };
  }

  setViewport(viewport) {
    if (!this.world || !Number.isFinite(viewport?.x) || !Number.isFinite(viewport?.y)) return;
    const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Number(viewport.zoom) || 1));
    this.cameraAnimationGeneration += 1;
    this.forceFitPending = false;
    this.world.scale.set(zoom);
    this.world.position.set(viewport.x, viewport.y);
    this.updateSemanticVisibility();
    this.drawEdges();
    this.drawMinimap();
  }

  reportViewportChange() {
    window.clearTimeout(this.viewportReportTimer);
    this.viewportReportTimer = window.setTimeout(() => {
      this.viewportReportTimer = 0;
      this.callbacks.onViewportChange?.(this.getViewport());
    }, 120);
  }

  setCenter(x, y, options = {}) {
    const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, options.zoom ?? this.world.scale.x));
    const target = {
      x: this.app.screen.width / 2 - x * zoom,
      y: this.app.screen.height / 2 - y * zoom,
      scale: zoom,
    };
    if (options.duration) this.animateCamera(target);
    else {
      this.world.scale.set(zoom);
      this.world.position.set(target.x, target.y);
      this.updateSemanticVisibility();
      this.drawEdges();
      this.drawMinimap();
    }
  }

  fitView(animate = true) {
    if (!this.app || !this.scene.nodes.length) return;
    const bounds = this.graphBounds();
    if (this.host) {
      const zoom = this.world.scale.x;
      const minimumDiameter = this.scene.nodes.reduce(
        (result, item) => Math.min(result, Math.min(item.width, item.height)),
        Infinity,
      );
      const maximumDiameter = this.scene.nodes.reduce(
        (result, item) => Math.max(result, Math.max(item.width, item.height)),
        0,
      );
      this.host.dataset.graphAspect = (bounds.width / Math.max(1, bounds.height)).toFixed(3);
      this.host.dataset.diagonalCorrelation = positionCorrelation([...this.positions.values()]).toFixed(3);
      this.host.dataset.zoom = zoom.toFixed(3);
      this.host.dataset.cameraCenterX = ((this.app.screen.width / 2 - this.world.x) / zoom).toFixed(2);
      this.host.dataset.cameraCenterY = ((this.app.screen.height / 2 - this.world.y) / zoom).toFixed(2);
      this.host.dataset.minNodeScreenDiameter = Number.isFinite(minimumDiameter)
        ? (minimumDiameter * zoom).toFixed(1)
        : '0';
      this.host.dataset.maxNodeScreenDiameter = (maximumDiameter * zoom).toFixed(1);
      this.host.dataset.horizontalNodeOverflowCount = String(this.scene.nodes.filter((item) => {
        const position = this.positions.get(item.id);
        if (!position) return false;
        const radius = Math.min(item.width, item.height) / 2;
        const left = (position.x - radius) * zoom + this.world.x;
        const right = (position.x + radius) * zoom + this.world.x;
        return left < 0 || right > this.app.screen.width;
      }).length);
      const selectedPosition = this.positions.get(this.interactionState.selectedId);
      if (selectedPosition) {
        this.host.dataset.selectedNodeWorldX = selectedPosition.x.toFixed(2);
        this.host.dataset.selectedNodeWorldY = selectedPosition.y.toFixed(2);
      } else {
        delete this.host.dataset.selectedNodeWorldX;
        delete this.host.dataset.selectedNodeWorldY;
      }
    }
    const padding = Math.min(48, Math.max(22, Math.min(this.app.screen.width, this.app.screen.height) * 0.055));
    const scale = Math.max(MIN_ZOOM, Math.min(1.35,
      Math.min(
        (this.app.screen.width - padding * 2) / Math.max(1, bounds.width),
        (this.app.screen.height - padding * 2) / Math.max(1, bounds.height),
      )));
    if (this.host) {
      this.host.dataset.fitHorizontalUtilization = ((bounds.width * scale) / this.app.screen.width).toFixed(3);
      this.host.dataset.fitVerticalUtilization = ((bounds.height * scale) / this.app.screen.height).toFixed(3);
      this.host.dataset.fitPadding = padding.toFixed(1);
    }
    const target = {
      x: this.app.screen.width / 2 - bounds.centerX * scale,
      y: this.app.screen.height / 2 - bounds.centerY * scale,
      scale,
    };
    if (!animate || this.interactionState.reducedMotion) {
      this.world.scale.set(target.scale);
      this.world.position.set(target.x, target.y);
      this.updateSemanticVisibility();
      this.drawEdges();
      this.drawMinimap();
      return;
    }
    this.animateCamera(target);
  }

  focusNode(nodeId) {
    const position = this.positions.get(nodeId);
    if (!position || !this.app) return;
    if (!this.selectionFocusActive) {
      const zoom = this.world.scale.x;
      this.selectionCameraSnapshot = {
        centerX: (this.app.screen.width / 2 - this.world.x) / zoom,
        centerY: (this.app.screen.height / 2 - this.world.y) / zoom,
        zoom,
      };
      this.selectionFocusActive = true;
    }
    const scale = Math.max(0.86, Math.min(1.15, this.world.scale.x));
    this.animateCamera({
      x: this.app.screen.width / 2 - position.x * scale,
      y: this.app.screen.height / 2 - position.y * scale,
      scale,
    });
  }

  restoreSelectionView() {
    if (!this.selectionFocusActive || !this.selectionCameraSnapshot || !this.app) return;
    const snapshot = this.selectionCameraSnapshot;
    this.selectionCameraSnapshot = null;
    this.selectionFocusActive = false;
    this.animateCamera({
      x: this.app.screen.width / 2 - snapshot.centerX * snapshot.zoom,
      y: this.app.screen.height / 2 - snapshot.centerY * snapshot.zoom,
      scale: snapshot.zoom,
    });
  }

  animateCamera(target) {
    const generation = this.cameraAnimationGeneration + 1;
    this.cameraAnimationGeneration = generation;
    const start = { x: this.world.x, y: this.world.y, scale: this.world.scale.x };
    const startedAt = performance.now();
    const duration = 360;
    const update = () => {
      if (this.destroyed || generation !== this.cameraAnimationGeneration) return;
      const progress = Math.min(1, (performance.now() - startedAt) / duration);
      const eased = 1 - (1 - progress) ** 3;
      const scale = start.scale + (target.scale - start.scale) * eased;
      this.world.scale.set(scale);
      this.world.position.set(
        start.x + (target.x - start.x) * eased,
        start.y + (target.y - start.y) * eased,
      );
      this.updateSemanticVisibility();
      this.drawMinimap();
      if (progress < 1) requestAnimationFrame(update);
      else this.drawEdges();
    };
    requestAnimationFrame(update);
  }

  updateSemanticVisibility() {
    if (!this.world) return;
    const zoom = this.world.scale.x;
    const viewport = this.worldViewportBounds(90);
    const largeGraph = this.scene.nodes.length > LARGE_GRAPH_NODE_LIMIT;
    let visibleNodeCount = 0;
    this.nodeViews.forEach((view) => {
      const position = this.positions.get(view.item.id) || point(0, 0);
      const radius = Math.max(view.item.width, view.item.height) / 2;
      const visible = circleIntersectsRect(position, radius, viewport);
      view.container.visible = visible;
      if (!visible) return;
      visibleNodeCount += 1;
      const priority = view.item.id === this.interactionState.selectedId
        || this.interactionState.activeNodeIds.has(view.item.id)
        || (view.item.degree || 0) >= 5;
      const mayCreateText = !largeGraph || priority
        || this.forceStatus === 'settled' || this.forceStatus === 'paused';
      if (!view.title && mayCreateText) this.attachNodeTexts(view);
      if (!view.title) return;
      view.metaText.visible = zoom >= 0.68 && (!largeGraph || zoom >= 0.86 || priority);
      view.title.visible = true;
      const desiredBoost = Math.max(1, MIN_TITLE_SCREEN_SCALE / zoom);
      const widthLimit = view.item.width * 0.82;
      const maxBoost = Math.max(1, widthLimit / Math.max(1, view.titleBaseWidth));
      const boost = Math.min(desiredBoost, maxBoost);
      view.title.scale.set(view.titleBaseScale * boost);
    });
    if (this.host) this.host.dataset.visibleNodeCount = String(visibleNodeCount);
    this.callbacks.onZoomTierChange?.(zoom < 0.52 ? 'compact' : zoom < 0.86 ? 'standard' : 'detail');
  }

  resize() {
    if (!this.app || !this.host) return;
    const width = Math.max(1, this.host.clientWidth);
    const height = Math.max(1, this.host.clientHeight);
    this.app.renderer.resize(width, height);
    this.app.stage.hitArea = new Rectangle(0, 0, width, height);
    this.drawGrid(width, height);
    const selectedPosition = this.positions.get(this.interactionState.selectedId);
    if (selectedPosition) {
      this.cameraAnimationGeneration += 1;
      const zoom = this.world.scale.x;
      this.world.position.set(
        width / 2 - selectedPosition.x * zoom,
        height / 2 - selectedPosition.y * zoom,
      );
    }
    this.drawMinimap();
  }

  drawGrid(width, height) {
    const grid = this.layers.grid.clear();
    for (let x = 0.5; x <= width; x += 24) grid.moveTo(x, 0).lineTo(x, height);
    for (let y = 0.5; y <= height; y += 24) grid.moveTo(0, y).lineTo(width, y);
    grid.stroke({ color: 0xdce2e9, width: 1, alpha: 0.62 });
  }

  drawMinimap(force = false) {
    const canvas = this.minimap;
    if (!canvas || !this.app || !this.scene.nodes.length) return;
    const now = performance.now();
    if (!force && now - this.lastMinimapDrawAt < MINIMAP_FRAME_INTERVAL_MS) return;
    this.lastMinimapDrawAt = now;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, canvas.clientWidth);
    const height = Math.max(1, canvas.clientHeight);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const context = canvas.getContext('2d');
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    context.fillStyle = 'rgba(255,255,255,.96)';
    context.fillRect(0, 0, width, height);
    const bounds = this.graphBounds();
    if (this.host) {
      const zoom = this.world.scale.x;
      const minimumDiameter = this.scene.nodes.reduce(
        (result, item) => Math.min(result, Math.min(item.width, item.height)),
        Infinity,
      );
      const maximumDiameter = this.scene.nodes.reduce(
        (result, item) => Math.max(result, Math.max(item.width, item.height)),
        0,
      );
      this.host.dataset.graphAspect = (bounds.width / Math.max(1, bounds.height)).toFixed(3);
      this.host.dataset.diagonalCorrelation = positionCorrelation([...this.positions.values()]).toFixed(3);
      this.host.dataset.zoom = zoom.toFixed(3);
      this.host.dataset.cameraCenterX = ((this.app.screen.width / 2 - this.world.x) / zoom).toFixed(2);
      this.host.dataset.cameraCenterY = ((this.app.screen.height / 2 - this.world.y) / zoom).toFixed(2);
      this.host.dataset.minNodeScreenDiameter = Number.isFinite(minimumDiameter)
        ? (minimumDiameter * zoom).toFixed(1)
        : '0';
      this.host.dataset.maxNodeScreenDiameter = (maximumDiameter * zoom).toFixed(1);
      this.host.dataset.horizontalNodeOverflowCount = String(this.scene.nodes.filter((item) => {
        const position = this.positions.get(item.id);
        if (!position) return false;
        const radius = Math.min(item.width, item.height) / 2;
        const left = (position.x - radius) * zoom + this.world.x;
        const right = (position.x + radius) * zoom + this.world.x;
        return left < 0 || right > this.app.screen.width;
      }).length);
      const selectedPosition = this.positions.get(this.interactionState.selectedId);
      if (selectedPosition) {
        this.host.dataset.selectedNodeWorldX = selectedPosition.x.toFixed(2);
        this.host.dataset.selectedNodeWorldY = selectedPosition.y.toFixed(2);
      } else {
        delete this.host.dataset.selectedNodeWorldX;
        delete this.host.dataset.selectedNodeWorldY;
      }
    }
    const padding = 9;
    const scale = Math.min(
      (width - padding * 2) / Math.max(1, bounds.width),
      (height - padding * 2) / Math.max(1, bounds.height),
    );
    const toMini = (position) => ({
      x: padding + (position.x - bounds.minX) * scale,
      y: padding + (position.y - bounds.minY) * scale,
    });
    this.scene.nodes.forEach((item) => {
      const position = this.positions.get(item.id);
      if (!position) return;
      const mini = toMini(position);
      context.fillStyle = (item.kind === 'cluster' ? CLUSTER_META : KIND_META[item.kind])?.color || '#9ba7b5';
      context.globalAlpha = item.id === this.interactionState.selectedId ? 1 : 0.72;
      drawMiniNode(context, item, mini, scale);
      if (item.id === this.interactionState.selectedId && item.kind === 'object') {
        objectAttributeLayout(item).forEach((attribute) => {
          context.beginPath();
          context.arc(
            mini.x + attribute.x * scale,
            mini.y + attribute.y * scale,
            Math.max(1.4, ATTRIBUTE_RADIUS * scale),
            0,
            Math.PI * 2,
          );
          context.fill();
        });
      }
    });
    context.globalAlpha = 1;
    const worldTopLeft = this.screenToWorld(0, 0);
    const worldBottomRight = this.screenToWorld(this.app.screen.width, this.app.screen.height);
    const viewportStart = toMini(worldTopLeft);
    const viewportEnd = toMini(worldBottomRight);
    context.strokeStyle = '#286ac7';
    context.lineWidth = 1.5;
    context.strokeRect(
      viewportStart.x,
      viewportStart.y,
      viewportEnd.x - viewportStart.x,
      viewportEnd.y - viewportStart.y,
    );
    canvas.dataset.minX = String(bounds.minX);
    canvas.dataset.minY = String(bounds.minY);
    canvas.dataset.scale = String(scale);
  }

  handleMinimapPointer(event) {
    if (!this.app || !this.minimap) return;
    const bounds = this.minimap.getBoundingClientRect();
    const scale = Number(this.minimap.dataset.scale || 1);
    const minX = Number(this.minimap.dataset.minX || 0);
    const minY = Number(this.minimap.dataset.minY || 0);
    const worldX = minX + (event.clientX - bounds.left - 9) / scale;
    const worldY = minY + (event.clientY - bounds.top - 9) / scale;
    this.world.position.set(
      this.app.screen.width / 2 - worldX * this.world.scale.x,
      this.app.screen.height / 2 - worldY * this.world.scale.y,
    );
    this.drawMinimap();
    this.reportViewportChange();
  }

  graphBounds() {
    if (!this.scene.nodes.length) return { minX: 0, minY: 0, maxX: 1, maxY: 1, width: 1, height: 1, centerX: 0.5, centerY: 0.5 };
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    this.scene.nodes.forEach((item) => {
      const position = this.positions.get(item.id) || point(0, 0);
      const selectedObject = item.id === this.interactionState.selectedId && item.kind === 'object';
      const attributeExtent = selectedObject && item.attributes?.length
        ? objectAttributeOrbitRadius(item) + ATTRIBUTE_RADIUS + 10
        : 0;
      const halfWidth = Math.max(item.width / 2, attributeExtent);
      const halfHeight = Math.max(item.height / 2, attributeExtent);
      minX = Math.min(minX, position.x - halfWidth);
      minY = Math.min(minY, position.y - halfHeight);
      maxX = Math.max(maxX, position.x + halfWidth);
      maxY = Math.max(maxY, position.y + halfHeight);
    });
    return {
      minX,
      minY,
      maxX,
      maxY,
      width: maxX - minX,
      height: maxY - minY,
      centerX: (minX + maxX) / 2,
      centerY: (minY + maxY) / 2,
    };
  }

  screenToWorld(x, y) {
    return point((x - this.world.x) / this.world.scale.x, (y - this.world.y) / this.world.scale.y);
  }

  tick(ticker) {
    const deltaSeconds = ticker.deltaMS / 1000;
    this.tickForceMotion(ticker.deltaMS);
    this.tickAttributeExpansion(ticker.deltaMS);
    this.tickAmbientNodeMotion(ticker.deltaMS);
    if (!this.particles.length) return;
    this.particleClock += deltaSeconds;
    this.particles.forEach((particle) => {
      const progress = (this.particleClock * particle.speed + particle.offset) % 1;
      const position = pointAlongPolyline(particle.points, progress);
      particle.graphics.position.set(position.x, position.y);
      particle.graphics.scale.set(1 / this.world.scale.x);
      particle.graphics.alpha = particle.baseAlpha
        + Math.sin((progress + particle.offset) * Math.PI * 2) * particle.alphaRange;
    });
  }

  isAmbientNodeMotionActive() {
    return this.layoutMode === 'force'
      && this.scene.nodes.length <= LARGE_GRAPH_NODE_LIMIT
      && this.forceStatus === 'settled'
      && !this.forceMotionActive
      && this.interactionState.motionEnabled
      && !this.interactionState.selectedId
      && !this.interactionState.hasHighlight
      && !this.hoveredEdgeId
      && !this.pointerAction;
  }

  captureAmbientNodePositions() {
    this.ambientBasePositions = new Map(
      [...this.positions].map(([id, position]) => [id, { ...position }]),
    );
    this.ambientNodeClock = 0;
    this.ambientNodeFrameElapsed = 0;
    this.ambientNodeMotionApplied = false;
    if (this.host) this.host.dataset.capturedSettledAspect = this.graphBoundsAspect();
  }

  graphBoundsAspect() {
    const bounds = this.graphBounds();
    return (bounds.width / Math.max(1, bounds.height)).toFixed(3);
  }

  restoreAmbientNodePositions(redraw) {
    if (!this.ambientNodeMotionApplied || !this.ambientBasePositions) return;
    this.positions = new Map(
      [...this.ambientBasePositions].map(([id, position]) => [id, { ...position }]),
    );
    this.nodeViews.forEach((view, id) => {
      const position = this.positions.get(id);
      if (position) view.container.position.set(position.x, position.y);
    });
    this.ambientNodeMotionApplied = false;
    this.ambientNodeFrameElapsed = 0;
    if (redraw) {
      this.drawEdges();
      this.drawMinimap();
    }
  }

  tickAmbientNodeMotion(deltaMS) {
    if (!this.isAmbientNodeMotionActive() || !this.ambientBasePositions) {
      this.restoreAmbientNodePositions(true);
      return;
    }
    this.ambientNodeFrameElapsed += deltaMS;
    if (this.ambientNodeFrameElapsed < AMBIENT_FRAME_INTERVAL_MS) return;
    this.ambientNodeClock += this.ambientNodeFrameElapsed / 1000;
    this.ambientNodeFrameElapsed = 0;
    const zoom = this.world.scale.x;

    this.ambientBasePositions.forEach((base, id) => {
      if (this.pinnedNodeIds.has(id)) {
        this.positions.set(id, { ...base });
        return;
      }
      const offset = ambientNodeOffset(
        id,
        this.ambientComponentKeys.get(id) || id,
        this.ambientNodeClock,
        zoom,
      );
      this.positions.set(id, point(
        base.x + offset.x,
        base.y + offset.y,
      ));
    });
    this.ambientNodeMotionApplied = true;
    this.nodeViews.forEach((view, id) => {
      const position = this.positions.get(id);
      if (position) view.container.position.set(position.x, position.y);
    });
    this.drawEdges();
    this.drawMinimap();
  }

  tickForceMotion(deltaMS) {
    if (!this.forceMotionActive || !this.forceTargetPositions.size) return;
    this.forceMotionFrameElapsed += deltaMS;
    if (this.forceMotionFrameElapsed < FORCE_POSITION_FRAME_MS) return;
    const elapsedMS = this.forceMotionFrameElapsed;
    this.forceMotionFrameElapsed = 0;
    let maximumDistance = 0;
    this.forceTargetPositions.forEach((target, id) => {
      if (this.pointerAction?.type === 'node' && this.pointerAction.nodeId === id) return;
      const current = this.positions.get(id) || target;
      const distance = Math.hypot(target.x - current.x, target.y - current.y);
      maximumDistance = Math.max(maximumDistance, distance);
      this.positions.set(id, distance < 0.12
        ? { ...target }
        : smoothGraphPosition(current, target, elapsedMS));
    });
    this.applyPositions();
    if (this.host) {
      this.host.dataset.forceSmoothingMs = String(FORCE_SMOOTHING_MS);
      this.host.dataset.forceInterpolationDistance = maximumDistance.toFixed(2);
    }
    if (!this.forceSettlePending || maximumDistance >= 0.35) return;
    this.forceTargetPositions.forEach((target, id) => this.positions.set(id, { ...target }));
    this.forceTargetPositions.clear();
    this.forceMotionActive = false;
    this.forceSettlePending = false;
    this.forceMotionFrameElapsed = 0;
    this.applyPositions();
    if (this.host) this.host.dataset.receivedSettledAspect = this.graphBoundsAspect();
    this.captureAmbientNodePositions();
    if (!this.forceFitPending) return;
    this.forceFitPending = false;
    if (this.interactionState.selectedId) {
      this.fitView(false);
      this.selectionCameraSnapshot = null;
      this.selectionFocusActive = false;
      this.focusNode(this.interactionState.selectedId);
    } else {
      this.fitView(true);
    }
  }

  destroy() {
    this.destroyed = true;
    this.scenePresentationGeneration += 1;
    window.clearTimeout(this.viewportReportTimer);
    this.stopForce(false);
    this.resizeObserver?.disconnect();
    this.minimap?.removeEventListener('pointerdown', this.handleMinimapPointer);
    this.nodeTooltip?.remove();
    this.nodeTooltip = null;
    if (this.app) {
      this.app.canvas.removeEventListener('webglcontextlost', this.handleContextLost);
      this.app.canvas.removeEventListener('wheel', this.handleWheel);
      this.app.ticker.remove(this.tick);
      this.app.destroy(true, { children: true });
      this.app = null;
    }
  }
}

function emptyInteractionState() {
  return {
    selectedId: '',
    activeNodeIds: new Set(),
    activeEdgeIds: new Set(),
    hasHighlight: false,
    motionEnabled: true,
    reducedMotion: false,
  };
}

function drawNodeShape(graphics, item, color, fill, selected, highlighted, pinned) {
  const selectionColor = selected ? color : highlighted ? 0x7ca7dc : color;
  traceNodeShape(graphics, item).fill({ color: fill });
  if (selected) traceNodeShape(graphics, item).fill({ color: selectionColor, alpha: 0.12 });
  else if (highlighted) traceNodeShape(graphics, item).fill({ color: selectionColor, alpha: 0.08 });
  if (item.kind === 'script') {
    const fold = Math.min(22, item.width * 0.18);
    graphics.moveTo(item.width / 2 - fold, -item.height / 2)
      .lineTo(item.width / 2 - fold, -item.height / 2 + fold)
      .lineTo(item.width / 2, -item.height / 2 + fold)
      .stroke({ color: selectionColor, width: 1.5, alpha: 0.72 });
  }
  if (pinned) graphics.circle(item.width * 0.34, -item.height * 0.34, 4).fill({ color });
}

function traceNodeShape(graphics, item, padding = 0) {
  if (item.kind === 'object' || item.kind === 'cluster') {
    return graphics.circle(0, 0, Math.min(item.width, item.height) / 2 + padding);
  }
  if (item.kind === 'operation') {
    return graphics.roundRect(
      -item.width / 2 - padding,
      -item.height / 2 - padding,
      item.width + padding * 2,
      item.height + padding * 2,
      19 + padding,
    );
  }
  if (item.kind === 'metric') {
    return graphics.roundRect(
      -item.width / 2 - padding,
      -item.height / 2 - padding,
      item.width + padding * 2,
      item.height + padding * 2,
      item.height / 2 + padding,
    );
  }
  return graphics.poly(nodePolygonPoints(item, padding));
}

function nodePolygonPoints(item, padding = 0) {
  const halfWidth = item.width / 2 + padding;
  const halfHeight = item.height / 2 + padding;
  if (item.kind === 'rule') {
    const inset = halfWidth * 0.22;
    return [
      -halfWidth + inset, -halfHeight,
      halfWidth - inset, -halfHeight,
      halfWidth, 0,
      halfWidth - inset, halfHeight,
      -halfWidth + inset, halfHeight,
      -halfWidth, 0,
    ];
  }
  if (item.kind === 'metric') return capsulePolygonPoints(halfWidth, halfHeight);
  if (item.kind === 'script') {
    const fold = Math.min(22 + padding, halfWidth * 0.32);
    return [
      -halfWidth, -halfHeight,
      halfWidth - fold, -halfHeight,
      halfWidth, -halfHeight + fold,
      halfWidth, halfHeight,
      -halfWidth, halfHeight,
    ];
  }
  return [
    -halfWidth, -halfHeight,
    halfWidth, -halfHeight,
    halfWidth, halfHeight,
    -halfWidth, halfHeight,
  ];
}

function nodeHitArea(item) {
  if (item.kind === 'object' || item.kind === 'cluster') return new Circle(0, 0, Math.min(item.width, item.height) / 2);
  if (item.kind === 'operation') return new Rectangle(-item.width / 2, -item.height / 2, item.width, item.height);
  if (item.kind === 'metric') {
    return new RoundedRectangle(-item.width / 2, -item.height / 2, item.width, item.height, item.height / 2);
  }
  return new Polygon(nodePolygonPoints(item));
}

function capsulePolygonPoints(halfWidth, halfHeight) {
  const centerOffset = Math.max(0, halfWidth - halfHeight);
  const points = [];
  const segments = 8;
  for (let index = 0; index <= segments; index += 1) {
    const angle = -Math.PI / 2 + Math.PI * index / segments;
    points.push(centerOffset + Math.cos(angle) * halfHeight, Math.sin(angle) * halfHeight);
  }
  for (let index = 0; index <= segments; index += 1) {
    const angle = Math.PI / 2 + Math.PI * index / segments;
    points.push(-centerOffset + Math.cos(angle) * halfHeight, Math.sin(angle) * halfHeight);
  }
  return points;
}

function createObjectAttributeOrbit(item, color, fill, tooltip, initialProgress = 0) {
  const container = new Container();
  container.eventMode = 'passive';
  const nodeRadius = Math.min(item.width, item.height) / 2;
  const items = objectAttributeLayout(item).map((attribute) => {
    const connector = new Graphics();
    const node = new Container();
    node.eventMode = 'static';
    node.cursor = 'help';
    node.hitArea = new Circle(0, 0, ATTRIBUTE_RADIUS);
    node.alpha = 0;
    const circle = new Graphics().circle(0, 0, ATTRIBUTE_RADIUS).fill({ color: fill, alpha: 0.98 });
    const labelText = attribute.field.cnName || attribute.field.label || attribute.field.key;
    const label = createText({ text: labelText, size: 10, color, weight: '650', maxWidth: ATTRIBUTE_RADIUS * 1.65 });
    label.anchor.set(0.5);
    node.on('pointerdown', (event) => event.stopPropagation());
    if (label.studioTruncated) {
      node.on('pointerenter', (event) => tooltip.show(label.studioFullText, event));
      node.on('pointermove', (event) => tooltip.move(event));
      node.on('pointerleave', () => tooltip.hide());
    }
    node.addChild(circle, label);
    container.addChild(connector, node);
    return {
      ...attribute,
      connector,
      node,
      truncated: label.studioTruncated,
      startRadius: nodeRadius + 10,
      targetRadius: attribute.radius,
    };
  });
  const orbit = {
    container,
    items,
    nodeRadius,
    color,
    progress: Math.max(0, Math.min(1, initialProgress)),
    truncatedLabelCount: items.filter((item) => item.truncated).length,
  };
  applyAttributeOrbitProgress(orbit);
  return orbit;
}

function applyAttributeOrbitProgress(orbit) {
  const eased = 1 - (1 - orbit.progress) ** 3;
  orbit.items.forEach((attribute) => {
    const radius = attribute.startRadius
      + (attribute.targetRadius - attribute.startRadius) * eased;
    const x = Math.cos(attribute.angle) * radius;
    const y = Math.sin(attribute.angle) * radius;
    attribute.node.position.set(x, y);
    attribute.node.scale.set(0.72 + eased * 0.28);
    attribute.node.alpha = eased;
    drawAttributeConnector(attribute.connector, attribute.angle, orbit.nodeRadius, radius, orbit.color, eased);
  });
}

function objectAttributeLayout(item) {
  const fields = Array.isArray(item.attributes)
    ? [...item.attributes].sort((left, right) => attributeStableKey(left).localeCompare(attributeStableKey(right)))
    : [];
  const radius = objectAttributeOrbitRadius(item);
  return fields.map((field, index) => {
    const angle = -Math.PI / 2 + (Math.PI * 2 * index) / Math.max(1, fields.length);
    return {
      field,
      angle,
      radius,
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
    };
  });
}

function attributeStableKey(field) {
  return String(field?.key || field?.name || field?.cnName || field?.label || '');
}

function objectAttributeOrbitRadius(item) {
  const count = Array.isArray(item.attributes) ? item.attributes.length : 0;
  const nodeRadius = Math.min(item.width, item.height) / 2;
  const circumferenceRadius = count
    ? count * (ATTRIBUTE_RADIUS * 2 + ATTRIBUTE_GAP) / (Math.PI * 2)
    : 0;
  return Math.max(nodeRadius + 62, circumferenceRadius);
}

function drawAttributeConnector(graphics, angle, nodeRadius, attributeRadius, color, progress) {
  graphics.clear();
  if (progress <= 0) return;
  const start = nodeRadius + 4;
  const end = Math.max(start, attributeRadius - ATTRIBUTE_RADIUS - 4);
  graphics.moveTo(Math.cos(angle) * start, Math.sin(angle) * start)
    .lineTo(Math.cos(angle) * end, Math.sin(angle) * end)
    .stroke({ color, width: 1.4, alpha: 0.2 + progress * 0.42 });
}

function drawMiniNode(context, item, center, scale) {
  const width = Math.max(5, item.width * scale);
  const height = Math.max(5, item.height * scale);
  context.save();
  context.translate(center.x, center.y);
  context.beginPath();
  if (item.kind === 'object' || item.kind === 'cluster') {
    context.arc(0, 0, Math.max(2.5, Math.min(width, height) / 2), 0, Math.PI * 2);
  } else if (item.kind === 'operation' || item.kind === 'metric') {
    const radius = item.kind === 'metric' ? height / 2 : Math.min(4, height * 0.18);
    context.roundRect(-width / 2, -height / 2, width, height, radius);
  } else {
    const points = nodePolygonPoints({ ...item, width, height });
    context.moveTo(points[0], points[1]);
    for (let index = 2; index < points.length; index += 2) context.lineTo(points[index], points[index + 1]);
    context.closePath();
  }
  context.fill();
  context.restore();
}

function createNodeTexts(item, meta, color, relationCount) {
  const metaText = createText({
    text: `${meta.label} · ${relationCount}条关系`,
    size: 10,
    color,
    weight: '600',
  });
  metaText.anchor.set(0.5);
  const title = createText({
    text: item.label,
    size: 15,
    color: 0x273341,
    weight: '600',
    maxWidth: item.width * 0.72,
    wrap: true,
  });
  title.anchor.set(0.5);
  const textGap = 4;
  const textBlockHeight = metaText.height + textGap + title.height;
  metaText.position.set(0, -textBlockHeight / 2 + metaText.height / 2);
  title.position.set(0, -textBlockHeight / 2 + metaText.height + textGap + title.height / 2);
  return { metaText, title };
}

function createEdgeLabelView(text, color, emphasized) {
  const label = createText({ text, size: 10, color, weight: emphasized ? '600' : '500' });
  label.anchor.set(0.5);
  const background = new Graphics()
    .roundRect(-label.width / 2 - 8, -label.height / 2 - 4, label.width + 16, label.height + 8, 4)
    .fill({ color: 0xffffff, alpha: 1 });
  const container = new Container();
  container.addChild(background, label);
  return { signature: `${text}|${color}|${Number(emphasized)}`, container, label };
}

function createText({
  text,
  size,
  color,
  weight = '400',
  family = 'Inter, Noto Sans SC, Microsoft YaHei, sans-serif',
  maxWidth,
  wrap = false,
}) {
  const fullText = String(text ?? '');
  const value = new Text({
    text: fullText,
    style: {
      fontFamily: family,
      fontSize: size,
      fontWeight: weight,
      fill: color,
      align: 'center',
      ...(wrap && maxWidth ? {
        wordWrap: true,
        wordWrapWidth: maxWidth,
        breakWords: true,
        lineHeight: Math.ceil(size * 1.2),
      } : {}),
    },
    resolution: Math.min(window.devicePixelRatio || 1, 2),
  });
  value.studioFullText = fullText;
  value.studioTruncated = false;
  if (!wrap && maxWidth && value.width > maxWidth) {
    value.text = ellipsizeText(value, fullText, maxWidth);
    value.studioTruncated = value.text !== fullText;
  }
  return value;
}

function ellipsizeText(value, fullText, maxWidth) {
  const characters = Array.from(fullText);
  let low = 0;
  let high = characters.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    value.text = `${characters.slice(0, middle).join('')}…`;
    if (value.width <= maxWidth) low = middle;
    else high = middle - 1;
  }
  const prefix = characters.slice(0, low).join('');
  return low < characters.length ? `${prefix}…` : fullText;
}

function edgeStyle(kind, sourceKind) {
  if (['uses_rule', 'applies_to_object'].includes(kind)) return EDGE_PALETTE.rule;
  if (['uses_metric', 'metric_uses_object'].includes(kind) || (kind === 'uses_object' && sourceKind === 'metric')) return EDGE_PALETTE.metric;
  if (['uses_script', 'script_bound_operation', 'script_uses_object'].includes(kind)) return EDGE_PALETTE.script;
  if (kind === 'object_relation') return EDGE_PALETTE.object;
  return EDGE_PALETTE.operation;
}

function drawEdgeLine(graphics, points, style) {
  if (!style.dash) {
    graphics.moveTo(points[0].x, points[0].y);
    points.slice(1).forEach((item) => graphics.lineTo(item.x, item.y));
    graphics.stroke({ color: style.color, width: style.width, alpha: style.alpha, cap: 'round', join: 'round' });
    return;
  }
  const [dash, gap] = style.dash;
  for (let index = 0; index < points.length - 1; index += 1) {
    drawDashedSegment(graphics, points[index], points[index + 1], dash, gap, style);
  }
}

function drawDashedSegment(graphics, start, end, dash, gap, style) {
  const length = Math.hypot(end.x - start.x, end.y - start.y);
  if (!length) return;
  const dx = (end.x - start.x) / length;
  const dy = (end.y - start.y) / length;
  for (let cursor = 0; cursor < length; cursor += dash + gap) {
    const to = Math.min(length, cursor + dash);
    graphics.moveTo(start.x + dx * cursor, start.y + dy * cursor)
      .lineTo(start.x + dx * to, start.y + dy * to);
  }
  graphics.stroke({ color: style.color, width: style.width, alpha: style.alpha, cap: 'round' });
}

function drawArrow(graphics, points, color, alpha) {
  const end = points[points.length - 1];
  let previous = points[points.length - 2];
  for (let index = points.length - 2; index >= 0; index -= 1) {
    if (Math.hypot(end.x - points[index].x, end.y - points[index].y) > 4) {
      previous = points[index];
      break;
    }
  }
  const angle = Math.atan2(end.y - previous.y, end.x - previous.x);
  const length = 10;
  const spread = 5;
  graphics.poly([
    end.x, end.y,
    end.x - Math.cos(angle) * length + Math.sin(angle) * spread,
    end.y - Math.sin(angle) * length - Math.cos(angle) * spread,
    end.x - Math.cos(angle) * length - Math.sin(angle) * spread,
    end.y - Math.sin(angle) * length + Math.cos(angle) * spread,
  ]).fill({ color, alpha });
}

function uniqueForceLinks(edges) {
  const seen = new Set();
  const result = [];
  edges.forEach((item) => {
    const key = [item.source, item.target].sort().join(':');
    if (seen.has(key)) return;
    seen.add(key);
    result.push({ source: item.source, target: item.target, kind: item.kind });
  });
  return result;
}

function ambientComponentKeys(nodes, edges) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const adjacency = new Map(nodes.map((node) => [node.id, new Set()]));
  edges.forEach((edge) => {
    if (!adjacency.has(edge.source) || !adjacency.has(edge.target)) return;
    adjacency.get(edge.source).add(edge.target);
    adjacency.get(edge.target).add(edge.source);
  });
  const result = new Map();
  const visited = new Set();
  [...nodeById.keys()].sort().forEach((startId) => {
    if (visited.has(startId)) return;
    if (!adjacency.get(startId)?.size) {
      visited.add(startId);
      result.set(startId, `isolated:${nodeById.get(startId)?.kind || 'node'}`);
      return;
    }
    const ids = [];
    const queue = [startId];
    visited.add(startId);
    while (queue.length) {
      const id = queue.shift();
      ids.push(id);
      [...(adjacency.get(id) || [])].sort().forEach((nextId) => {
        if (visited.has(nextId)) return;
        visited.add(nextId);
        queue.push(nextId);
      });
    }
    const componentKey = `component:${ids.sort()[0]}`;
    ids.forEach((id) => result.set(id, componentKey));
  });
  return result;
}

function rendererName(app) {
  const gl = app.renderer.context?.gl;
  if (typeof WebGL2RenderingContext !== 'undefined' && gl instanceof WebGL2RenderingContext) return 'WebGL 2';
  return 'WebGL';
}

function shapeAnchor(center, node, toward) {
  const dx = toward.x - center.x;
  const dy = toward.y - center.y;
  if (node.kind === 'object') {
    const length = Math.max(1e-6, Math.hypot(dx, dy));
    const radius = Math.min(node.width, node.height) / 2;
    return point(center.x + dx / length * radius, center.y + dy / length * radius);
  }
  const polygon = nodePolygonPoints(node);
  let nearest = Infinity;
  for (let index = 0; index < polygon.length; index += 2) {
    const next = (index + 2) % polygon.length;
    const start = point(polygon[index], polygon[index + 1]);
    const end = point(polygon[next], polygon[next + 1]);
    const segmentX = end.x - start.x;
    const segmentY = end.y - start.y;
    const denominator = dx * segmentY - dy * segmentX;
    if (Math.abs(denominator) < 1e-8) continue;
    const rayDistance = (start.x * segmentY - start.y * segmentX) / denominator;
    const segmentDistance = (start.x * dy - start.y * dx) / denominator;
    if (rayDistance >= 0 && segmentDistance >= 0 && segmentDistance <= 1) {
      nearest = Math.min(nearest, rayDistance);
    }
  }
  if (!Number.isFinite(nearest)) return { ...center };
  return point(center.x + dx * nearest, center.y + dy * nearest);
}

function quadraticPoints(start, end, offset, steps) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const length = Math.max(1, Math.hypot(dx, dy));
  const control = point((start.x + end.x) / 2 - dy / length * offset, (start.y + end.y) / 2 + dx / length * offset);
  return Array.from({ length: steps + 1 }, (_, index) => {
    const t = index / steps;
    const inverse = 1 - t;
    return point(
      inverse * inverse * start.x + 2 * inverse * t * control.x + t * t * end.x,
      inverse * inverse * start.y + 2 * inverse * t * control.y + t * t * end.y,
    );
  });
}

function findLabelPlacement({ points, width, height, placed, nodeObstacles }) {
  const fractions = [0.5, 0.4, 0.6, 0.3, 0.7, 0.22, 0.78, 0.14, 0.86];
  let best = null;

  for (const fraction of fractions) {
    const result = labelCandidateForTrack(points, fraction, 0, width, height, placed, nodeObstacles);
    if (!result.blocked) return result;
    if (!best || result.score < best.score) best = result;
  }
  return best || {
    x: points[0]?.x || 0,
    y: points[0]?.y || 0,
    rect: centeredRect(points[0] || point(0, 0), width, height),
    fraction: 0,
    offset: 0,
    blocked: false,
    score: 0,
  };
}

function labelCandidateForTrack(points, fraction, offset, width, height, placed, nodeObstacles) {
  const pathPoint = pointAndNormalAlongPolyline(points, fraction);
  const candidate = point(
    pathPoint.x + pathPoint.normalX * offset,
    pathPoint.y + pathPoint.normalY * offset,
  );
  const rect = centeredRect(candidate, width, height);
  const labelOverlap = placed.reduce(
    (total, item) => total + rectOverlapArea(rect, item.rect, LABEL_GAP),
    0,
  );
  const nodeOverlap = nodeObstacles.reduce(
    (total, item) => total + Number(rectIntersectsCircle(rect, item.center, item.radius)),
    0,
  );
  return {
    ...candidate,
    rect,
    fraction,
    offset,
    blocked: Boolean(labelOverlap || nodeOverlap),
    score: labelOverlap * 12 + nodeOverlap * width * height * 2
      + Math.abs(fraction - 0.5) * 18 + Math.abs(offset) * 0.04,
  };
}

function pointAndNormalAlongPolyline(points, fraction) {
  const segments = [];
  let total = 0;
  for (let index = 0; index < points.length - 1; index += 1) {
    const start = points[index];
    const end = points[index + 1];
    const length = Math.hypot(end.x - start.x, end.y - start.y);
    if (!length) continue;
    segments.push({ start, end, length });
    total += length;
  }
  if (!segments.length) return { ...(points[0] || point(0, 0)), normalX: 0, normalY: -1 };
  let remaining = total * fraction;
  for (const segment of segments) {
    if (remaining <= segment.length) {
      const t = remaining / segment.length;
      const dx = segment.end.x - segment.start.x;
      const dy = segment.end.y - segment.start.y;
      return {
        x: segment.start.x + dx * t,
        y: segment.start.y + dy * t,
        normalX: -dy / segment.length,
        normalY: dx / segment.length,
      };
    }
    remaining -= segment.length;
  }
  const last = segments[segments.length - 1];
  return {
    ...last.end,
    normalX: -(last.end.y - last.start.y) / last.length,
    normalY: (last.end.x - last.start.x) / last.length,
  };
}

function centeredRect(center, width, height) {
  return {
    left: center.x - width / 2,
    right: center.x + width / 2,
    top: center.y - height / 2,
    bottom: center.y + height / 2,
  };
}

function circleIntersectsRect(center, radius, rect) {
  return center.x + radius >= rect.left
    && center.x - radius <= rect.right
    && center.y + radius >= rect.top
    && center.y - radius <= rect.bottom;
}

function polylineIntersectsRect(points, rect) {
  for (const current of points) {
    if (current.x >= rect.left && current.x <= rect.right
      && current.y >= rect.top && current.y <= rect.bottom) return true;
  }
  for (let index = 1; index < points.length; index += 1) {
    const start = points[index - 1];
    const end = points[index];
    if (segmentIntersectsRect(start, end, rect)) return true;
  }
  return false;
}

function segmentIntersectsRect(start, end, rect) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const p = [-dx, dx, -dy, dy];
  const q = [start.x - rect.left, rect.right - start.x, start.y - rect.top, rect.bottom - start.y];
  let minimum = 0;
  let maximum = 1;
  for (let index = 0; index < 4; index += 1) {
    if (p[index] === 0) {
      if (q[index] < 0) return false;
      continue;
    }
    const ratio = q[index] / p[index];
    if (p[index] < 0) minimum = Math.max(minimum, ratio);
    else maximum = Math.min(maximum, ratio);
    if (minimum > maximum) return false;
  }
  return true;
}

function rectOverlapArea(left, right, gap = 0) {
  const width = Math.min(left.right, right.right) - Math.max(left.left, right.left);
  const height = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top);
  if (width <= -gap || height <= -gap) return 0;
  return (Math.max(0, width + gap) || 1) * (Math.max(0, height + gap) || 1);
}

function rectIntersectsCircle(rect, center, radius) {
  const nearestX = Math.max(rect.left, Math.min(center.x, rect.right));
  const nearestY = Math.max(rect.top, Math.min(center.y, rect.bottom));
  return Math.hypot(nearestX - center.x, nearestY - center.y) < radius;
}

function countLabelCollisions(placed, nodeObstacles) {
  let count = 0;
  for (let leftIndex = 0; leftIndex < placed.length; leftIndex += 1) {
    const rect = placed[leftIndex].rect;
    if (nodeObstacles.some((item) => rectIntersectsCircle(rect, item.center, item.radius))) count += 1;
    for (let rightIndex = leftIndex + 1; rightIndex < placed.length; rightIndex += 1) {
      if (rectOverlapArea(rect, placed[rightIndex].rect, 1)) count += 1;
    }
  }
  return count;
}

function pointToPolylineDistance(target, points) {
  let minimum = Infinity;
  for (let index = 0; index < points.length - 1; index += 1) {
    minimum = Math.min(minimum, pointToSegmentDistance(target, points[index], points[index + 1]));
  }
  return minimum;
}

function pointToSegmentDistance(target, start, end) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const denominator = dx * dx + dy * dy;
  const t = denominator ? Math.max(0, Math.min(1, ((target.x - start.x) * dx + (target.y - start.y) * dy) / denominator)) : 0;
  return Math.hypot(target.x - (start.x + dx * t), target.y - (start.y + dy * t));
}

function pointAlongPolyline(points, progress) {
  const lengths = [];
  let total = 0;
  for (let index = 0; index < points.length - 1; index += 1) {
    const length = Math.hypot(points[index + 1].x - points[index].x, points[index + 1].y - points[index].y);
    lengths.push(length);
    total += length;
  }
  let remaining = total * progress;
  for (let index = 0; index < lengths.length; index += 1) {
    if (remaining <= lengths[index]) {
      const t = lengths[index] ? remaining / lengths[index] : 0;
      return point(
        points[index].x + (points[index + 1].x - points[index].x) * t,
        points[index].y + (points[index + 1].y - points[index].y) * t,
      );
    }
    remaining -= lengths[index];
  }
  return points[points.length - 1] || point(0, 0);
}

function stableParticleOffset(value) {
  return Math.abs(stableHash(value) % 1000) / 1000;
}

function stableHash(value) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) hash = ((hash << 5) - hash + value.charCodeAt(index)) | 0;
  return hash;
}

function positionCorrelation(positions) {
  if (positions.length < 3) return 0;
  const meanX = positions.reduce((total, item) => total + item.x, 0) / positions.length;
  const meanY = positions.reduce((total, item) => total + item.y, 0) / positions.length;
  let covariance = 0;
  let varianceX = 0;
  let varianceY = 0;
  positions.forEach((item) => {
    const dx = item.x - meanX;
    const dy = item.y - meanY;
    covariance += dx * dy;
    varianceX += dx * dx;
    varianceY += dy * dy;
  });
  const denominator = Math.sqrt(varianceX * varianceY);
  return denominator ? covariance / denominator : 0;
}

function cssColorToNumber(value) {
  return Number.parseInt(value.replace('#', ''), 16);
}

function point(x, y) {
  return { x, y };
}

function sceneSignature(scene) {
  const nodeIds = scene.nodes.map((item) => item.id).sort().join('|');
  const edgeIds = scene.edges.map((item) => item.id).sort().join('|');
  return `${nodeIds}::${edgeIds}`;
}
