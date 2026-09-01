import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
} from 'd3-force';

const POSITION_INTERVAL_MS = 32;
const MAX_TICKS = 800;
const MAX_WALL_TIME_MS = 3000;

let simulation = null;
let simulationNodes = [];
let generation = 0;
let tickCount = 0;
let startedAt = 0;
let lastPostedAt = 0;
let paused = false;
let targetAspectGoal = 1.6;
let simulationStatus = 'idle';

if (typeof self !== 'undefined') self.onmessage = ({ data }) => {
  switch (data.type) {
    case 'init': initialize(data); break;
    case 'pause': pause(); break;
    case 'resume': resume(); break;
    case 'reheat': reheat(data.alpha ?? 0.72); break;
    case 'drag': pin(data.nodeId, data.x, data.y, true); break;
    case 'pin': pin(data.nodeId, data.x, data.y, false); break;
    case 'unpin': unpin(data.nodeId); break;
    case 'dispose': dispose(); break;
    default: break;
  }
};

function initialize(data) {
  dispose();
  generation = data.generation;
  tickCount = 0;
  startedAt = performance.now();
  lastPostedAt = 0;
  paused = false;
  simulationStatus = 'running';

  const degreeById = new Map(data.nodes.map((item) => [item.id, 0]));
  data.links.forEach((item) => {
    degreeById.set(item.source, (degreeById.get(item.source) || 0) + 1);
    degreeById.set(item.target, (degreeById.get(item.target) || 0) + 1);
  });
  simulationNodes = [...data.nodes]
    .sort((left, right) => left.id.localeCompare(right.id))
    .map((item) => ({
      ...item,
      x: item.x,
      y: item.y,
      anchorX: item.x,
      anchorY: item.y,
      degree: degreeById.get(item.id) || 0,
    }));
  const viewportRatio = Math.max(0.4, (data.viewport?.width || 1) / Math.max(1, data.viewport?.height || 1));
  const targetAspect = Math.min(2.2, Math.max(1.35, viewportRatio * 1.08));
  targetAspectGoal = targetAspect;
  applyHorizontalPacking(simulationNodes, data.links, targetAspect);

  simulation = forceSimulation(simulationNodes)
    .randomSource(seededRandom(data.seed || 'studio-force-guide-v1'))
    .alpha(0.9)
    .alphaMin(0.02)
    .alphaDecay(0.035)
    .velocityDecay(0.42)
    .force('link', forceLink(data.links.map((item) => ({ ...item })))
      .id((item) => item.id)
      .distance((item) => linkDistance(item.kind))
      .strength((item) => linkStrength(item.kind)))
    .force('charge', forceManyBody()
      .strength((item) => -Math.min(520, Math.max(180, 160 + 28 * Math.sqrt(item.degree + 1))))
      .distanceMax(720)
      .theta(0.82))
    .force('collision', forceCollide()
      .radius((item) => Math.max(item.width, item.height) / 2 + 20)
      .strength(0.92)
      .iterations(2))
    .force('horizontal-anchor', forceX((item) => item.anchorX).strength(0.08))
    .force('vertical-pack', forceY((item) => item.anchorY).strength(0.22))
    .force('aspect', horizontalAspectForce(targetAspect))
    .on('tick', publishTick)
    .on('end', publishSettled);

  postMessage({ type: 'status', generation, status: 'running' });
}

function publishTick() {
  if (!simulation || paused) return;
  tickCount += 1;
  const now = performance.now();
  if (now - lastPostedAt >= POSITION_INTERVAL_MS) {
    publishPositions('positions');
    lastPostedAt = now;
  }
  if (tickCount >= MAX_TICKS || now - startedAt >= MAX_WALL_TIME_MS) {
    simulation.stop();
    publishSettled();
  }
}

function publishSettled() {
  if (!simulation) return;
  simulationStatus = 'settled';
  enforceHorizontalAspect(simulationNodes, targetAspectGoal);
  publishPositions('settled');
  const bounds = nodeBounds(simulationNodes);
  postMessage({
    type: 'status',
    generation,
    status: 'settled',
    aspect: bounds.width / Math.max(1, bounds.height),
    targetAspect: targetAspectGoal,
  });
}

function publishPositions(type) {
  const positions = new Float32Array(simulationNodes.length * 2);
  simulationNodes.forEach((item, index) => {
    positions[index * 2] = item.x;
    positions[index * 2 + 1] = item.y;
  });
  postMessage({
    type,
    generation,
    tick: tickCount,
    alpha: simulation?.alpha() || 0,
    nodeIds: simulationNodes.map((item) => item.id),
    positions,
  }, [positions.buffer]);
}

function pause() {
  if (!simulation) return;
  paused = true;
  simulationStatus = 'paused';
  simulation.stop();
  publishPositions('positions');
  postMessage({ type: 'status', generation, status: 'paused' });
}

function resume() {
  if (!simulation) return;
  paused = false;
  simulationStatus = 'running';
  startedAt = performance.now();
  simulation.alpha(Math.max(0.28, simulation.alpha())).restart();
  postMessage({ type: 'status', generation, status: 'running' });
}

