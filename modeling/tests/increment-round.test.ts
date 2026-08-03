import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assertIncrementChangesAllowed, buildIncrementRequirement, IncrementRemovalNotAllowedError,
} from '../src/ai-construction/increment.js';
import type { PackageSpec } from '../src/types.js';

const spec = {
  rules: [{
    key: 'Eligibility', title: '准入规则', content: '资产达标才可推荐',
    conditions: [{ condition: '资产 ≥ 100 万', conclusion: '可推荐高阶产品' }],
  }],
} as unknown as PackageSpec;

test('提示词把边界写死：只动本轮资料涉及的内容', () => {
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '新规则.md', content: '门槛调整通知' }],
    affected: [],
  });
  assert.match(requirement, /增量补充/);
  assert.match(requirement, /《新规则\.md》/);
  assert.match(requirement, /门槛调整通知/);
  assert.match(requirement, /与本轮资料无关的内容一律不要动/);
  assert.match(requirement, /不得使用 remove/);
  assert.match(requirement, /有就用 modify 改它/);
  // 拿不出依据的改动不该提——否则增量会变成模型的自由发挥
  assert.match(requirement, /无法追溯到资料的改动不要提/);
});

test('带上相关已有内容，模型才可能提出「怎么改」而不是重写一遍', () => {
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '通知.md' }],
    affected: [{ targetKind: 'rule', targetKey: 'Eligibility', action: 'modify' }],
    spec,
  });
  assert.match(requirement, /rule Eligibility 当前内容/);
  assert.match(requirement, /资产 ≥ 100 万/);
  assert.match(requirement, /语义重复的规则/);
});

test('没有新资料时也能构造需求，不产出空指令', () => {
  const requirement = buildIncrementRequirement({ materials: [], affected: [] });
  assert.match(requirement, /本轮没有新增资料/);
});

test('服务端挡下删除，而不是静默过滤', () => {
  const changes = [
    { targetKind: 'rule' as const, targetKey: 'A', action: 'modify' },
    { targetKind: 'rule' as const, targetKey: 'B', action: 'remove' },
  ];
  // 静默丢弃会让专家以为方案已经完整，实际少了一部分——所以抛错而不是过滤
  assert.throws(() => assertIncrementChangesAllowed(changes),
    (error: unknown) => error instanceof IncrementRemovalNotAllowedError);
  try {
    assertIncrementChangesAllowed(changes);
  } catch (error) {
    // 报错要说清替代做法，否则又是一条没有出口的红字
    assert.match((error as Error).message, /B/);
    assert.match((error as Error).message, /修改它们的适用条件或例外/);
  }
});

test('全是新增和修改时放行', () => {
  assert.doesNotThrow(() => assertIncrementChangesAllowed([
    { targetKind: 'rule', targetKey: 'A', action: 'add' },
    { targetKind: 'operation', targetKey: 'B', action: 'modify' },
  ]));
  assert.doesNotThrow(() => assertIncrementChangesAllowed([]));
});

// ---- 增量后必须重新评测：靠既有的发布门禁保证，不另起一套 ----

test('知识包改动之后，旧评测轮次不再算通过', async () => {
  const { buildReleaseReadiness } = await import('../src/workflow/release.js');
  const base = {
    aiConstructionCompleted: true,
    packageCheck: { revision: 'rev-2', findings: [] },
    currentRevision: 'rev-2',
    pendingRequiredTasks: [],
    pendingCandidateChanges: [],
    latestResults: [{ verdict: 'pass' }],
    requiredTestcases: [{ id: 't1' }],
  } as any;

  // 上一轮评测跑在 rev-1 上，增量把知识包推到了 rev-2
  const stale = buildReleaseReadiness({
    ...base,
    latestRound: { status: 'done', artifactRevision: 'rev-1' },
  });
  const staleGate = stale.gates.find((gate: any) => gate.id === 'evaluation');
  assert.equal(staleGate?.passed, false, '增量之后旧评测不能算数');
  assert.match(staleGate!.detail, /重新评测/);
  assert.equal(stale.ready, false, '发布必须被挡住');

  // 对当前修订重新评测过才放行
  const fresh = buildReleaseReadiness({
    ...base,
    latestRound: { status: 'done', artifactRevision: 'rev-2' },
  });
  assert.equal(fresh.gates.find((gate: any) => gate.id === 'evaluation')?.passed, true);
});

