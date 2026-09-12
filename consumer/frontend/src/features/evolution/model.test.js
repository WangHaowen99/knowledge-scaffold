import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildLineWordDiff,
  buildEvolutionKnowledgeRefresh,
  buildSkillApprovalPayload,
  acceptsEvolutionResponse,
  canCancelSkillTask,
  canDeleteSkillTask,
  filterOperationExport,
  evolutionScopeKey,
  groupOperationRecords,
  groupSkillTasksByOntology,
  isSkillDiffReady,
  isActiveSkillTask,
  operationExportFilename,
  permitsEvolutionWrite,
  reconcileSkillTaskSelection,
  selectionBoundValue,
  SKILL_NEW_VERSION_ACTIVATION_COPY,
} from './model.js';

test('new-version approval copy states that approval immediately activates the version', () => {
  assert.match(SKILL_NEW_VERSION_ACTIVATION_COPY, /立即.*激活版本/);
});

test('request scope rejects stale workspace, ontology, and selection responses', () => {
  const first = evolutionScopeKey('workspace-a', 'onto-a');
  const nextWorkspace = evolutionScopeKey('workspace-b', 'onto-a');
  const nextOntology = evolutionScopeKey('workspace-a', 'onto-b');

  assert.equal(
    acceptsEvolutionResponse({ currentScope: first, requestScope: first }),
    true,
  );
  assert.equal(
    acceptsEvolutionResponse({
      currentScope: nextWorkspace,
      requestScope: first,
    }),
    false,
  );
  assert.equal(
    acceptsEvolutionResponse({
      currentScope: nextOntology,
      requestScope: first,
    }),
    false,
  );
  assert.equal(
    acceptsEvolutionResponse({
      currentScope: first,
      requestScope: first,
      currentSelection: 'task-new',
      requestSelection: 'task-old',
    }),
    false,
  );
});

test('selection-bound payloads never expose details from a previous selection', () => {
  const previous = {
    proposals: [{ fingerprint: 'old' }],
    staged: { staged: 'old patch' },
  };
  assert.equal(selectionBoundValue('onto/op-b', 'onto/op-a', previous), null);
  assert.equal(
    selectionBoundValue('onto/op-a', 'onto/op-a', previous),
    previous,
  );
});

test('evolution writes require an active version and matching detail selection', () => {
  assert.equal(permitsEvolutionWrite({ readOnly: true }), false);
  assert.equal(
    permitsEvolutionWrite({
      currentSelection: 'onto/op-a',
      boundSelection: '',
    }),
    false,
  );
  assert.equal(
    permitsEvolutionWrite({
      currentSelection: 'onto/op-b',
      boundSelection: 'onto/op-a',
    }),
    false,
  );
  assert.equal(
    permitsEvolutionWrite({
      currentSelection: 'onto/op-a',
      boundSelection: 'onto/op-a',
    }),
    true,
  );
});

test('operation apply refresh follows the active version after writing it', () => {
  assert.deepEqual(
    buildEvolutionKnowledgeRefresh('operation-apply', 'onto-a'),
    {
      ontologyId: 'onto-a',
      reason: 'operation-apply',
      followActive: true,
    },
  );
});

test('skill diff readiness requires the selected task id to match its response binding', () => {
  const base = { selectedTaskId: 'task-new', diff: { files: [] } };
  assert.equal(isSkillDiffReady({ ...base, diffTaskId: 'task-old' }), false);
  assert.equal(
    isSkillDiffReady({ ...base, diffTaskId: 'task-new', loading: true }),
    false,
  );
  assert.equal(
    isSkillDiffReady({ ...base, diffTaskId: 'task-new', error: 'failed' }),
    false,
  );
  assert.equal(isSkillDiffReady({ ...base, diffTaskId: 'task-new' }), true);
});

test('skill tasks group by ontology and expose lifecycle counts', () => {
  const groups = groupSkillTasksByOntology([
    {
      task_id: 'terminal',
      ontology_id: 'onto-a',
      ontology_name: 'A',
      status: 'approved',
      updated_at: '2026-01-01T00:00:00Z',
    },
    {
      task_id: 'active',
      ontology_id: 'onto-a',
      ontology_name: 'A',
      status: 'running',
      updated_at: '2026-01-03T00:00:00Z',
    },
    {
      task_id: 'approval',
      ontology_id: 'onto-b',
      ontology_name: 'B',
      status: 'completed',
      updated_at: '2026-01-02T00:00:00Z',
    },
  ]);

  assert.deepEqual(
    groups.map((group) => group.ontologyId),
    ['onto-a', 'onto-b'],
  );
  assert.deepEqual(
    groups[0].tasks.map((task) => task.task_id),
    ['active', 'terminal'],
  );
  assert.equal(groups[0].activeCount, 1);
  assert.equal(groups[0].terminalCount, 1);
  assert.equal(groups[1].approvalCount, 1);
});

