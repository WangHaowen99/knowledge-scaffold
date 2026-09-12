export const ACTIVE_SKILL_STATUSES = Object.freeze([
  'submitted',
  'running',
  'downloading',
]);
export const SKILL_NEW_VERSION_ACTIVATION_COPY =
  '批准后，新版本会立即成为知识包激活版本。';

export const DIFF_SKILL_STATUSES = Object.freeze([
  'completed',
  'approved',
  'rejected',
]);
export const TERMINAL_SKILL_STATUSES = Object.freeze([
  'approved',
  'rejected',
  'no_change',
  'failed',
  'interrupted',
  'cancelled',
]);

const activeStatuses = new Set(ACTIVE_SKILL_STATUSES);
const diffStatuses = new Set(DIFF_SKILL_STATUSES);
const terminalStatuses = new Set(TERMINAL_SKILL_STATUSES);

export function evolutionScopeKey(workspaceId = '', ontologyId = '') {
  return JSON.stringify([String(workspaceId || ''), String(ontologyId || '')]);
}

export function acceptsEvolutionResponse({
  currentScope,
  requestScope,
  currentSelection = '',
  requestSelection = '',
}) {
  return (
    currentScope === requestScope &&
    (!requestSelection || currentSelection === requestSelection)
  );
}

export function selectionBoundValue(
  currentSelection,
  boundSelection,
  value,
  fallback = null,
) {
  return Boolean(currentSelection) && currentSelection === boundSelection ?
    value :
    fallback;
}

export function permitsEvolutionWrite({
  readOnly = false,
  currentSelection = '',
  boundSelection = '',
} = {}) {
  return (
    !readOnly &&
    ((!currentSelection && !boundSelection) ||
      (Boolean(currentSelection) && currentSelection === boundSelection))
  );
}

export function buildEvolutionKnowledgeRefresh(reason, ontologyId) {
  return {
    ontologyId: String(ontologyId || ''),
    reason: String(reason || ''),
    followActive: true,
  };
}

export function isSkillDiffReady({
  selectedTaskId = '',
  diffTaskId = '',
  diff = null,
  loading = false,
  error = '',
} = {}) {
  return Boolean(
    selectedTaskId &&
      selectedTaskId === diffTaskId &&
      diff &&
      !loading &&
      !error,
  );
}

export function isActiveSkillTask(task) {
  return activeStatuses.has(String(task?.status || ''));
}

export function canLoadSkillDiff(task) {
  return diffStatuses.has(String(task?.status || ''));
}

export function canDeleteSkillTask(task) {
  return terminalStatuses.has(String(task?.status || ''));
}

export function canCancelSkillTask(
  task,
  { isAdmin = false, username = '' } = {},
) {
  return (
    isActiveSkillTask(task) &&
    (isAdmin || String(task?.triggered_by || '') === String(username || ''))
  );
}

export function isValidVersionName(value) {
  const name = String(value || '').trim();
  return Boolean(name) && /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name);
}

export function buildSkillApprovalPayload(mode, versionName = '') {
  if (mode !== 'overwrite' && mode !== 'new_version') {
    throw new Error('审批模式必须是 overwrite 或 new_version');
  }
  if (mode === 'overwrite') {
    return { mode: 'overwrite' };
  }
  const name = String(versionName || '').trim();
  if (name && !isValidVersionName(name)) {
    throw new Error(
      '版本号仅支持字母、数字、点、下划线和短横线，且必须以字母或数字开头',
    );
  }
  return name ?
    { mode: 'new_version', version_name: name } :
    { mode: 'new_version' };
}

function timestampOf(task) {
  const parsed = Date.parse(task?.updated_at || task?.created_at || '');
  return Number.isFinite(parsed) ? parsed : 0;
}

export function groupSkillTasksByOntology(tasks) {
  const groups = new Map();
  for (const task of Array.isArray(tasks) ? tasks : []) {
    if (!task || typeof task !== 'object') {
      continue;
    }
    const ontologyId = String(task.ontology_id || '').trim() || 'unknown';
    const current = groups.get(ontologyId) || {
      ontologyId,
      ontologyName: String(task.ontology_name || '').trim() || ontologyId,
      tasks: [],
      activeCount: 0,
      approvalCount: 0,
      terminalCount: 0,
      latestAt: 0,
    };
    current.tasks.push(task);
    current.activeCount += isActiveSkillTask(task) ? 1 : 0;
    current.approvalCount += task.status === 'completed' ? 1 : 0;
    current.terminalCount += canDeleteSkillTask(task) ? 1 : 0;
    current.latestAt = Math.max(current.latestAt, timestampOf(task));
    if (task.ontology_name) {
      current.ontologyName = String(task.ontology_name);
    }
    groups.set(ontologyId, current);
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      tasks: [...group.tasks].sort((a, b) => timestampOf(b) - timestampOf(a)),
    }))
    .sort(
      (a, b) =>
        b.latestAt - a.latestAt || a.ontologyName.localeCompare(b.ontologyName),
    );
}

