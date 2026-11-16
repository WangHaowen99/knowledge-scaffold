import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import { transformWithEsbuild } from 'vite';
import { enqueueStudioDraftSave } from '../src/lib/studioDraftQueue.js';

const source = readFileSync(new URL('../src/components/StudioEntryEditor.jsx', import.meta.url), 'utf8');
const { code: compiled } = await transformWithEsbuild(source, 'StudioEntryEditor.jsx', {
  loader: 'jsx', format: 'cjs', jsx: 'automatic',
});
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

// Execute the component with controlled hooks, API promises and timers. Each render
// has its own state snapshot, just as React does across asynchronous handlers.
function editorHarness(overrides = {}) {
  const slots = [];
  const effects = [];
  const timers = new Map();
  const calls = [];
  const errors = [];
  let cursor = 0;
  let timerId = 0;
  let tree;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], (value) => { slots[index] = value; }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
    useMemo(fn) { return fn(); },
    useEffect(fn, dependencies) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || dependencies.some((value, i) => value !== previous[i])) {
        effects.push(fn);
        slots[index] = dependencies;
      }
    },
  };
  const api = {
    studioGetDraft: async () => ({ draft: null }),
    studioLease: async () => ({ holder: 'synthetic-user' }),
    studioSaveDraft: async (...args) => { calls.push(['save', args[2]]); },
    studioReleaseLease: async () => { calls.push(['release']); },
    ...overrides,
  };
  const element = (type, props) => ({ type, props });
  const modules = {
    react,
    'react/jsx-runtime': { jsx: element, jsxs: element },
    '@ant-design/icons': {},
    antd: {
      Button: 'Button', Input: { TextArea: 'TextArea' }, Space: 'Space',
      Tag: 'Tag', Spin: 'Spin', Typography: { Title: 'Title' },
      message: { error: (msg) => errors.push(msg), warning() {}, info() {} },
    },
    '../lib/api.js': { api },
    '../lib/studioDraftQueue.js': { enqueueStudioDraftSave },
    '../lib/workspaceDirty.js': { setWorkspaceDirty() {} },
  };
  const context = {
    module: { exports: {} },
    require(name) {
      assert.ok(name in modules, `Unexpected dependency: ${name}`);
      return modules[name];
    },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(compiled, context);
  const props = {
    project: { id: 'synthetic-project' },
    entry: {
      path: 'ontology/object/sample.md', revision: 'baseline',
      schema: [{ title: 'Description', kind: 'text' }],
      sections: [{ title: 'Description', level: 1, content: 'original' }],
    },
    onExit: () => calls.push(['exit']),
  };
  function render() {
    cursor = 0;
    tree = context.module.exports.default(props);
    effects.splice(0).forEach((effect) => effect());
  }
  function find(type, label) {
    function visit(node) {
      if (!node || typeof node !== 'object') return null;
      if (node.type === type && (!label || node.props.children === label)) return node;
      return [node.props?.children].flat(Infinity).map(visit).find(Boolean);
    }
    const node = visit(tree);
    assert.ok(node, `Missing ${type} ${label || ''}`);
    return node;
  }
  render();
  return {
    api, calls, errors, render,
    async ready() { await tick(); render(); },
    edit(value) { find('TextArea').props.onChange({ target: { value } }); render(); },
    exit() { return find('Button', '退出').props.onClick(); },
    exitLoading() { render(); return find('Button', '退出').props.loading; },
    async autosave() {
      const callbacks = [...timers.values()];
      timers.clear();
      return Promise.all(callbacks.map((fn) => fn()));
    },
    savingLabel() {
      render();
      return JSON.stringify(tree).includes('草稿保存中');
    },
  };
}

const loadedDraft = async () => ({ draft: { content: '# Description\n\nexisting draft\n', base_revision: 'baseline' } });

test('read-only editor with a loaded draft exits without saving or releasing another owner lease', async () => {
  const h = editorHarness({ studioGetDraft: loadedDraft, studioLease: async () => { throw Error('lease conflict'); } });
  await h.ready();
  await h.exit();
  assert.deepEqual(h.calls, [['exit']]);
  assert.deepEqual(h.errors, []);
});

test('exit observes a lease denial received after the click', async () => {
  const lease = deferred();
  const h = editorHarness({ studioGetDraft: loadedDraft, studioLease: () => lease.promise });
  await h.ready();
  const exiting = h.exit();
  lease.reject(Error('lease conflict'));
  await exiting;
  assert.deepEqual(h.calls, [['exit']]);
});

test('unchanged exit waits for lease release and never creates a draft', async () => {
  const release = deferred();
  const h = editorHarness({ studioReleaseLease: () => release.promise });
  await h.ready();
  const exiting = h.exit();
  await tick();
  assert.deepEqual(h.calls, []);
  release.resolve();
  await exiting;
  assert.deepEqual(h.calls, [['exit']]);
});

test('autosave failure clears saving and exit retries with the latest edited content', async () => {
  const h = editorHarness({ studioSaveDraft: async () => { throw Error('offline'); } });
  await h.ready();
  h.edit('first change');
  await h.autosave();
  assert.equal(h.savingLabel(), false);
  h.edit('latest change');
  h.api.studioSaveDraft = async (_project, _path, content) => h.calls.push(['save', content]);
  await h.exit();
  assert.deepEqual(h.calls, [['save', '# Description\n\nlatest change\n'], ['release'], ['exit']]);
  assert.deepEqual(h.errors, ['offline']);
});

test('exit during a failing autosave queues a new save and can succeed on the same click', async () => {
  const first = deferred();
  let attempts = 0;
  const h = editorHarness({ studioSaveDraft: async () => {
    attempts += 1;
    if (attempts === 1) await first.promise;
  } });
  await h.ready();
  h.edit('changed');
  const saving = h.autosave();
  await tick();
  h.render();
  const exiting = h.exit();
  first.reject(Error('temporary failure'));
  await Promise.all([saving, exiting]);
  assert.equal(attempts, 2);
  assert.deepEqual(h.calls, [['release'], ['exit']]);
});

test('failed exit preserves edits and lease, clears loading, and allows a later retry', async () => {
  let attempts = 0;
  const h = editorHarness({ studioSaveDraft: async () => {
    attempts += 1;
    throw Error('offline');
  } });
  await h.ready();
  h.edit('retain this');
  await h.autosave();
  h.render();
  await h.exit();
  assert.equal(attempts, 2);
  assert.deepEqual(h.calls, []);
  assert.equal(h.exitLoading(), false);
  h.api.studioSaveDraft = async (_project, _path, content) => h.calls.push(['save', content]);
  await h.exit();
  assert.deepEqual(h.calls, [['save', '# Description\n\nretain this\n'], ['release'], ['exit']]);
});

test('exit saves latest content after an older pending save before releasing the lease', async () => {
  const first = deferred();
  let attempts = 0;
  const h = editorHarness({ studioSaveDraft: async (_project, _path, content) => {
    attempts += 1;
    if (attempts === 1) await first.promise;
    h.calls.push(['save', content]);
  } });
  await h.ready();
  h.edit('older');
  const saving = h.autosave();
  await tick();
  h.edit('newer');
  const exiting = h.exit();
  await tick();
  assert.equal(attempts, 1);
  assert.deepEqual(h.calls, []);
  first.resolve();
  await Promise.all([saving, exiting]);
  assert.deepEqual(h.calls, [
    ['save', '# Description\n\nolder\n'],
    ['save', '# Description\n\nnewer\n'], ['release'], ['exit'],
  ]);
  await h.autosave();
  assert.equal(attempts, 2);
});
