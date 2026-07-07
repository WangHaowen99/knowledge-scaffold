import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildReasoningDetailGroups,
  getReasoningDetailEntryKey,
} from './reasoningDetailGroups.js';

test('process-only subagent groups get unique detail entry keys', () => {
  const { groups } = buildReasoningDetailGroups([
    {
      type: 'subagent_group',
      groupId: 'group-a',
      meta: { iteration: 1 },
      children: {},
    },
    {
      type: 'subagent_group',
      groupId: 'group-b',
      meta: { iteration: 2 },
      children: {},
    },
  ]);

  const entries = groups.map((group, groupIdx) => ({
    kind: 'group',
    groupIdx,
    globalIndex: group.localVisibleIndex == null ? null : group.localVisibleIndex,
    processSegs: group.processSegs,
    contentSegs: group.contentSegs,
    actualIteration: group.actualIteration,
  }));

  const keys = entries.map((entry, entryIdx) => getReasoningDetailEntryKey(entry, entryIdx));

  assert.deepEqual(
    entries.map((entry) => entry.globalIndex),
    [null, null]
  );
  assert.equal(new Set(keys).size, keys.length);
  assert.ok(keys.every((key) => key !== null && key !== 'null'));
});