function reheat(alpha) {
  if (!simulation) return;
  paused = false;
  simulationStatus = 'running';
  tickCount = 0;
  startedAt = performance.now();
  simulation.alpha(Math.max(0.25, Math.min(1, alpha))).restart();
  postMessage({ type: 'status', generation, status: 'running' });
}

function pin(nodeId, x, y, dragging) {
  const node = simulationNodes.find((item) => item.id === nodeId);
  if (!node || !simulation) return;
  node.fx = x;
  node.fy = y;
  node.x = x;
  node.y = y;
  if (dragging && simulationStatus === 'running') {
    startedAt = performance.now();
    simulation.alphaTarget(0.12).alpha(Math.max(0.24, simulation.alpha())).restart();
    postMessage({ type: 'status', generation, status: 'running' });
  } else if (dragging) {
    return;
  } else if (simulationStatus === 'paused') {
    simulation.alphaTarget(0);
    publishPositions('positions');
    postMessage({ type: 'status', generation, status: 'paused' });
  } else if (simulationStatus === 'settled') {
    simulation.alphaTarget(0);
    publishPositions('settled');
    const bounds = nodeBounds(simulationNodes);
    postMessage({
      type: 'status',
      generation,
      status: 'settled',
      aspect: bounds.width / Math.max(1, bounds.height),
      targetAspect: targetAspectGoal,
    });
  } else {
    startedAt = performance.now();
    simulation.alphaTarget(0).alpha(Math.max(0.24, simulation.alpha())).restart();
    postMessage({ type: 'status', generation, status: 'running' });
  }
}

function unpin(nodeId) {
  const node = simulationNodes.find((item) => item.id === nodeId);
  if (!node || !simulation) return;
  node.fx = null;
  node.fy = null;
  if (simulationStatus === 'paused') {
    publishPositions('positions');
    postMessage({ type: 'status', generation, status: 'paused' });
    return;
  }
  startedAt = performance.now();
  simulationStatus = 'running';
  simulation.alphaTarget(0).alpha(Math.max(0.22, simulation.alpha())).restart();
  postMessage({ type: 'status', generation, status: 'running' });
}

function dispose() {
  simulation?.stop();
  simulation = null;
  simulationNodes = [];
  simulationStatus = 'idle';
}

export function applyHorizontalPacking(nodes, links, targetAspect) {
  if (!nodes.length) return;
  const graphCenter = nodeBounds(nodes);
  const nodeById = new Map(nodes.map((item) => [item.id, item]));
  const adjacency = new Map(nodes.map((item) => [item.id, new Set()]));
  links.forEach((item) => {
    const sourceId = typeof item.source === 'object' ? item.source.id : item.source;
    const targetId = typeof item.target === 'object' ? item.target.id : item.target;
    if (!adjacency.has(sourceId) || !adjacency.has(targetId)) return;
    adjacency.get(sourceId).add(targetId);
    adjacency.get(targetId).add(sourceId);
  });

  const components = [];
  const visited = new Set();
  [...nodeById.keys()].sort().forEach((startId) => {
    if (visited.has(startId)) return;
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
    components.push(ids.map((id) => nodeById.get(id)));
  });

  const packed = components.map((component) => {
    const initialBounds = nodeBounds(component);
    const currentAspect = initialBounds.width / Math.max(1, initialBounds.height);
    const stretch = Math.min(2.2, Math.max(1, Math.sqrt(targetAspect / Math.max(0.35, currentAspect))));
    component.forEach((item) => {
      item.x = (item.x - initialBounds.centerX) * stretch;
      item.y = (item.y - initialBounds.centerY) / stretch;
    });
    distributeAcrossHorizontalLanes(component);
    const bounds = nodeBounds(component);
    return {
      nodes: component,
      key: component.map((item) => item.id).sort()[0],
      width: bounds.width,
      height: bounds.height,
      centerX: bounds.centerX,
      centerY: bounds.centerY,
    };
  }).sort((left, right) => left.key.localeCompare(right.key));

  packComponentGrid(packed, graphCenter, targetAspect);
}

function packComponentGrid(components, graphCenter, targetAspect) {
  if (!components.length) return;
  const horizontalGap = 80;
  const verticalGap = 64;
  let best = null;
  for (let columns = 1; columns <= components.length; columns += 1) {
    const rows = [];
    for (let index = 0; index < components.length; index += columns) {
      const items = components.slice(index, index + columns);
      rows.push({
        items,
        width: items.reduce((total, item) => total + item.width, 0)
          + Math.max(0, items.length - 1) * horizontalGap,
        height: Math.max(...items.map((item) => item.height)),
      });
    }
    const width = Math.max(...rows.map((row) => row.width));
    const height = rows.reduce((total, row) => total + row.height, 0)
      + Math.max(0, rows.length - 1) * verticalGap;
    const aspect = width / Math.max(1, height);
    const emptySlots = rows.length * columns - components.length;
    const score = Math.abs(Math.log(aspect / targetAspect))
      + emptySlots / components.length * 0.025;
    if (!best || score < best.score) best = { rows, width, height, score };
  }

  let rowTop = graphCenter.centerY - best.height / 2;
  best.rows.forEach((row) => {
    let columnLeft = graphCenter.centerX - row.width / 2;
    row.items.forEach((component) => {
      const targetCenterX = columnLeft + component.width / 2;
      const targetCenterY = rowTop + row.height / 2;
      component.nodes.forEach((item) => {
        item.x += targetCenterX - component.centerX;
        item.y += targetCenterY - component.centerY;
        item.anchorX = item.x;
        item.anchorY = item.y;
      });
      columnLeft += component.width + horizontalGap;
    });
    rowTop += row.height + verticalGap;
  });
}

