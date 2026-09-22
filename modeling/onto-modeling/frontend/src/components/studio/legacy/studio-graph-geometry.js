const AXIS_EPSILON = 0.75;
const MIN_SHARED_HOVER_LENGTH = 24;
const MIN_LABEL_INTERVAL = 28;
const LABEL_HEIGHT = 21;
const LABEL_GAP = 4;
const NODE_LABEL_GAP = 8;
const ROUTE_LABEL_GAP = 5;
const ROUTE_OWNERSHIP_MARGIN = 4;
const LABEL_MAX_WIDTH = 112;
const LABEL_SECONDARY_OFFSET = 26;
const RECIPROCAL_ROUTE_GAP = 18;

export function buildStudioRoutePresentation(routes, labels = {}, nodes = []) {
  const segmentsByEdge = new Map(
    Object.entries(routes).map(([edgeId, points]) => [edgeId, routeSegments(points)]),
  );
  const sharedEdges = new Map([...segmentsByEdge.keys()].map((edgeId) => [edgeId, new Set()]));
  const entries = [...segmentsByEdge.entries()];
  for (let leftIndex = 0; leftIndex < entries.length; leftIndex += 1) {
    const [leftId, leftSegments] = entries[leftIndex];
    for (let rightIndex = leftIndex + 1; rightIndex < entries.length; rightIndex += 1) {
      const [rightId, rightSegments] = entries[rightIndex];
      if (longestSharedInterval(leftSegments, rightSegments) < MIN_SHARED_HOVER_LENGTH) continue;
      sharedEdges.get(leftId).add(rightId);
      sharedEdges.get(rightId).add(leftId);
    }
  }
  const labelAnchors = Object.fromEntries(entries.map(([edgeId, segments]) => [
    edgeId,
    chooseLabelAnchor(edgeId, segments, entries),
  ]));
  separateLabelAnchors(routes, labelAnchors, labels, nodes);
  return {
    labelAnchors,
    sharedEdges: Object.fromEntries(
      [...sharedEdges.entries()].map(([edgeId, related]) => [edgeId, [...related]]),
    ),
  };
}

export function studioRoundedPath(points, radius = 11) {
  if (points.length < 2) return '';
  let path = `M ${points[0].x} ${points[0].y}`;
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    const next = points[index + 1];
    const incoming = Math.hypot(current.x - previous.x, current.y - previous.y);
    const outgoing = Math.hypot(next.x - current.x, next.y - current.y);
    const cross = (current.x - previous.x) * (next.y - current.y)
      - (current.y - previous.y) * (next.x - current.x);
    if (!incoming || !outgoing || Math.abs(cross) < 0.01) {
      path += ` L ${current.x} ${current.y}`;
      continue;
    }
    const cornerRadius = Math.min(radius, incoming / 2, outgoing / 2);
    const before = {
      x: current.x - ((current.x - previous.x) / incoming) * cornerRadius,
      y: current.y - ((current.y - previous.y) / incoming) * cornerRadius,
    };
    const after = {
      x: current.x + ((next.x - current.x) / outgoing) * cornerRadius,
      y: current.y + ((next.y - current.y) / outgoing) * cornerRadius,
    };
    path += ` L ${before.x} ${before.y} Q ${current.x} ${current.y} ${after.x} ${after.y}`;
  }
  const last = points[points.length - 1];
  return `${path} L ${last.x} ${last.y}`;
}

export function studioRoutePoints(route, sourceX, sourceY, targetX, targetY) {
  const fallback = [{ x: sourceX, y: sourceY }, { x: targetX, y: targetY }];
  return (route?.length >= 2 ? route : fallback)
    .filter((point, index, items) => index === 0 || point.x !== items[index - 1].x || point.y !== items[index - 1].y)
    .map((point) => ({ ...point }));
}

