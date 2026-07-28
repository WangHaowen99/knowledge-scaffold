export function latestNodeOf(nodes, type) {
  return nodes.reduce((latest, node) => {
    if (node.type !== type) return latest;
    return !latest || node.ordinal > latest.ordinal ? node : latest;
  }, undefined);
}

export function batchesForNode(batches, nodeId) {
  if (!nodeId) return [];
  return batches.filter((batch) => batch.nodeId === nodeId);
}
