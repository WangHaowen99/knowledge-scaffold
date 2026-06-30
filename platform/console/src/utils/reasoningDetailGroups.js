import { buildIndexedSegmentGroups } from './segmentGroups.js';

const DETAIL_PROCESS_SEGMENT_TYPES = new Set(['tool', 'reasoning', 'state', 'subagent', 'subagent_group']);

export function resolveGroupIterationNumber(processSegs = [], contentSegs = []) {
  const candidates = [...processSegs, ...contentSegs]
    .map((segment) => {
      const value = segment?.meta?.iteration;
      const numeric = typeof value === 'number' ? value : Number(value);
      return Number.isFinite(numeric) && numeric > 0 ? Math.floor(numeric) : null;
    })
    .filter((value) => value != null);

  if (candidates.length === 0) return null;

  const counts = new Map();
  candidates.forEach((value) => {
    counts.set(value, (counts.get(value) || 0) + 1);
  });

  return [...counts.entries()]
    .sort((a, b) => {
      if (b[1] !== a[1]) return b[1] - a[1];
      return a[0] - b[0];
    })[0]?.[0] ?? null;
}

export function buildReasoningDetailGroups(streamSegments = []) {
  if (!Array.isArray(streamSegments) || streamSegments.length === 0) {
    return { groups: [], visibleGroupCount: 0 };
  }

  const hasIterationMetadata = streamSegments.some((segment) => {
    const value = segment?.meta?.iteration;
    const numeric = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(numeric) && numeric > 0;
  });

  if (!hasIterationMetadata) {
    const { completedGroups } = buildIndexedSegmentGroups(streamSegments);
    return {
      groups: completedGroups.map((group) => ({
        actualIteration: null,
        processSegs: group.processSegs,
        contentSegs: group.contentSegs,
        localVisibleIndex: group.index,
      })),
      visibleGroupCount: completedGroups.length,
    };
  }

  const buckets = new Map();
  streamSegments.forEach((segment) => {
    const value = segment?.meta?.iteration;
    const numeric = typeof value === 'number' ? value : Number(value);
    if (!Number.isFinite(numeric) || numeric <= 0) return;
    const iteration = Math.floor(numeric);
    if (!buckets.has(iteration)) {
      buckets.set(iteration, {
        actualIteration: iteration,
        processSegs: [],
        contentSegs: [],
      });
    }
    const bucket = buckets.get(iteration);
    if (DETAIL_PROCESS_SEGMENT_TYPES.has(segment?.type)) {
      bucket.processSegs.push(segment);
    } else {
      bucket.contentSegs.push(segment);
    }
  });

  let visibleGroupCount = 0;
  const groups = [...buckets.values()]
    .sort((a, b) => a.actualIteration - b.actualIteration)
    .map((group) => {
      const hasVisibleContent = group.contentSegs.some((segment) => (
        typeof segment?.text === 'string' && segment.text.trim().length > 0
      ));
      const localVisibleIndex = hasVisibleContent ? (visibleGroupCount += 1) : null;
      return {
        ...group,
        localVisibleIndex,
      };
    });

  return { groups, visibleGroupCount };
}

function getSegmentStableId(segment) {
  if (!segment) return '';

  return String(
    segment.groupId
    ?? segment.meta?.group_id
    ?? segment.meta?.subagent_id
    ?? segment.meta?.tool_call_id
    ?? segment.meta?.reasoning_block_id
    ?? segment.meta?.iteration
    ?? segment.ts
    ?? ''
  ).trim();
}

export function getReasoningDetailEntryKey(entry, entryIdx = 0) {
  if (entry?.kind === 'metric-only') {
    return `metric-only-${entry.iteration}`;
  }

  if (entry?.globalIndex != null) {
    return `content-group-${entry.globalIndex}`;
  }

  const processSegs = Array.isArray(entry?.processSegs) ? entry.processSegs : [];
  const contentSegs = Array.isArray(entry?.contentSegs) ? entry.contentSegs : [];
  const stableId = processSegs.map(getSegmentStableId).find(Boolean)
    || contentSegs.map(getSegmentStableId).find(Boolean)
    || `entry-${entryIdx}`;
  const iteration = Number.isFinite(Number(entry?.actualIteration)) ? Number(entry.actualIteration) : 'unknown';
  const groupIdx = Number.isFinite(Number(entry?.groupIdx)) ? Number(entry.groupIdx) : entryIdx;

  return `process-group-${iteration}-${groupIdx}-${stableId}`;
}