function distributeAcrossHorizontalLanes(nodes) {
  if (nodes.length < 3) return;
  const meanX = nodes.reduce((total, item) => total + item.x, 0) / nodes.length;
  const meanY = nodes.reduce((total, item) => total + item.y, 0) / nodes.length;
  const varianceX = nodes.reduce((total, item) => total + (item.x - meanX) ** 2, 0);
  const covariance = nodes.reduce((total, item) => total + (item.x - meanX) * (item.y - meanY), 0);
  const slope = varianceX ? covariance / varianceX : 0;
  nodes.forEach((item) => { item.y -= slope * (item.x - meanX); });

  const flattenedCenterY = nodes.reduce((total, item) => total + item.y, 0) / nodes.length;
  const maxDiameter = nodes.reduce((result, item) => Math.max(result, item.width, item.height), 0);
  const laneGap = maxDiameter * 0.82 + 42;
  const maxDegree = nodes.reduce((result, item) => Math.max(result, item.degree || 0), 0);
  const hubThreshold = Math.max(4, maxDegree - 1);
  const peripheral = nodes
    .filter((item) => (item.degree || 0) < hubThreshold)
    .sort((left, right) => left.x - right.x || left.id.localeCompare(right.id));
  const laneById = new Map(nodes.map((item) => [item.id, 0]));
  const lanePattern = nodes.length > 20 ? [-1, 1, -2, 2] : [-1, 1];
  peripheral.forEach((item, index) => laneById.set(item.id, lanePattern[index % lanePattern.length]));
  nodes.forEach((item) => {
    const residual = (item.y - flattenedCenterY) * 0.12;
    item.y = flattenedCenterY + laneById.get(item.id) * laneGap + residual;
  });
}

function nodeBounds(nodes) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  nodes.forEach((item) => {
    const radius = Math.max(item.width, item.height) / 2;
    minX = Math.min(minX, item.x - radius);
    minY = Math.min(minY, item.y - radius);
    maxX = Math.max(maxX, item.x + radius);
    maxY = Math.max(maxY, item.y + radius);
  });
  return {
    width: Math.max(1, maxX - minX),
    height: Math.max(1, maxY - minY),
    centerX: (minX + maxX) / 2,
    centerY: (minY + maxY) / 2,
  };
}

function horizontalAspectForce(targetAspect) {
  let nodes = [];
  const force = () => {
    if (nodes.length < 2) return;
    const bounds = nodeBounds(nodes);
    const currentAspect = bounds.width / Math.max(1, bounds.height);
    if (currentAspect >= targetAspect) return;
    const scale = Math.min(1.06, targetAspect / Math.max(0.1, currentAspect));
    nodes.forEach((item) => {
      item.x = bounds.centerX + (item.x - bounds.centerX) * scale;
      item.vx *= scale;
    });
  };
  force.initialize = (items) => { nodes = items; };
  return force;
}

function enforceHorizontalAspect(nodes, targetAspect) {
  for (let iteration = 0; iteration < 6; iteration += 1) {
    const bounds = nodeBounds(nodes);
    const currentAspect = bounds.width / Math.max(1, bounds.height);
    if (currentAspect >= targetAspect * 0.995) return;
    const scale = Math.min(1.5, targetAspect / Math.max(0.1, currentAspect));
    nodes.forEach((item) => {
      item.x = bounds.centerX + (item.x - bounds.centerX) * scale;
      item.vx *= scale;
    });
  }
}

function linkDistance(kind) {
  if (kind === 'object_relation') return 220;
  if (kind === 'uses_object') return 250;
  if (['uses_rule', 'applies_to_object'].includes(kind)) return 210;
  if (['uses_metric', 'metric_uses_object'].includes(kind)) return 205;
  if (['uses_script', 'script_bound_operation', 'script_uses_object'].includes(kind)) return 250;
  return 230;
}

function linkStrength(kind) {
  if (kind === 'object_relation') return 0.18;
  if (kind === 'uses_object') return 0.22;
  if (['uses_rule', 'applies_to_object'].includes(kind)) return 0.24;
  if (['uses_metric', 'metric_uses_object'].includes(kind)) return 0.2;
  if (['uses_script', 'script_bound_operation', 'script_uses_object'].includes(kind)) return 0.16;
  return 0.18;
}

function seededRandom(seed) {
  let state = 2166136261;
  for (const char of seed) {
    state ^= char.charCodeAt(0);
    state = Math.imul(state, 16777619);
  }
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}