export function studioRouteForNodePositions(
  route,
  sourceNode,
  targetNode,
  sourcePosition,
  targetPosition,
) {
  const points = (route || []).map((point) => ({ ...point }));
  if (points.length < 2) return points;
  const sourceDelta = positionDelta(sourceNode?.position, sourcePosition);
  const targetDelta = positionDelta(targetNode?.position, targetPosition);
  if (samePoint(sourceDelta, targetDelta)) {
    return points.map((point) => ({
      x: point.x + sourceDelta.x,
      y: point.y + sourceDelta.y,
    }));
  }
  const originalFirst = points[0];
  const originalSecond = points[1];
  const originalLast = points[points.length - 1];
  const originalPrevious = points[points.length - 2];
  points[0] = {
    x: originalFirst.x + sourceDelta.x,
    y: originalFirst.y + sourceDelta.y,
  };
  points[points.length - 1] = {
    x: originalLast.x + targetDelta.x,
    y: originalLast.y + targetDelta.y,
  };
  if (points.length === 2) {
    return orthogonalRoute(points[0], points[1], isHorizontal(originalFirst, originalLast));
  }
  if (isHorizontal(originalFirst, originalSecond)) points[1].y = points[0].y;
  else points[1].x = points[0].x;
  const previousIndex = points.length - 2;
  if (isHorizontal(originalPrevious, originalLast)) points[previousIndex].y = points[points.length - 1].y;
  else points[previousIndex].x = points[points.length - 1].x;
  return compactRoutePoints(points);
}

