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
    }), { kind: 'studio', target: '知识包 Studio', view: 'graph' });
  }
});

test('合成项目首次进入 Studio，快照、历史焦点和活动事件不会覆盖首页', async (t) => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: new URL('http://localhost/workbench.html?project=project_alpha') };
  t.after(() => { globalThis.window = previousWindow; });
  const interaction = { id: 'synthetic-confirmation', kind: 'confirmation' };
  t.mock.method(globalThis, 'fetch', async (input) => {
    const path = String(input);
    const data = path.includes('/snapshot')
      ? { project: { id: 'project_alpha', name: '合成项目' }, files: ['diagrams/synthetic.mmd'], materials: [] }
      : path.includes('/state')
        ? { state: { phase: 'evaluation' }, focus: { kind: 'flow', target: 'diagrams/synthetic.mmd' }, interaction, lastSeq: 5 }
        : path.includes('/diff') ? { files: [{ path: 'rules/synthetic.md' }] }
        : { content: '' };
    return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
  });
  const app = await import('../frontend/src/workbench/lib/appStore.js?test=studio-home');
  const home = { kind: 'studio', target: '知识包 Studio', view: 'graph' };
  assert.deepEqual(app.store.get().focus, home);
  assert.equal(app.store.get().follow, false);
  await app.actions.bootstrap('project_alpha');
  assert.deepEqual(app.store.get().focus, home);
  assert.deepEqual(app.store.get().interaction, interaction);
  app.actions.handleEvent({ eventType: 'focus', seq: 4, target: { kind: 'flow', target: 'diagrams/synthetic.mmd' } });
  assert.deepEqual(app.store.get().focus, home);
  app.actions.handleEvent({ eventType: 'focus', seq: 6, target: { kind: 'flow', target: 'diagrams/synthetic.mmd' } });
  assert.deepEqual(app.store.get().focus, home);
  app.actions.setFollow(true);
  assert.equal(app.store.get().focus.kind, 'flow');
  app.actions.selectPath('diagrams/synthetic.mmd');
  assert.equal(app.store.get().focus.target, 'diagrams/synthetic.mmd');
});

test('Studio 显式条目链接优先于首页，手动选择不被启动快照覆盖', () => {
  const entry = { kind: 'studio', target: '知识包 Studio', view: 'entry', elementId: 'rule:synthetic' };
  assert.deepEqual(resolveBootstrapFocus({ current: { kind: 'empty' }, routeFocus: entry }), entry);
  const manual = { kind: 'file', target: 'rules/synthetic.md' };
  assert.equal(resolveBootstrapFocus({ current: manual, routeFocus: entry }), manual);
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

test('运行恢复之后自动撤掉「需要恢复」卡片', () => {
  // 线上现场：Agent 重启比 Core 快，自动恢复失败落了一张红卡；十几秒后运行
  // 其实已经恢复并继续写文件，红卡却一直挂着。后端此时不再下发 recovery
  // 字段（state.public() 只在 recovery_status 非空时带它），而字段缺席过去被
  // 当成「保持原值」，于是只能靠用户自己点刷新。
  store.set({ recovery: null, lastSeq: 0, replayUntilSeq: 0 });

  actions.handleEvent({ kind: 'state', seq: 1, execution: 'suspended', recovery: { status: 'resume_failed' } });
  assert.deepEqual(store.get().recovery, { status: 'resume_failed' });

  // 仍然挂起时不能清：REST 侧的恢复诊断可能还有更细的信息
  actions.handleEvent({ kind: 'state', seq: 2, execution: 'suspended' });
  assert.deepEqual(store.get().recovery, { status: 'resume_failed' });

  actions.handleEvent({ kind: 'state', seq: 3, execution: 'running' });
  assert.equal(store.get().recovery, null, '重新跑起来就不该再提示需要恢复');

  // 停下来等用户同样不是「需要恢复」
  store.set({ recovery: { status: 'resume_failed' }, lastSeq: 3 });
  actions.handleEvent({ kind: 'state', seq: 4, execution: 'waiting' });
  assert.equal(store.get().recovery, null);
});
