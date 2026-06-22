const PROCESS_TYPES = new Set(['tool', 'reasoning', 'state', 'subagent', 'subagent_group']);

/**
 * Groups stream segments into indexed pairs of (processSegs → contentSegs).
 *
 * Returns:
 *   completedGroups: [{ index, processSegs, contentSegs }]  — each content output block with its preceding process segments
 *   liveProcessSegs: StreamSegment[]                         — trailing process segs not yet followed by content (currently streaming)
 */
export function buildIndexedSegmentGroups(streamSegments) {
  if (!Array.isArray(streamSegments) || streamSegments.length === 0) {
    return { completedGroups: [], liveProcessSegs: [] };
  }

  // Build alternating groups of process / content
  const rawGroups = [];
  let cur = null;
  for (const seg of streamSegments) {
    const t = PROCESS_TYPES.has(seg.type) ? 'process' : 'content';
    if (!cur || cur.type !== t) {
      cur = { type: t, segments: [] };
      rawGroups.push(cur);
    }
    cur.segments.push(seg);
  }

  // Pair each content group with its preceding process group
  const completedGroups = [];
  let pendingProcessGroup = null;
  let contentIndex = 0;

  for (const group of rawGroups) {
    if (group.type === 'process') {
      pendingProcessGroup = group;
    } else {
      const visibleContentSegs = group.segments.filter((seg) => (
        typeof seg?.text === 'string' && seg.text.trim().length > 0
      ));
      if (visibleContentSegs.length === 0) {
        continue;
      }
      contentIndex += 1;
      completedGroups.push({
        index: contentIndex,
        processSegs: pendingProcessGroup ? pendingProcessGroup.segments : [],
        contentSegs: visibleContentSegs,
      });
      pendingProcessGroup = null;
    }
  }

  // Any trailing process group = live (no content following it yet)
  const liveProcessSegs = pendingProcessGroup ? pendingProcessGroup.segments : [];

  return { completedGroups, liveProcessSegs };
}
