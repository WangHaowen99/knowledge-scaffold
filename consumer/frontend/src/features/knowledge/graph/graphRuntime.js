const LIVE_STATUSES = new Set([
  'running',
  'streaming',
  'queued',
  'reconnecting',
  'finalizing',
]);
const TERMINAL_STATUSES = new Set([
  'completed',
  'finished',
  'success',
  'succeeded',
  'failed',
  'error',
  'cancelled',
  'canceled',
  'interrupted',
]);

const NODE_CANDIDATE_FIELDS = new Set([
  'element_class',
  'entity',
  'entity_type',
  'key',
  'label',
  'name',
  'object',
  'object_key',
  'operation',
  'operation_key',
  'resource_key',
]);

const STRUCTURED_NODE_FIELDS = new Set(['id', 'node_id', 'nodeid']);

const STRUCTURED_COLLECTION_FIELDS = new Set(['nodes', 'vertices']);

const RELATION_COLLECTION_FIELDS = new Set([
  'edges',
  'links',
  'paths',
  'relationships',
  'relations',
]);

const SOURCE_FIELDS = ['source', 'from', 'start', 'start_node', 'source_id'];
const TARGET_FIELDS = ['target', 'to', 'end', 'end_node', 'target_id'];

const STATUS_PRIORITY = {
  history: 1,
  recent: 2,
  live: 3,
};

const stringValue = (value) => (value == null ? '' : String(value).trim());

const plainObject = (value) =>
  value != null && typeof value === 'object' && !Array.isArray(value);

const normalizeToken = (value) =>
  stringValue(value)
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/\\/g, '/')
    .replace(/\.md$/u, '')
    .replace(/[\s_-]+/gu, '');