export function separateStudioReciprocalRoutes(routes, edges, gap = RECIPROCAL_ROUTE_GAP) {
  const separated = Object.fromEntries(
    Object.entries(routes).map(([edgeId, points]) => [edgeId, points.map((point) => ({ ...point }))]),
  );
  const groups = new Map();
  for (const edge of edges || []) {
    const pairKey = [edge.source, edge.target].sort().join('\u0000');
    const pathKey = canonicalRouteKey(separated[edge.id]);
    if (!pathKey) continue;
    const key = `${pairKey}\u0001${pathKey}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(edge);
  }
  for (const group of groups.values()) {
    const directions = new Set(group.map((edge) => `${edge.source}\u0000${edge.target}`));
    if (group.length < 2 || directions.size < 2) continue;
    const ordered = [...group].sort((left, right) => String(left.id).localeCompare(String(right.id)));
    ordered.forEach((edge, index) => {
      const points = separated[edge.id];
      const offset = (index - (ordered.length - 1) / 2) * gap;
      const first = points[0];
      const last = points[points.length - 1];
      const horizontal = Math.abs(last.x - first.x) >= Math.abs(last.y - first.y);
      separated[edge.id] = points.map((point) => ({
        x: point.x + (horizontal ? 0 : offset),
        y: point.y + (horizontal ? offset : 0),
      }));
    });
  }
  return separated;
}

export function studioRouteHandles(route, sourceNode, targetNode) {
  if (!route?.length || !sourceNode || !targetNode) {
    return { sourceHandle: 'source-right', targetHandle: 'target-left' };
  }
  return {
    sourceHandle: `source-${nearestHorizontalSide(route[0], sourceNode, 'right')}`,
    targetHandle: `target-${nearestHorizontalSide(route[route.length - 1], targetNode, 'left')}`,
  };
}

export function studioLabelPosition(points, anchor) {
  if (!anchor || !points[anchor.segmentIndex + 1]) return longestSegmentMiddle(points);
  const start = points[anchor.segmentIndex];
  const end = points[anchor.segmentIndex + 1];
  const point = {
    x: start.x + (end.x - start.x) * anchor.t,
    y: start.y + (end.y - start.y) * anchor.t,
  };
  return point;
}

export function studioPolylineLength(points) {
  let length = 0;
  for (let index = 0; index < points.length - 1; index += 1) {
    length += Math.hypot(points[index + 1].x - points[index].x, points[index + 1].y - points[index].y);
  }
  return length;
}

function routeSegments(points = []) {
  const normalized = points.filter((point, index, items) => (
    index === 0 || point.x !== items[index - 1].x || point.y !== items[index - 1].y
  ));
  return normalized.slice(0, -1).map((start, index) => {
    const end = normalized[index + 1];
    const horizontal = Math.abs(end.x - start.x) >= Math.abs(end.y - start.y);
    const startAxis = horizontal ? start.x : start.y;
    const endAxis = horizontal ? end.x : end.y;
    return {
      index,
      horizontal,
      crossAxis: horizontal ? (start.y + end.y) / 2 : (start.x + end.x) / 2,
      startAxis,
      endAxis,
      from: Math.min(startAxis, endAxis),
      to: Math.max(startAxis, endAxis),
      length: Math.abs(endAxis - startAxis),
    };
  }).filter((segment) => segment.length > AXIS_EPSILON);
}

function longestSharedInterval(leftSegments, rightSegments) {
  let longest = 0;
  for (const left of leftSegments) {
    for (const right of rightSegments) {
      const overlap = segmentOverlap(left, right);
      if (overlap) longest = Math.max(longest, overlap.to - overlap.from);
    }
  }
  return longest;
}

function chooseLabelAnchor(edgeId, segments, allEntries) {
  const otherSegments = allEntries
    .filter(([candidateId]) => candidateId !== edgeId)
    .flatMap(([, candidateSegments]) => candidateSegments);
  const candidates = [];
  for (const segment of segments) {
    const covered = mergeIntervals(otherSegments
      .map((candidate) => segmentOverlap(segment, candidate))
      .filter(Boolean));
    for (const interval of subtractIntervals({ from: segment.from, to: segment.to }, covered)) {
      const length = interval.to - interval.from;
      if (length < MIN_LABEL_INTERVAL) continue;
      candidates.push({ segment, interval, score: length + segment.index * 4 });
    }
  }
  const selected = candidates.sort((left, right) => right.score - left.score)[0];
  if (selected) return anchorForInterval(edgeId, selected.segment, selected.interval, false);
  const fallback = [...segments].sort((left, right) => right.length - left.length)[0];
  if (!fallback) return null;
  return anchorForInterval(edgeId, fallback, { from: fallback.from, to: fallback.to }, true);
}

function anchorForInterval(edgeId, segment, interval, fallback) {
  const axis = (interval.from + interval.to) / 2;
  const denominator = segment.endAxis - segment.startAxis;
  return {
    segmentIndex: segment.index,
    t: denominator ? (axis - segment.startAxis) / denominator : 0.5,
    normalOffset: stableSide(edgeId) * (fallback ? 16 : 10),
  };
}

function separateLabelAnchors(routes, anchors, labels, nodes) {
  const obstacles = nodeLabelObstacles(nodes);
  for (const edgeId of Object.keys(anchors).sort()) {
    const anchor = anchors[edgeId];
    const points = routes[edgeId];
    if (!labels[edgeId] || !anchor || !points?.[anchor.segmentIndex + 1]) continue;
    const preferredSide = Math.sign(anchor.normalOffset) || stableSide(edgeId);
    const preferredDistance = Math.max(10, Math.abs(anchor.normalOffset));
    const offsets = [
      preferredSide * preferredDistance,
      -preferredSide * preferredDistance,
      preferredSide * LABEL_SECONDARY_OFFSET,
      -preferredSide * LABEL_SECONDARY_OFFSET,
      0,
    ];
    const routeObstacles = routeLabelObstacles(routes, edgeId);
    const tCandidates = labelAnchorCandidates(anchor.t);
    const size = estimatedLabelSize(labels[edgeId]);
    let selected = offsets[offsets.length - 1];
    let selectedT = anchor.t;
    let selectedBox = null;
    let bestFallback = null;
    outer: for (const normalOffset of offsets) {
      for (const t of tCandidates) {
        const candidate = { ...anchor, normalOffset, t };
        const position = studioLabelPosition(points, candidate);
        const box = labelBox(position, size);
        const boxConflicts = obstacles.filter(
          (obstacle) => boxesOverlap(box, obstacle.box, obstacle.gap),
        ).length;
        const routeConflicts = routeObstacles.filter(
          (obstacle) => boxesOverlap(box, obstacle, ROUTE_LABEL_GAP),
        ).length;
        const ownDistance = pointToRouteDistance(position, points);
        const nearestOtherDistance = nearestRouteDistance(position, routes, edgeId);
        const ownershipDeficit = Number.isFinite(nearestOtherDistance)
          ? Math.max(0, ownDistance + ROUTE_OWNERSHIP_MARGIN - nearestOtherDistance)
          : 0;
        const fallbackScore = ownershipDeficit * 100000
          + routeConflicts * 10000
          + boxConflicts * 1000
          + Math.abs(normalOffset) * 10
          + Math.abs(t - anchor.t) * 100;
        if (!bestFallback || fallbackScore < bestFallback.score) {
          bestFallback = { box, normalOffset, score: fallbackScore, t };
        }
        const clearOfBoxes = boxConflicts === 0;
        const clearOfRoutes = routeConflicts === 0;
        const ownedByRoute = ownershipDeficit === 0;
        if (!clearOfBoxes || !clearOfRoutes || !ownedByRoute) continue;
        selected = normalOffset;
        selectedT = t;
        selectedBox = box;
        break outer;
      }
    }
    if (!selectedBox && bestFallback) {
      selected = bestFallback.normalOffset;
      selectedT = bestFallback.t;
      selectedBox = bestFallback.box;
    }
    anchor.normalOffset = selected;
    anchor.t = selectedT;
    obstacles.push({
      box: selectedBox || labelBox(studioLabelPosition(points, anchor), size),
      gap: LABEL_GAP,
    });
  }
}

function nearestRouteDistance(point, routes, edgeId) {
  let nearest = Number.POSITIVE_INFINITY;
  for (const [candidateId, points] of Object.entries(routes)) {
    if (candidateId === edgeId) continue;
    nearest = Math.min(nearest, pointToRouteDistance(point, points));
  }
  return nearest;
}

function positionDelta(original, current) {
  const originalX = Number(original?.x);
  const originalY = Number(original?.y);
  const currentX = Number(current?.x);
  const currentY = Number(current?.y);
  return {
    x: Number.isFinite(originalX) && Number.isFinite(currentX) ? currentX - originalX : 0,
    y: Number.isFinite(originalY) && Number.isFinite(currentY) ? currentY - originalY : 0,
  };
}

function orthogonalRoute(start, end, horizontal) {
  if (start.x === end.x || start.y === end.y) return [start, end];
  if (horizontal) {
    const middleX = (start.x + end.x) / 2;
    return compactRoutePoints([
      start,
      { x: middleX, y: start.y },
      { x: middleX, y: end.y },
      end,
    ]);
  }
  const middleY = (start.y + end.y) / 2;
  return compactRoutePoints([
    start,
    { x: start.x, y: middleY },
    { x: end.x, y: middleY },
    end,
  ]);
}

function compactRoutePoints(points) {
  return points.filter((point, index, items) => (
    index === 0 || !samePoint(point, items[index - 1])
  ));
}

function samePoint(left, right) {
  return left?.x === right?.x && left?.y === right?.y;
}

function isHorizontal(start, end) {
  return Math.abs(end.x - start.x) >= Math.abs(end.y - start.y);
}

function pointToRouteDistance(point, points) {
  let nearest = Number.POSITIVE_INFINITY;
  for (let index = 0; index < (points?.length || 0) - 1; index += 1) {
    const start = points[index];
    const end = points[index + 1];
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const denominator = dx * dx + dy * dy;
    const t = denominator
      ? Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / denominator))
      : 0;
    nearest = Math.min(nearest, Math.hypot(
      point.x - (start.x + t * dx),
      point.y - (start.y + t * dy),
    ));
  }
  return nearest;
}

function routeLabelObstacles(routes, edgeId) {
  return Object.entries(routes).flatMap(([candidateId, points]) => {
    if (candidateId === edgeId) return [];
    return (points || []).slice(0, -1).flatMap((start, index) => {
      const end = points[index + 1];
      if (!end || (start.x === end.x && start.y === end.y)) return [];
      return [{
        left: Math.min(start.x, end.x),
        right: Math.max(start.x, end.x),
        top: Math.min(start.y, end.y),
        bottom: Math.max(start.y, end.y),
      }];
    });
  });
}

function labelAnchorCandidates(preferred) {
  return [...new Set([preferred, 0.32, 0.68, 0.18, 0.82, 0.12, 0.88].map((value) => (
    Math.min(0.88, Math.max(0.12, Math.round(value * 100) / 100))
  )))].sort((left, right) => Math.abs(left - preferred) - Math.abs(right - preferred));
}

function nodeLabelObstacles(nodes) {
  return (nodes || []).flatMap((node) => {
    const left = Number(node.position?.x);
    const top = Number(node.position?.y);
    const width = Number(node.width);
    const height = Number(node.height);
    if (![left, top, width, height].every(Number.isFinite)) return [];
    return [{
      box: { left, right: left + width, top, bottom: top + height },
      gap: NODE_LABEL_GAP,
    }];
  });
}

function estimatedLabelSize(label) {
  const textWidth = Array.from(String(label || '')).reduce(
    (width, character) => width + (character.codePointAt(0) > 255 ? 11 : 6.2),
    0,
  );
  return { width: Math.min(LABEL_MAX_WIDTH, Math.max(28, textWidth + 14)), height: LABEL_HEIGHT };
}

function labelBox(point, size) {
  return {
    left: point.x - size.width / 2,
    right: point.x + size.width / 2,
    top: point.y - size.height / 2,
    bottom: point.y + size.height / 2,
  };
}

function boxesOverlap(left, right, gap) {
  return left.left < right.right + gap
    && left.right + gap > right.left
    && left.top < right.bottom + gap
    && left.bottom + gap > right.top;
}

function canonicalRouteKey(points) {
  if (!points?.length) return '';
  const forward = points.map(pointKey).join(';');
  const reverse = [...points].reverse().map(pointKey).join(';');
  return forward < reverse ? forward : reverse;
}

function pointKey(point) {
  return `${Math.round(Number(point.x) * 10) / 10},${Math.round(Number(point.y) * 10) / 10}`;
}

function nearestHorizontalSide(point, node, fallback) {
  const x = Number(point?.x);
  const left = Number(node.position?.x);
  const width = Number(node.width);
  if (![x, left, width].every(Number.isFinite)) return fallback;
  return Math.abs(x - left) <= Math.abs(x - (left + width)) ? 'left' : 'right';
}

function segmentOverlap(left, right) {
  if (left.horizontal !== right.horizontal) return null;
  if (Math.abs(left.crossAxis - right.crossAxis) > AXIS_EPSILON) return null;
  const from = Math.max(left.from, right.from);
  const to = Math.min(left.to, right.to);
  return to - from > AXIS_EPSILON ? { from, to } : null;
}

function mergeIntervals(intervals) {
  const sorted = [...intervals].sort((left, right) => left.from - right.from);
  const merged = [];
  for (const interval of sorted) {
    const previous = merged[merged.length - 1];
    if (!previous || interval.from > previous.to + AXIS_EPSILON) merged.push({ ...interval });
    else previous.to = Math.max(previous.to, interval.to);
  }
  return merged;
}

function subtractIntervals(source, covered) {
  const result = [];
  let cursor = source.from;
  for (const interval of covered) {
    if (interval.to <= cursor) continue;
    if (interval.from > cursor) result.push({ from: cursor, to: Math.min(interval.from, source.to) });
    cursor = Math.max(cursor, interval.to);
    if (cursor >= source.to) break;
  }
  if (cursor < source.to) result.push({ from: cursor, to: source.to });
  return result.filter((interval) => interval.to - interval.from > AXIS_EPSILON);
}

function longestSegmentMiddle(points) {
  let result = points[0] || { x: 0, y: 0 };
  let longest = -1;
  for (let index = 0; index < points.length - 1; index += 1) {
    const start = points[index];
    const end = points[index + 1];
    const length = Math.hypot(end.x - start.x, end.y - start.y);
    if (length <= longest) continue;
    longest = length;
    result = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
  }
  return result;
}

function stableSide(value) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) hash = ((hash << 5) - hash + value.charCodeAt(index)) | 0;
  return hash % 2 === 0 ? -1 : 1;
}
