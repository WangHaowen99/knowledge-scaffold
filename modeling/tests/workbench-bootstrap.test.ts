import assert from 'node:assert/strict';
import test from 'node:test';

const { resolveWorkbenchBootstrapResults } = await import('../frontend/src/workbench/lib/appStore.js');

test('Workbench 在 Agent 不可用时仍保留知识包快照', () => {
  const snapshot = {
    project: { id: 'project_alpha', name: '合成知识包项目' },
    files: ['manifest.yaml', 'objects/record.md'],
    materials: [],
  };
  const result = resolveWorkbenchBootstrapResults([
    { status: 'fulfilled', value: snapshot },
    { status: 'rejected', reason: new Error('建模助手暂时不可用') },
    { status: 'fulfilled', value: { files: [] } },
  ]);

  assert.equal(result.snapshot, snapshot);
  assert.equal(result.agentAvailable, false);
  assert.deepEqual(result.agent, {});
  assert.match(result.warning, /建模助手暂时不可用/u);
});

test('Workbench 只在知识包快照失败时阻断启动', () => {
  const failure = new Error('知识包快照读取失败');
  assert.throws(() => resolveWorkbenchBootstrapResults([
    { status: 'rejected', reason: failure },
    { status: 'fulfilled', value: {} },
    { status: 'fulfilled', value: { files: [] } },
  ]), failure);
});