test('task lifecycle follows backend terminal and ownership contracts', () => {
  const running = { status: 'running', triggered_by: 'alice' };
  assert.equal(isActiveSkillTask(running), true);
  assert.equal(canDeleteSkillTask({ status: 'completed' }), false);
  assert.equal(canDeleteSkillTask({ status: 'failed' }), true);
  assert.equal(canCancelSkillTask(running, { username: 'alice' }), true);
  assert.equal(canCancelSkillTask(running, { username: 'bob' }), false);
  assert.equal(
    canCancelSkillTask(running, { username: 'bob', isAdmin: true }),
    true,
  );
});

test('selection keeps current task then prefers approval and active tasks', () => {
  const tasks = [
    { task_id: 'old', status: 'approved' },
    { task_id: 'active', status: 'running' },
    { task_id: 'approval', status: 'completed' },
  ];
  assert.equal(reconcileSkillTaskSelection(tasks, 'old'), 'old');
  assert.equal(reconcileSkillTaskSelection(tasks, 'missing'), 'approval');
  assert.equal(reconcileSkillTaskSelection(tasks.slice(0, 2), ''), 'active');
});

test('approval payload supports overwrite and named or automatic new versions', () => {
  assert.deepEqual(buildSkillApprovalPayload('overwrite', 'ignored'), {
    mode: 'overwrite',
  });
  assert.deepEqual(buildSkillApprovalPayload('new_version', ''), {
    mode: 'new_version',
  });
  assert.deepEqual(buildSkillApprovalPayload('new_version', 'evol-2026.1'), {
    mode: 'new_version',
    version_name: 'evol-2026.1',
  });
  assert.throws(
    () => buildSkillApprovalPayload('new_version', '../unsafe'),
    /版本号/,
  );
  assert.throws(() => buildSkillApprovalPayload('copy'), /审批模式/);
});

test('line diff marks changed words and collapses long unchanged runs', () => {
  const unchanged = Array.from(
    { length: 18 },
    (_, index) => `line ${index + 1}`,
  );
  const original = ['header', ...unchanged, 'cost is 10 yuan', 'tail'].join(
    '\n',
  );
  const evolved = ['header', ...unchanged, 'cost is 20 yuan', 'tail'].join(
    '\n',
  );
  const rows = buildLineWordDiff(original, evolved, {
    contextLines: 2,
    collapseThreshold: 4,
  });
  const collapsed = rows.find((row) => row.type === 'collapsed');
  const changed = rows.find((row) => row.type === 'changed');

  assert.ok(collapsed);
  assert.ok(collapsed.count >= 10);
  assert.ok(
    changed.words.before.some(
      (part) => part.type === 'removed' && part.text === '10',
    ),
  );
  assert.ok(
    changed.words.after.some(
      (part) => part.type === 'added' && part.text === '20',
    ),
  );
});

test('line diff preserves added and removed line numbers', () => {
  const rows = buildLineWordDiff('a\nb\nc', 'a\nnew\nc');
  const changed = rows.find((row) => row.type === 'changed');
  assert.equal(changed.oldNumber, 2);
  assert.equal(changed.newNumber, 2);
  assert.equal(changed.before, 'b');
  assert.equal(changed.after, 'new');
});

test('operation records group by fingerprint and export is narrowed to ontology', () => {
  const grouped = groupOperationRecords([
    { id: 'r1', fingerprint: 'fp-a' },
    { id: 'r2', fingerprint: 'fp-a' },
    { id: 'r3', fingerprint: 'fp-b' },
  ]);
  assert.equal(grouped.get('fp-a').length, 2);

  const filtered = filterOperationExport(
    {
      schema: 'operation-evolution-proposals.v1',
      operations: [
        {
          summary: { ontology_id: 'onto-a', aggregate_count: 2 },
          proposals: [{}, {}],
          proposal_records: [{}, {}, {}],
        },
        {
          summary: { ontology_id: 'onto-b', aggregate_count: 1 },
          proposals: [{}],
          proposal_records: [{}],
        },
      ],
    },
    'onto-a',
  );
  assert.equal(filtered.operation_count, 1);
  assert.equal(filtered.aggregate_count, 2);
  assert.equal(filtered.record_count, 3);
  assert.equal(filtered.operations[0].summary.ontology_id, 'onto-a');
  assert.match(
    operationExportFilename('onto/a', new Date('2026-01-01T01:02:03Z')),
    /^operation-evolution-onto-a-/,
  );
});
