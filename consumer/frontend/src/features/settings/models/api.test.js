import assert from 'node:assert/strict';
import test from 'node:test';

import {
  loadGlobalModels,
  loadWorkspaceModels,
  loadWorkspacePrivateModels,
  loadWorkspaceReferenceInventory,
  loadWorkspaceRuntime,
  requestModelJson,
  saveGlobalModels,
  saveWorkspaceModelRefs,
  saveWorkspaceModels,
  testGlobalModel,
  testWorkspaceModel,
} from './api.js';

const response = (payload, { ok = true, status = 200 } = {}) => ({
  ok,
  status,
  json: async () => payload,
});

test('request adapter preserves backend detail, status, and code', async () => {
  await assert.rejects(
    () => requestModelJson(async () => response({ detail: '模型引用无效', code: 'BAD_REF' }, { ok: false, status: 400 }), '/models'),
    (error) => error.message === '模型引用无效' && error.status === 400 && error.code === 'BAD_REF',
  );
});

test('global API uses llm_profiles section for save and unsaved test', async () => {
  const calls = [];
  const request = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === '/api/system-config' && !options.method) return response({ llm_profiles: { profiles: [{ id: 'fast' }] } });
    return response({ success: true });
  };
  assert.deepEqual(await loadGlobalModels({ request }), { profiles: [{ id: 'fast' }] });
  await saveGlobalModels({ profiles: [{ id: 'fast' }] }, { request });
  await testGlobalModel({ profiles: [{ id: 'fast' }], profile_id: 'fast' }, { request });

  assert.equal(calls[1].url, '/api/system-config');
  assert.equal(calls[1].options.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    section: 'llm_profiles',
    data: { profiles: [{ id: 'fast' }] },
  });
  assert.equal(calls[2].url, '/api/system-config/test');
  assert.equal(calls[2].options.method, 'POST');
  assert.equal(JSON.parse(calls[2].options.body).section, 'llm_profiles');
  assert.equal(JSON.parse(calls[2].options.body).data.profile_id, 'fast');
});

test('workspace API loads available, private, and runtime configs from exact routes', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    if (url === '/agentic/model-profiles') return response({ profiles: [{ id: 'global:fast' }] });
    if (url.endsWith('/llm-profiles')) return response({ profiles: [{ id: 'private' }] });
    return response({ llm: { default_main_profile_ref: { scope: 'global', id: 'fast' } } });
  };
  const bundle = await loadWorkspaceModels('space / a', { request });
  assert.deepEqual(calls, [
    '/agentic/model-profiles',
    '/agentic/workspaces/space%20%2F%20a/llm-profiles',
    '/agentic/workspaces/space%20%2F%20a/runtime-config',
  ]);
  assert.equal(bundle.privateProfiles[0].id, 'private');
  assert.equal(bundle.runtime.llm.default_main_profile_ref.id, 'fast');
});

test('workspace runtime reference refresh uses the scoped GET endpoint', async () => {
  const calls = [];
  const request = async (url, options) => {
    calls.push({ url, options });
    return response({ llm: { default_task_profile_ref: { scope: 'workspace', id: 'private' } } });
  };
  const runtime = await loadWorkspaceRuntime('alpha', { request });
  assert.equal(runtime.llm.default_task_profile_ref.id, 'private');
  assert.equal(calls[0].url, '/agentic/workspaces/alpha/runtime-config');
  assert.equal(calls[0].options.method, undefined);
});

test('workspace private model snapshot refresh uses the scoped GET endpoint', async () => {
  const calls = [];
  const request = async (url, options) => {
    calls.push({ url, options });
    return response({ profiles: [{ id: 'private' }] });
  };
  const profiles = await loadWorkspacePrivateModels('alpha', { request });
  assert.deepEqual(profiles, [{ id: 'private' }]);
  assert.equal(calls[0].url, '/agentic/workspaces/alpha/llm-profiles');
  assert.equal(calls[0].options.method, undefined);
});

test('workspace save, test, and role refs use PATCH/POST contracts without DELETE', async () => {
  const calls = [];
  const request = async (url, options) => { calls.push({ url, options }); return response({ success: true }); };
  await saveWorkspaceModels('alpha', { profiles: [] }, { request });
  await testWorkspaceModel('alpha', { profiles: [{ id: 'private' }], profile_id: 'private' }, { request });
  await saveWorkspaceModelRefs('alpha', {
    default_main_profile_ref: { scope: 'workspace', id: 'private' },
    default_task_profile_ref: null,
  }, { request });

  assert.deepEqual(calls.map((call) => [call.url, call.options.method]), [
    ['/agentic/workspaces/alpha/llm-profiles', 'PATCH'],
    ['/agentic/workspaces/alpha/llm-profiles/test', 'POST'],
    ['/agentic/workspaces/alpha/runtime-config', 'PATCH'],
  ]);
  assert.deepEqual(JSON.parse(calls[0].options.body), { profiles: [] });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    llm: {
      default_main_profile_ref: { scope: 'workspace', id: 'private' },
      default_task_profile_ref: null,
    },
  });
});

test('reference inventory can use provided workspaces and fails closed on any runtime error', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    if (url.includes('/broken/')) return response({ detail: 'Forbidden' }, { ok: false, status: 403 });
    return response({ llm: {} });
  };
  const inventory = await loadWorkspaceReferenceInventory({
    request,
    workspaces: [{ id: 'alpha', name: 'Alpha' }, { id: 'broken', name: 'Broken' }],
  });
  assert.deepEqual(calls, [
    '/agentic/workspaces/alpha/runtime-config',
    '/agentic/workspaces/broken/runtime-config',
  ]);
  assert.equal(inventory.complete, false);
  assert.equal(inventory.entries[0].runtimeKnown, true);
  assert.equal(inventory.entries[1].runtimeKnown, false);
  assert.match(inventory.entries[1].error, /Forbidden/);
});

test('reference inventory loads workspaces itself when props do not provide them', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    if (url === '/agentic/workspaces') return response({ workspaces: [{ id: 'alpha', name: 'Alpha' }] });
    return response({ llm: {} });
  };
  const inventory = await loadWorkspaceReferenceInventory({ request });
  assert.deepEqual(calls, ['/agentic/workspaces', '/agentic/workspaces/alpha/runtime-config']);
  assert.equal(inventory.complete, true);
});

test('an empty workspaces prop is not trusted as a complete deletion inventory', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    if (url === '/agentic/workspaces') return response({ workspaces: [] });
    throw new Error(`unexpected request ${url}`);
  };
  const inventory = await loadWorkspaceReferenceInventory({ request, workspaces: [] });
  assert.deepEqual(calls, ['/agentic/workspaces']);
  assert.deepEqual(inventory, { complete: true, entries: [] });
});
