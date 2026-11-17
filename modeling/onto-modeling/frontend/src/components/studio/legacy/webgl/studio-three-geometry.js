export function graphNodeDepth(node) {
  let hash = 2166136261;
  for (const character of String(node.id)) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  // Avalanche sequential IDs before assigning depth; adjacent IDs and kinds
  // must not turn into parallel sheets when viewed from the side.
  hash = Math.imul(hash ^ (hash >>> 16), 0x85ebca6b);
  hash = Math.imul(hash ^ (hash >>> 13), 0xc2b2ae35);
  return ((hash ^ (hash >>> 16)) >>> 0) / 4294967295 - 0.5;
}

export function graphDepthDistribution(nodes = []) {
  const ranked = nodes.map((node) => ({ id: node.id, depth: graphNodeDepth(node) }))
    .sort((a, b) => a.depth - b.depth || String(a.id).localeCompare(String(b.id)));
  // Stratification occupies the volume, including its middle, without an
  // iterative 3D simulation or placing all nodes on a spherical surface.
  return new Map(ranked.map((node, index) => [node.id, ranked.length > 1 ? index / (ranked.length - 1) - 0.5 : 0]));
}

export function graphDepthSpan(positions, mode = 'force') {
  let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
  let minZ = Infinity; let maxZ = -Infinity;
  positions.forEach(({ x, y, z }) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    if (Number.isFinite(z)) { minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z); }
  });
  if (mode === 'layered' && Number.isFinite(minZ)) return maxZ - minZ;
  const width = Number.isFinite(minX) ? maxX - minX : 0;
  const height = Number.isFinite(minY) ? maxY - minY : 0;
  // Category rows remain readable; the relationship network has comparable
  // planar and depth extents even after its force worker changes x/y positions.
  return mode === 'layered' ? Math.max(180, width * 0.45) : Math.max(360, Math.max(width, height) * 0.85);
}

export function graphNodeRadius(node) {
  return Math.max(13, Math.min(31, Math.min(node.width || 100, node.height || 100) * 0.27));
}

export function graphWorldPosition(node, position, depth = graphNodeDepth(node)) {
  return { x: position.x, y: -position.y, z: depth };
}

export function graphCameraDistance(size, aspect, fieldOfView = 42) {
  const tangent = Math.tan(fieldOfView * Math.PI / 360);
  return Math.max(260, Math.max(size.y, size.x / Math.max(0.2, aspect)) / (2 * tangent) * 1.2 + size.z * 0.7);
}

export function validCameraSnapshot(value) {
  return Boolean(value && ['position', 'target'].every((key) => (
    Array.isArray(value[key]) && value[key].length === 3
      && value[key].every((number) => Number.isFinite(number) && Math.abs(number) < 1e7)
  )) && Math.hypot(...value.position.map((number, index) => number - value.target[index])) > 1);
}