export function reconcileSkillTaskSelection(tasks, selectedId = '') {
  const list = Array.isArray(tasks) ? tasks : [];
  if (selectedId && list.some((task) => task.task_id === selectedId)) {
    return selectedId;
  }
  const approval = list.find((task) => task.status === 'completed');
  if (approval) {
    return approval.task_id;
  }
  const active = list.find(isActiveSkillTask);
  const [firstTask] = list;
  return active?.task_id || firstTask?.task_id || '';
}

function splitLines(value) {
  const text = String(value || '').replace(/\r\n?/g, '\n');
  return text ? text.split('\n') : [];
}

function fallbackLineOperations(before, after) {
  let prefix = 0;
  while (
    prefix < before.length &&
    prefix < after.length &&
    before[prefix] === after[prefix]
  ) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < before.length - prefix &&
    suffix < after.length - prefix &&
    before[before.length - 1 - suffix] === after[after.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  return [
    ...before.slice(0, prefix).map((value) => ({ type: 'equal', value })),
    ...before
      .slice(prefix, before.length - suffix)
      .map((value) => ({ type: 'delete', value })),
    ...after
      .slice(prefix, after.length - suffix)
      .map((value) => ({ type: 'insert', value })),
    ...before
      .slice(before.length - suffix)
      .map((value) => ({ type: 'equal', value })),
  ];
}

function lineOperations(before, after, matrixLimit) {
  if (!before.length) {
    return after.map((value) => ({ type: 'insert', value }));
  }
  if (!after.length) {
    return before.map((value) => ({ type: 'delete', value }));
  }
  if (before.length * after.length > matrixLimit) {
    return fallbackLineOperations(before, after);
  }

  const matrix = Array.from(
    { length: before.length + 1 },
    () => new Uint32Array(after.length + 1),
  );
  for (let oldIndex = before.length - 1; oldIndex >= 0; oldIndex -= 1) {
    for (let newIndex = after.length - 1; newIndex >= 0; newIndex -= 1) {
      matrix[oldIndex][newIndex] =
        before[oldIndex] === after[newIndex] ?
          matrix[oldIndex + 1][newIndex + 1] + 1 :
          Math.max(
            matrix[oldIndex + 1][newIndex],
            matrix[oldIndex][newIndex + 1],
          );
    }
  }

  const operations = [];
  let oldIndex = 0;
  let newIndex = 0;
  while (oldIndex < before.length || newIndex < after.length) {
    if (
      oldIndex < before.length &&
      newIndex < after.length &&
      before[oldIndex] === after[newIndex]
    ) {
      operations.push({ type: 'equal', value: before[oldIndex] });
      oldIndex += 1;
      newIndex += 1;
    } else if (
      oldIndex < before.length &&
      (newIndex >= after.length ||
        matrix[oldIndex + 1][newIndex] >= matrix[oldIndex][newIndex + 1])
    ) {
      operations.push({ type: 'delete', value: before[oldIndex] });
      oldIndex += 1;
    } else {
      operations.push({ type: 'insert', value: after[newIndex] });
      newIndex += 1;
    }
  }
  return operations;
}

function wordTokens(value) {
  return (
    String(value || '').match(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) || []
  );
}

function wordParts(before, after) {
  const oldTokens = wordTokens(before);
  const newTokens = wordTokens(after);
  if (oldTokens.length * newTokens.length > 12000) {
    return {
      before: before ? [{ type: 'removed', text: before }] : [],
      after: after ? [{ type: 'added', text: after }] : [],
    };
  }
  const operations = lineOperations(oldTokens, newTokens, 12000);
  const oldParts = [];
  const newParts = [];
  for (const operation of operations) {
    if (operation.type === 'equal') {
      oldParts.push({ type: 'equal', text: operation.value });
      newParts.push({ type: 'equal', text: operation.value });
    } else if (operation.type === 'delete') {
      oldParts.push({ type: 'removed', text: operation.value });
    } else {
      newParts.push({ type: 'added', text: operation.value });
    }
  }
  return { before: oldParts, after: newParts };
}

function numberedRows(operations) {
  const rows = [];
  let oldNumber = 1;
  let newNumber = 1;
  let index = 0;
  while (index < operations.length) {
    const operation = operations[index];
    if (operation.type === 'equal') {
      rows.push({
        type: 'unchanged',
        before: operation.value,
        after: operation.value,
        oldNumber,
        newNumber,
      });
      oldNumber += 1;
      newNumber += 1;
      index += 1;
      continue;
    }

    const removed = [];
    const added = [];
    while (index < operations.length && operations[index].type !== 'equal') {
      if (operations[index].type === 'delete') {
        removed.push(operations[index].value);
      } else {
        added.push(operations[index].value);
      }
      index += 1;
    }
    const changedCount = Math.max(removed.length, added.length);
    for (let changeIndex = 0; changeIndex < changedCount; changeIndex += 1) {
      const before = removed[changeIndex];
      const after = added[changeIndex];
      if (before !== undefined && after !== undefined) {
        rows.push({
          type: 'changed',
          before,
          after,
          oldNumber,
          newNumber,
          words: wordParts(before, after),
        });
        oldNumber += 1;
        newNumber += 1;
      } else if (before !== undefined) {
        rows.push({
          type: 'removed',
          before,
          after: '',
          oldNumber,
          newNumber: null,
        });
        oldNumber += 1;
      } else {
        rows.push({
          type: 'added',
          before: '',
          after,
          oldNumber: null,
          newNumber,
        });
        newNumber += 1;
      }
    }
  }
  return rows;
}

function collapseUnchangedRows(rows, contextLines, collapseThreshold) {
  const collapsed = [];
  let index = 0;
  while (index < rows.length) {
    if (rows[index].type !== 'unchanged') {
      collapsed.push(rows[index]);
      index += 1;
      continue;
    }
    let end = index;
    while (end < rows.length && rows[end].type === 'unchanged') {
      end += 1;
    }
    const run = rows.slice(index, end);
    const hiddenCount = run.length - contextLines * 2;
    if (hiddenCount >= collapseThreshold) {
      collapsed.push(
        ...run.slice(0, contextLines),
        {
          type: 'collapsed',
          count: hiddenCount,
          oldStart: run[contextLines]?.oldNumber || null,
          newStart: run[contextLines]?.newNumber || null,
        },
        ...run.slice(-contextLines),
      );
    } else {
      collapsed.push(...run);
    }
    index = end;
  }
  return collapsed;
}

export function buildLineWordDiff(original, evolved, options = {}) {
  const before = splitLines(original);
  const after = splitLines(evolved);
  const contextLines = Math.max(1, Number(options.contextLines) || 3);
  const collapseThreshold = Math.max(1, Number(options.collapseThreshold) || 8);
  const matrixLimit = Math.max(1000, Number(options.matrixLimit) || 160000);
  const rows = numberedRows(lineOperations(before, after, matrixLimit));
  return collapseUnchangedRows(rows, contextLines, collapseThreshold);
}

export function groupOperationRecords(records) {
  const grouped = new Map();
  for (const record of Array.isArray(records) ? records : []) {
    const fingerprint = String(record?.fingerprint || '');
    if (!fingerprint) {
      continue;
    }
    const bucket = grouped.get(fingerprint) || [];
    bucket.push(record);
    grouped.set(fingerprint, bucket);
  }
  return grouped;
}

export function filterOperationExport(payload, ontologyId = '') {
  const scope = String(ontologyId || '').trim();
  const operations = (
    Array.isArray(payload?.operations) ? payload.operations : []
  ).filter((operation) => !scope || operation?.summary?.ontology_id === scope);
  return {
    ...payload,
    scope_ontology_id: scope || null,
    operation_count: operations.length,
    aggregate_count: operations.reduce(
      (sum, operation) =>
        sum +
        Number(
          operation?.summary?.aggregate_count ||
            operation?.proposals?.length ||
            0,
        ),
      0,
    ),
    record_count: operations.reduce(
      (sum, operation) =>
        sum + Number(operation?.proposal_records?.length || 0),
      0,
    ),
    operations,
  };
}

export function operationExportFilename(ontologyId = '', now = new Date()) {
  const segment =
    String(ontologyId || 'workspace')
      .trim()
      .replace(/[^A-Za-z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'workspace';
  const stamp = now.toISOString().replace(/[:.]/g, '-');
  return `operation-evolution-${segment}-${stamp}.json`;
}
