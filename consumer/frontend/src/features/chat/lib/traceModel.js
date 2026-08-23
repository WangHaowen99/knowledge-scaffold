import { buildConversationViewModel } from './conversationModel.js';

const timestampValue = (value) => (
  typeof value === 'number' && Number.isFinite(value) ? value : null
);

const attachNodesBySpanId = (nodes, parentCandidates, fallbackParentKey) => {
  const nodeBySpanId = new Map();
  [...parentCandidates, ...nodes].forEach((node) => {
    if (node?.spanId && !nodeBySpanId.has(node.spanId)) {
      nodeBySpanId.set(node.spanId, node);
    }
  });
  const roots = [];
  nodes.forEach((node) => {
    const parent = node?.parentSpanId ? nodeBySpanId.get(node.parentSpanId) : null;
    if (parent && parent !== node) {
      node.parentKey = parent.key;
      parent.children.push(node);
    } else {
      node.parentKey = fallbackParentKey;
      roots.push(node);
    }
  });
  return roots;
};

const traceProgressNode = (progress, parentKey, index) => ({
  key: `trace:${progress.key}`,
  sourceKey: progress.key,
  parentKey,
  type: progress.kind,
  subtype: progress.type,
  status: progress.terminal ? 'finished' : 'running',
  terminal: progress.terminal,
  text: progress.text,
  startedAt: progress.startedAt,
  endedAt: progress.endedAt,
  durationMs: progress.durationMs ?? 0,
  order: index,
  children: [],
});

const traceSubagentGroup = (group, parentKey) => ({
  key: `trace:${group.key}`,
  sourceKey: group.key,
  parentKey,
  type: 'subagent',
  subtype: 'group',
  status: group.status,
  terminal: group.terminal,
  text: group.groupId,
  traceId: group.traceId,
  spanId: group.spanId,
  parentSpanId: group.parentSpanId,
  startedAt: group.startedAt,
  endedAt: group.endedAt,
  durationMs: group.durationMs,
  order: 0,
  children: group.children.map((child, childIndex) => {
    const childKey = `trace:${child.key}`;
    const eventNode = (event, eventIndex) => ({
      key: `trace:${event.key}`,
      sourceKey: event.key,
      parentKey: childKey,
      type: event.kind,
      subtype: event.kind === 'tool' ? 'tool' : 'event',
      status: event.status,
      terminal: event.terminal,
      text: event.text,
      toolName: event.toolName,
      toolCallId: event.toolCallId,
      traceId: event.traceId,
      spanId: event.spanId,
      parentSpanId: event.parentSpanId,
      input: event.input,
      output: event.output,
      outputRef: event.outputRef,
      outputSize: event.outputSize,
      outputTruncated: event.outputTruncated,
      parsedOutput: event.parsedOutput,
      error: event.error,
      startedAt: event.startedAt,
      endedAt: event.endedAt,
      durationMs: event.durationMs,
      order: eventIndex,
      children: event.progress.map((progress, progressIndex) => (
        traceProgressNode(progress, `trace:${event.key}`, progressIndex)
      )),
    });
    const orderedItems = child.items?.length ? child.items : [...child.events, ...child.groups];
    const childNodes = orderedItems.map((item, itemIndex) => (
      item.subtype === 'group'
        ? traceSubagentGroup(item, childKey)
        : eventNode(item, itemIndex)
    ));
    return {
      key: childKey,
      sourceKey: child.key,
      parentKey: `trace:${group.key}`,
      type: 'subagent',
      subtype: 'task',
      status: child.status,
      terminal: child.terminal,
      text: child.task,
      subagentId: child.subagentId,
      taskId: child.taskId,
      result: child.result,
      error: child.error,
      traceId: child.traceId,
      spanId: child.spanId,
      parentSpanId: child.parentSpanId,
      startedAt: child.startedAt,
      endedAt: child.endedAt,
      durationMs: child.durationMs,
      order: childIndex,
      children: attachNodesBySpanId(childNodes, [], childKey),
    };
  }),
});