const timestampValue = (value) => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 0 && value < 1e11 ? value * 1000 : value;
  }
  if (typeof value !== 'string' || !value.trim()) {
    return null;
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    return timestampValue(numeric);
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const parseStructuredString = (value) => {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed || !['{', '['].includes(trimmed[0])) {
    return null;
  }
  try {
    const parsed = JSON.parse(trimmed);
    return plainObject(parsed) || Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
};

const graphNodeAliases = (node) => {
  const source = stringValue(node?.source).replace(/\\/g, '/');
  const sourceName = source.split('/').at(-1) || '';
  return [node?.id, node?.key, node?.label, source, sourceName]
    .map(normalizeToken)
    .filter(Boolean);
};

const buildNodeIndex = (nodes) => {
  const aliases = new Map();
  const nodesById = new Map();
  (Array.isArray(nodes) ? nodes : []).forEach((node) => {
    const nodeId = stringValue(node?.id);
    if (!nodeId) {
      return;
    }
    nodesById.set(nodeId, node);
    graphNodeAliases(node).forEach((alias) => {
      if (!aliases.has(alias)) {
        aliases.set(alias, new Set());
      }
      aliases.get(alias).add(nodeId);
    });
  });
  return { aliases, nodesById };
};

const nodeTypeForResource = (value) => {
  const normalized = stringValue(value).toLocaleLowerCase();
  if (['object', 'objects', 'entity', 'entities'].includes(normalized)) {
    return 'object';
  }
  if (['operation', 'operations', 'logic', 'logics'].includes(normalized)) {
    return 'operation';
  }
  return '';
};

const resolveCandidate = (value, index, typeHint = '') => {
  const normalized = normalizeToken(value);
  if (!normalized) {
    return [];
  }
  const matches = Array.from(index.aliases.get(normalized) || []);
  if (!typeHint || matches.length < 2) {
    return matches;
  }
  const filtered = matches.filter(
    (nodeId) => index.nodesById.get(nodeId)?.type === typeHint,
  );
  return filtered.length ? filtered : matches;
};

const endpointValue = (value) => {
  if (!plainObject(value)) {
    return value;
  }
  return (
    value.id ??
    value.node_id ??
    value.nodeId ??
    value.key ??
    value.entity ??
    value.entity_type ??
    value.label ??
    value.name
  );
};

const firstFieldValue = (value, fields) => {
  if (!plainObject(value)) {
    return undefined;
  }
  for (const field of fields) {
    if (value[field] != null) {
      return endpointValue(value[field]);
    }
  }
  return undefined;
};

const collectHitsFromValue = (value, index, options = {}, seen = new Set()) => {
  const nodeIds = new Set();
  const pairs = [];
  const addCandidate = (candidate, typeHint = '') => {
    resolveCandidate(candidate, index, typeHint).forEach((nodeId) =>
      nodeIds.add(nodeId),
    );
  };

  const visit = (current, context = {}) => {
    if (current == null) {
      return;
    }
    if (typeof current === 'string') {
      const parsed = parseStructuredString(current);
      if (parsed) {
        visit(parsed, { structured: true });
      } else if (context.candidate) {
        addCandidate(current, context.typeHint);
      }
      return;
    }
    if (typeof current !== 'object') {
      if (context.candidate) {
        addCandidate(current, context.typeHint);
      }
      return;
    }
    if (seen.has(current)) {
      return;
    }
    seen.add(current);

    if (Array.isArray(current)) {
      current.forEach((item) => visit(item, context));
      return;
    }

    const resourceType = nodeTypeForResource(
      current.resource ?? current.resource_type,
    );
    if (resourceType) {
      addCandidate(
        current.key ?? current.resource_key ?? current.name,
        resourceType,
      );
    }

    const sourceValue = firstFieldValue(current, SOURCE_FIELDS);
    const targetValue = firstFieldValue(current, TARGET_FIELDS);
    if (sourceValue != null && targetValue != null && context.relation) {
      const sources = resolveCandidate(sourceValue, index);
      const targets = resolveCandidate(targetValue, index);
      sources.forEach((source) => {
        nodeIds.add(source);
        targets.forEach((target) => {
          nodeIds.add(target);
          if (source !== target) {
            pairs.push([source, target]);
          }
        });
      });
    }

    Object.entries(current).forEach(([rawKey, item]) => {
      const key = rawKey.toLocaleLowerCase();
      const isStructuredCollection = STRUCTURED_COLLECTION_FIELDS.has(key);
      const isRelationCollection = RELATION_COLLECTION_FIELDS.has(key);
      const isCandidate =
        NODE_CANDIDATE_FIELDS.has(key) ||
        (context.structured && STRUCTURED_NODE_FIELDS.has(key));
      let typeHint = '';
      if (key.startsWith('operation')) {
        typeHint = 'operation';
      } else if (key.startsWith('object') || key.startsWith('entity')) {
        typeHint = 'object';
      }
      visit(item, {
        candidate: isCandidate,
        structured:
          context.structured || isStructuredCollection || isRelationCollection,
        relation: context.relation || isRelationCollection,
        typeHint,
      });
    });
  };

  visit(value, { structured: Boolean(options.structured), relation: false });
  return { nodeIds, pairs };
};

const normalizedStatus = (value) => stringValue(value).toLocaleLowerCase();

const statusIsLive = (value) => LIVE_STATUSES.has(normalizedStatus(value));
const statusIsTerminal = (value) =>
  TERMINAL_STATUSES.has(normalizedStatus(value));

const ontologyIdFrom = (value) => {
  if (!plainObject(value)) {
    return '';
  }
  const readDirect = (candidate) =>
    candidate?.selected_ontology_id ??
    candidate?.selectedOntologyId ??
    candidate?.ontology_id ??
    candidate?.ontologyId;
  const direct = readDirect(value);
  if (stringValue(direct)) {
    return stringValue(direct);
  }
  for (const nested of [
    value.metadata,
    value.context,
    value.sessionMetadata,
    value.input,
    value.tool_input,
    value.payload,
    value.meta,
    value.meta?.input,
    value.meta?.tool_input,
    value.payload?.input,
    value.payload?.tool_input,
  ]) {
    if (!plainObject(nested)) {
      continue;
    }
    const nestedId = readDirect(nested);
    if (stringValue(nestedId)) {
      return stringValue(nestedId);
    }
  }
  return '';
};

const liveStateFor = (value, inherited = false) => {
  const status = value?.status ?? value?.state;
  const eventType = normalizedStatus(value?.type);
  if (
    value?.finished === true ||
    statusIsTerminal(status) ||
    eventType === 'tool.finished'
  ) {
    return false;
  }
  if (
    value?.finished === false ||
    statusIsLive(status) ||
    eventType === 'tool.started'
  ) {
    return true;
  }
  return Boolean(inherited);
};

const unitTimestamp = (value, fallback = null) =>
  timestampValue(
    value?.endTs ??
      value?.endedAt ??
      value?.ts ??
      value?.timestamp ??
      value?.startTs ??
      value?.startedAt,
  ) ?? fallback;

const addUnit = (units, value, context) => {
  if (!plainObject(value)) {
    return;
  }
  units.push({
    value,
    live:
      typeof context.liveOverride === 'boolean' ?
        context.liveOverride :
        liveStateFor(value, context.live),
    runId: stringValue(context.runId),
    ontologyId: ontologyIdFrom(value) || stringValue(context.ontologyId),
    timestamp: unitTimestamp(value, context.timestamp),
    toolName: stringValue(
      value?.toolName ??
        value?.tool_name ??
        value?.name ??
        value?.meta?.tool_name ??
        value?.meta?.name,
    ),
    position: units.length,
  });
};

const collectBlock = (block, context, units) => {
  if (!plainObject(block)) {
    return;
  }
  const blockContext = {
    ...context,
    live: liveStateFor(block, context.live),
    ontologyId: ontologyIdFrom(block) || context.ontologyId,
    timestamp: unitTimestamp(block, context.timestamp),
  };
  const kind = stringValue(block.kind ?? block.type).toLocaleLowerCase();
  if (['tool', 'business_data', 'web_search'].includes(kind)) {
    addUnit(units, block, blockContext);
  }

  const group = block.group ?? (kind === 'subagent_group' ? block : null);
  if (group) {
    collectGroup(group, blockContext, units);
  }
};

const collectGroup = (group, context, units) => {
  if (!plainObject(group)) {
    return;
  }
  const groupContext = {
    ...context,
    live: liveStateFor(group, context.live),
    ontologyId: ontologyIdFrom(group) || context.ontologyId,
    timestamp: unitTimestamp(group, context.timestamp),
  };
  const children = Array.isArray(group.children) ?
    group.children :
    Object.values(group.children || {});
  children.forEach((child) => {
    const childContext = {
      ...groupContext,
      live: liveStateFor(child, groupContext.live),
      ontologyId: ontologyIdFrom(child) || groupContext.ontologyId,
      timestamp: unitTimestamp(child, groupContext.timestamp),
    };
    const items = Array.isArray(child?.items) ?
      child.items :
      [
        ...(child?.events || []),
        ...(child?.groups || child?.childGroups || []),
      ];
    items.forEach((item) => {
      if (item?.kind === 'tool') {
        addUnit(units, item, childContext);
      } else if (item?.subtype === 'group' || item?.children) {
        collectGroup(item, childContext, units);
      }
    });
  });
};

const collectMessage = (message, units, rootOntologyId = '') => {
  if (!plainObject(message)) {
    return;
  }
  const runId = stringValue(message.runId ?? message.run_id);
  const messageStatus = message.status;
  const context = {
    live: liveStateFor(
      message,
      statusIsLive(messageStatus) || Boolean(message.isLoading),
    ),
    runId,
    ontologyId: ontologyIdFrom(message) || rootOntologyId,
    timestamp: unitTimestamp(message),
  };
  let blocks = [];
  if (Array.isArray(message.blocks)) {
    ({ blocks } = message);
  } else if (Array.isArray(message.streamSegments)) {
    blocks = message.streamSegments;
  }
  blocks.forEach((block) => collectBlock(block, context, units));
};

const collectTraceUnits = (input) => {
  const units = [];
  const rootOntologyId = ontologyIdFrom(input);
  let values;
  if (Array.isArray(input)) {
    values = input;
  } else if (Array.isArray(input?.messages)) {
    values = input.messages;
  } else if (Array.isArray(input?.envelopes)) {
    values = input.envelopes;
  } else if (plainObject(input)) {
    values = [input];
  } else {
    values = [];
  }
  values.forEach((value) => {
    const kind = stringValue(value?.kind ?? value?.type).toLocaleLowerCase();
    if (
      ['tool', 'business_data', 'web_search', 'subagent_group'].includes(kind)
    ) {
      collectBlock(
        value,
        {
          live:
            statusIsLive(value?.status ?? value?.state) ||
            value?.finished === false,
          runId: stringValue(value?.runId ?? value?.run_id),
          ontologyId: ontologyIdFrom(value) || rootOntologyId,
          timestamp: unitTimestamp(value),
        },
        units,
      );
      return;
    }
    collectMessage(value, units, rootOntologyId);
  });
  return units;
};

const stateForUnit = (
  unit,
  latestRunId,
  latestPosition,
  now,
  recentWindowMs,
) => {
  if (unit.live) {
    return 'live';
  }
  if (unit.runId && unit.runId === latestRunId) {
    return 'recent';
  }
  if (!unit.runId && unit.position === latestPosition) {
    return 'recent';
  }
  if (unit.timestamp != null && now - unit.timestamp <= recentWindowMs) {
    return 'recent';
  }
  return 'history';
};

const mergeHit = (target, key, state, unit) => {
  const existing = target[key];
  const next = {
    state,
    hitCount: (existing?.hitCount || 0) + 1,
    lastHitAt: Math.max(existing?.lastHitAt || 0, unit.timestamp || 0) || null,
    runId: unit.runId || existing?.runId || '',
    toolName: unit.toolName || existing?.toolName || '',
  };
  if (existing && STATUS_PRIORITY[existing.state] > STATUS_PRIORITY[state]) {
    next.state = existing.state;
  }
  target[key] = next;
};

export const graphEdgeIdentity = (edge, index = 0) =>
  [
    stringValue(edge?.source),
    stringValue(edge?.target),
    stringValue(edge?.type || edge?.label || 'related'),
    index,
  ].join('::');

/**
 * Project hydrated messages or canonical chat blocks onto the static ontology
 * graph. All matching is exact after light identifier
 * normalization so user prose cannot accidentally light up the graph.
 */
export const deriveGraphRuntimeState = (graph, traceInput, options = {}) => {
  const now = Number.isFinite(Number(options.now)) ?
    Number(options.now) :
    Date.now();
  const recentWindowMs = Number.isFinite(Number(options.recentWindowMs)) ?
    Math.max(0, Number(options.recentWindowMs)) :
    5 * 60 * 1000;
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph?.edges) ? graph.edges : [];
  const index = buildNodeIndex(nodes);
  const targetOntologyId = stringValue(options.ontologyId);
  const rootOntologyId = ontologyIdFrom(traceInput);
  const allUnits = collectTraceUnits(traceInput);
  let units = allUnits;
  if (targetOntologyId) {
    if (rootOntologyId && rootOntologyId !== targetOntologyId) {
      units = [];
    } else {
      units = allUnits.filter((unit) => {
        if (unit.ontologyId) {
          return unit.ontologyId === targetOntologyId;
        }
        return rootOntologyId === targetOntologyId;
      });
    }
  }
  const latestRunId =
    [...units].reverse().find((unit) => unit.runId)?.runId || '';
  const latestPosition = units.at(-1)?.position ?? -1;
  const nodeHits = {};
  const edgeHits = {};

  units.forEach((unit) => {
    const state = stateForUnit(
      unit,
      latestRunId,
      latestPosition,
      now,
      recentWindowMs,
    );
    const meta = unit.value?.meta || {};
    const input =
      unit.value?.input ??
      meta.input ??
      unit.value?.tool_input ??
      meta.tool_input;
    const output =
      unit.value?.parsedOutput ??
      unit.value?.outputProjection ??
      unit.value?.output_projection ??
      unit.value?.output ??
      unit.value?.output_preview ??
      meta.output_projection ??
      meta.output ??
      meta.output_preview;
    const payload = {
      ...unit.value,
      input,
      output,
    };
    const { nodeIds, pairs } = collectHitsFromValue(payload, index, {
      structured: true,
    });
    nodeIds.forEach((nodeId) => mergeHit(nodeHits, nodeId, state, unit));

    edges.forEach((edge, edgeIndex) => {
      const inferred = nodeIds.has(edge.source) && nodeIds.has(edge.target);
      const explicit = pairs.some(
        ([source, target]) =>
          (source === edge.source && target === edge.target) ||
          (source === edge.target && target === edge.source),
      );
      if (inferred || explicit) {
        mergeHit(edgeHits, graphEdgeIdentity(edge, edgeIndex), state, unit);
      }
    });
  });

  const counts = { live: 0, recent: 0, history: 0 };
  Object.values(nodeHits).forEach((hit) => {
    counts[hit.state] += 1;
  });
  return {
    nodes: nodeHits,
    edges: edgeHits,
    counts,
    hasHits: Object.keys(nodeHits).length > 0,
  };
};
