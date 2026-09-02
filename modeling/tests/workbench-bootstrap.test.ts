import assert from 'node:assert/strict';
import test from 'node:test';

const {
  actions,
  resolveBootstrapFocus,
  resolveWorkbenchBootstrapResults,
  store,
} = await import('../frontend/src/workbench/lib/appStore.js');

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

test('Workbench 仅在评测阶段将发布门禁焦点替换为正式评测', () => {
  const gateFocus = { kind: 'gates', target: '正式发布门禁' };

  assert.deepEqual(resolveBootstrapFocus({
    current: { kind: 'empty', target: '' },
    agentFocus: gateFocus,
    workflowPhase: 'evaluation',
    initialPath: 'manifest.yaml',
  }), { kind: 'evaluation', target: '正式评测' });

  assert.deepEqual(resolveBootstrapFocus({
    current: { kind: 'empty', target: '' },
    workflowPhase: 'evaluation',
    initialPath: 'manifest.yaml',
  }), { kind: 'evaluation', target: '正式评测' });

  for (const workflowPhase of [
    'intake', 'goal', 'process', 'knowledge', 'increment', 'validation', 'release',
  ]) {
    assert.deepEqual(resolveBootstrapFocus({
      current: { kind: 'empty', target: '' },
      agentFocus: gateFocus,
      workflowPhase,
      initialPath: 'manifest.yaml',
    }), { kind: 'file', target: 'manifest.yaml' });
  }
});

test('Workbench 实时门禁焦点不会让建模阶段提前进入评测', () => {
  store.set({
    workflow: { phase: 'knowledge' },
    focus: { kind: 'file', target: 'manifest.yaml' },
    follow: true,
    pendingFocus: null,
    files: ['manifest.yaml'],
    materials: [],
    candidateDiff: [],
  });

  actions.agentFocus({ kind: 'gates', target: '正式发布门禁' });
  assert.deepEqual(store.get().focus, { kind: 'file', target: 'manifest.yaml' });

  actions.focusFrom({ kind: 'gates', target: '正式发布门禁' });
  assert.deepEqual(store.get().focus, { kind: 'file', target: 'manifest.yaml' });

  store.set({ workflow: { phase: 'evaluation' } });
  actions.agentFocus({ kind: 'gates', target: '正式发布门禁' });
  assert.deepEqual(store.get().focus, {
    kind: 'evaluation', target: '正式评测', source: 'agent',
  });
});