const traceNodeForBlock = (block, turnKey) => {
  const key = `trace:${block.key}`;
  const isNarrative = block.kind === 'content' || block.kind === 'reasoning';
  const narrativeLabel = block.kind === 'content'
    ? 'Agent 输出'
    : (block.summary || '模型思考');
  const label = isNarrative ? narrativeLabel : (block.summary || block.text);
  const node = {
    key,
    sourceKey: block.key,
    parentKey: turnKey,
    spanKey: block.spanKey,
    sourceParentKey: block.parentSpanId || block.parentKey,
    traceId: block.traceId,
    spanId: block.spanId,
    parentSpanId: block.parentSpanId,
    type: block.kind,
    subtype: block.segmentType,
    status: block.status,
    terminal: block.terminal,
    text: label,
    detail: block.text && block.text !== label ? block.text : '',
    startedAt: block.startedAt,
    endedAt: block.endedAt,
    durationMs: block.durationMs,
    order: block.order,
    children: block.progress.map((progress, index) => traceProgressNode(progress, key, index)),
  };
  if (['tool', 'business_data', 'web_search'].includes(block.kind)) {
    node.toolName = block.toolName;
    node.toolCallId = block.toolCallId;
    node.input = block.input;
    node.output = block.output;
    node.outputRef = block.outputRef;
    node.outputSize = block.outputSize;
    node.outputTruncated = block.outputTruncated;
    node.parsedOutput = block.parsedOutput;
    node.error = block.error;
  }
  if (block.kind === 'web_search') {
    node.query = block.query;
    node.sources = block.sources;
    node.resultCount = block.resultCount;
    node.provider = block.provider;
    node.responseTime = block.responseTime;
  }
  if (block.group) node.children.push(traceSubagentGroup(block.group, key));
  if (block.kind === 'awaiting') node.interaction = block.interaction;
  return node;
};

const iterationNodes = (turn, turnKey) => (turn.iterationMetrics || []).map((metric, index) => {
  const iteration = Number.isFinite(Number(metric?.iteration)) ? Number(metric.iteration) : index + 1;
  return {
    key: `${turnKey}:iteration:${iteration}`,
    sourceKey: `${turn.key}:iteration:${iteration}`,
    parentKey: turnKey,
    type: 'iteration',
    subtype: 'metric',
    status: metric?.status || 'finished',
    terminal: true,
    text: `iteration ${iteration}`,
    traceId: metric?.traceId || '',
    spanId: metric?.spanId || '',
    parentSpanId: metric?.parentSpanId || '',
    startedAt: null,
    endedAt: timestampValue(metric?.ts),
    durationMs: Number.isFinite(Number(metric?.totalElapsedMs)) ? Number(metric.totalElapsedMs) : null,
    order: index,
    metrics: { ...metric },
    children: [],
  };
});

const iterationFromParentKey = (parentKey) => {
  const match = String(parentKey || '').match(/^iteration:[^:]+:(\d+)$/);
  return match ? Number(match[1]) : null;
};

const iterationForBlock = (block) => (
  iterationFromParentKey(block?.parentKey)
  ?? (Number.isFinite(Number(block?.iteration)) && Number(block.iteration) > 0
    ? Math.floor(Number(block.iteration))
    : null)
);

/**
 * Trace consumes the canonical conversation model. `nodes` preserves source block
 * order; nested progress/subagent details live in `children` without reordering roots.
 */
export const buildTraceViewModel = (input = []) => {
  const conversation = input?.kind === 'conversation'
    ? input
    : buildConversationViewModel(input);
  const turns = conversation.turns.map((turn, turnIndex) => {
    const key = `trace-turn:${turn.key}`;
    const iterations = iterationNodes(turn, key);
    const iterationByNumber = new Map(
      iterations.map((iteration) => [Number(iteration.metrics?.iteration), iteration]),
    );
    const blockEntries = turn.blocks.map((block) => ({
      block,
      node: traceNodeForBlock(block, key),
    }));
    const nodeBySpanId = new Map();
    [...iterations, ...blockEntries.map((entry) => entry.node)].forEach((node) => {
      if (node?.spanId && !nodeBySpanId.has(node.spanId)) {
        nodeBySpanId.set(node.spanId, node);
      }
    });
    const nodes = [];
    blockEntries.forEach(({ block, node }) => {
      const otelParent = node.parentSpanId ? nodeBySpanId.get(node.parentSpanId) : null;
      const legacyIteration = iterationByNumber.get(iterationForBlock(block));
      const parent = otelParent && otelParent !== node ? otelParent : legacyIteration;
      if (parent) {
        node.parentKey = parent.key;
        parent.children.push(node);
        return;
      }
      nodes.push(node);
    });
    return {
      key,
      sourceKey: turn.key,
      runId: turn.runId,
      messageId: turn.id,
      status: turn.status,
      terminal: turn.terminal,
      startedAt: turn.startedAt,
      endedAt: turn.endedAt,
      durationMs: turn.durationMs,
      order: turnIndex,
      nodes,
      iterations,
    };
  });
  return {
    kind: 'trace',
    turns,
  };
};
