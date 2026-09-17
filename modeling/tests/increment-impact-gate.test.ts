import assert from 'node:assert/strict';
import test from 'node:test';

import { incrementImpactGate } from '../src/agent/core-service.js';
import type { StoredMaterialImpact } from '../src/documents/material-impact.js';

/* 增量轮次的目标只能来自这道门禁。它算错一档，模型本轮就会去做别的事：
   范围还没界定就先发确认卡，或者反过来，改完了还在反复界定范围。 */

const files = {
  'objects/customer.md': 'key: customer\n',
  'operations/settle.md': 'key: settle\n',
  'rules/discount.md': 'key: discount\n',
  'objects/index.md': '',
};

function impact(overrides: Partial<StoredMaterialImpact> = {}): StoredMaterialImpact {
  return {
    id: 'impact_1',
    projectId: 'p1',
    sourceKey: '结算口径说明',
    changeKind: 'replaced',
    createdAt: '2026-09-10T00:00:00.000Z',
    semanticChanged: true,
    scopeResolved: true,
    affectedResources: [{ kind: 'object', key: 'customer' }],
    staleWorkflowSteps: [],
    ...overrides,
  };
}

test('增量门禁第一档：范围未界定时只让先界定影响范围', () => {
  const gate = incrementImpactGate(
    [impact({ scopeResolved: false, affectedResources: [] })],
    files,
    new Set(),
    new Map(),
  );
  assert.equal(gate.pass, false);
  assert.match(gate.agentActionable![0], /analyze_material_impact/u);
  assert.match(gate.agentActionable![0], /direct_resources/u);
  // 只给一项：_turn_goal 取 items[0]，多给会让模型同时开几个头
  assert.equal(gate.agentActionable!.length, 1);
  assert.match(gate.blockers[0], /结算口径说明/u);
});

test('增量门禁第二档：范围已界定但受影响内容没改，只派受影响的那些', () => {
  const gate = incrementImpactGate(
    [impact({
      affectedResources: [
        { kind: 'object', key: 'customer' },
        { kind: 'rule', key: 'discount' },
      ],
    })],
    files,
    new Set(),
    new Map(),
  );
  assert.equal(gate.pass, false);
  const goal = gate.agentActionable![0];
  assert.match(goal, /objects\/customer\.md/u);
  assert.match(goal, /rules\/discount\.md/u);
  // 没被影响的 operations/settle.md 不能出现在目标里
  assert.doesNotMatch(goal, /operations\/settle\.md/u);
  assert.match(goal, /未受影响的正式内容保持不变/u);
});

test('增量门禁第三档：改动已暂存时让发确认卡，不再重复派修改', () => {
  const gate = incrementImpactGate(
    [impact()],
    files,
    new Set(['objects/customer.md']),
    new Map(),
  );
  assert.equal(gate.pass, false);
  assert.match(gate.agentActionable![0], /发起确认卡/u);
  assert.doesNotMatch(gate.agentActionable![0], /形成候选/u);
});

test('受影响内容在影响记录之后被确认过就算处理完，门禁通过', () => {
  const gate = incrementImpactGate(
    [impact()],
    files,
    new Set(),
    new Map([['objects/customer.md', '2026-09-11T00:00:00.000Z']]),
  );
  assert.equal(gate.pass, true);
  assert.deepEqual(gate.blockers, []);
  assert.deepEqual(gate.agentActionable, []);
});

test('影响记录之前的旧确认不算处理，否则存量基线会让门禁直接通过', () => {
  const gate = incrementImpactGate(
    [impact()],
    files,
    new Set(),
    new Map([['objects/customer.md', '2026-09-09T00:00:00.000Z']]),
  );
  assert.equal(gate.pass, false);
  assert.match(gate.agentActionable![0], /objects\/customer\.md/u);
});

test('受影响资源已被删除时不卡住增量', () => {
  const gate = incrementImpactGate(
    [impact({ affectedResources: [{ kind: 'object', key: 'removed_object' }] })],
    files,
    new Set(),
    new Map(),
  );
  assert.equal(gate.pass, true);
});

test('多条影响记录里只要有一条没界定范围，就先界定再谈修改', () => {
  const gate = incrementImpactGate(
    [
      impact({ id: 'impact_done', affectedResources: [{ kind: 'rule', key: 'discount' }] }),
      impact({ id: 'impact_new', sourceKey: '新增台账', scopeResolved: false, affectedResources: [] }),
    ],
    files,
    new Set(),
    new Map(),
  );
  assert.equal(gate.pass, false);
  assert.match(gate.agentActionable![0], /新增台账/u);
  // blockers 面向用户，要把两件事都列出来
  assert.equal(gate.blockers.length, 2);
});