// ---- 前端入口：不摆一个点了必然被拒的按钮 ----

test('建模没确认完成时不显示补充入口', async () => {
  const { incrementEntryState } = await import('../frontend/src/incrementEntry.js');
  assert.equal(incrementEntryState({
    milestones: [{ id: 'construction_completed', status: 'active' }], tasks: [],
  }).visible, false);
  assert.equal(incrementEntryState(undefined).visible, false);
});

test('建完之后常驻「补充新资料」', async () => {
  const { incrementEntryState } = await import('../frontend/src/incrementEntry.js');
  const entry = incrementEntryState({
    milestones: [{ id: 'construction_completed', status: 'confirmed' }], tasks: [],
  });
  assert.equal(entry.visible, true);
  assert.equal(entry.action, 'start_increment_round');
});

test('已在一轮增量里就换成「生成方案」，不会再开一轮', async () => {
  const { incrementEntryState } = await import('../frontend/src/incrementEntry.js');
  const inRound = incrementEntryState({
    milestones: [
      { id: 'construction_completed', status: 'confirmed' },
      { id: 'increment_confirmed', status: 'active' },
    ],
    tasks: [{ kind: 'incremental_intake', status: 'pending' }],
  });
  assert.equal(inRound.action, 'propose_increment');
});

test('入口动作都在助手动作目录里登记过', async () => {
  const { ASSISTANT_ACTION_KINDS } = await import('../src/ai-construction/assistant-actions.js');
  for (const id of ['start_increment_round', 'propose_increment']) {
    assert.ok(ASSISTANT_ACTION_KINDS[id], `${id} 未登记，会变成点了没反应的按钮`);
  }
});

test('需求文本必须带上资料正文——只给文件名模型会直接放弃', () => {
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '门槛调整通知.md', content: '资产门槛由 100 万上调至 500 万。' }],
    affected: [],
  });
  // 实测教训：只给文件名时模型回「您提到本轮资料为…但未附上具体内容」然后不产出方案
  assert.match(requirement, /《门槛调整通知\.md》/);
  assert.match(requirement, /资产门槛由 100 万上调至 500 万/);
});

test('长文档不塞正文，改为给大纲并要求模型自己检索', () => {
  // 真实事故：20 万字手册前 5290 字符全是目录，截取开头喂给模型，
  // 它读不到任何产品条款，得出「资料只提供了目录、没有具体正文」的错误结论。
  const body = [
    '# 目录', ...Array.from({ length: 60 }, (_, i) => `${i + 1}. 第 ${i + 1} 章`),
    '# 前言', 'x'.repeat(5_000),
    '# 第三章 产品条款', '大额存单起存金额为 20 万元。',
  ].join('\n');
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '产品手册.docx', content: body }],
    affected: [],
  });

  assert.match(requirement, /共 \d+ 字符（正文未附/);
  assert.match(requirement, /章节：/, '要给大纲，让模型知道去哪找');
  assert.match(requirement, /search_documents/);
  assert.match(requirement, /read_document/);
  // 关键：不能把正文整段塞进去，也不能让提示词随文档长度膨胀
  assert.ok(requirement.length < 4_000, `提示词过长：${requirement.length}`);
  assert.ok(!requirement.includes('x'.repeat(2_000)), '不应把正文大段塞进提示词');
});

test('短资料仍然直接给全文，不必让模型多跑一趟工具', () => {
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '通知.md', content: '门槛由 100 万上调至 500 万。' }],
    affected: [],
  });
  assert.match(requirement, /全文：/);
  assert.match(requirement, /门槛由 100 万上调至 500 万/);
});

test('没有正文的资料如实说明，不假装有内容', () => {
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '客户表.xlsx' }], affected: [],
  });
  assert.match(requirement, /这份资料没有可读正文/);
});
