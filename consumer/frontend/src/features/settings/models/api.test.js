import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  loadGlobalModels,
  loadWorkspaceModels,
  loadWorkspacePrivateModels,
  loadModelProfileReferences,
  loadWorkspaceRuntime,
  requestModelJson,
  saveGlobalModels,
  saveWorkspaceModelRefs,
  saveWorkspaceModels,
  testGlobalModel,
  testWorkspaceModel,
} from './api.js';

const currentDir = dirname(fileURLToPath(import.meta.url));
const modelSettingsSource = readFileSync(join(currentDir, 'ModelSettings.jsx'), 'utf8');

const response = (payload, { ok = true, status = 200, headers = {} } = {}) => ({
  ok,
  status,
  headers: new Headers(headers),
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
    if (url === '/api/system-config' && !options.method) {
      return response(
        { llm_profiles: { profiles: [{ id: 'fast' }] } },
        {
          headers: {
            'x-system-config-generation': '17',
            'x-llm-profiles-fingerprint': 'sha256:config-fast',
          },
        },
      );
    }
    return response({ success: true });
  };
  assert.deepEqual(await loadGlobalModels({ request }), {
    config: { profiles: [{ id: 'fast' }] },
    snapshot: { generation: 17, fingerprint: 'sha256:config-fast' },
  });
  const precondition = {
    config_generation: 17,
    config_fingerprint: 'sha256:config-fast',
    reference_generation: 'sha256:references-fast',
    removed_profile_ids: ['legacy'],
    renames: [{ original_id: 'legacy', new_id: 'fast' }],
  };
  await saveGlobalModels({ profiles: [{ id: 'fast' }] }, { request, precondition });
  await testGlobalModel({ profiles: [{ id: 'fast' }], profile_id: 'fast' }, { request });

  assert.equal(calls[1].url, '/api/system-config');
  assert.equal(calls[1].options.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    section: 'llm_profiles',
    data: { profiles: [{ id: 'fast' }] },
    precondition,
  });
  assert.equal(calls[2].url, '/api/system-config/test');
  assert.equal(calls[2].options.method, 'POST');
  assert.equal(JSON.parse(calls[2].options.body).section, 'llm_profiles');
  assert.equal(JSON.parse(calls[2].options.body).data.profile_id, 'fast');
});

test('global API keeps missing concurrency headers unknown instead of coercing generation zero', async () => {
  const result = await loadGlobalModels({
    request: async () => response({ llm_profiles: { profiles: [] } }),
  });

  assert.deepEqual(result.snapshot, { generation: null, fingerprint: '' });
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

test('reference inventory uses one aggregate request and normalizes the authoritative generation', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    return response({
      complete: true,
      generation: 'sha256:inventory-a',
      entries: [{
        workspace_id: 'alpha',
        workspace_name: 'Alpha',
        config_generation: 7,
        references: [{ field: 'default_main_profile_ref', scope: 'global', profile_id: 'fast' }],
      }],
    });
  };
  const inventory = await loadModelProfileReferences('global:fast', {
    request,
  });
  assert.deepEqual(calls, ['/api/model-profile-references?profile_id=global%3Afast']);
  assert.equal(inventory.complete, true);
  assert.equal(inventory.generation, 'sha256:inventory-a');
  assert.equal(inventory.entries[0].runtimeKnown, true);
  assert.equal(inventory.entries[0].workspaceId, 'alpha');
  assert.equal(inventory.entries[0].references[0].profile_id, 'fast');
});

test('aggregate reference inventory preserves fail-closed diagnostics', async () => {
  const inventory = await loadModelProfileReferences('global:fast', {
    request: async () => response({
      complete: false,
      generation: 'sha256:inventory-b',
      entries: [{
        workspace_id: 'broken',
        workspace_name: 'Broken',
        config_generation: null,
        references: [],
        diagnostic: { code: 'runtime_config_invalid', message: 'not verified' },
      }],
    }),
  });

  assert.equal(inventory.complete, false);
  assert.equal(inventory.entries[0].runtimeKnown, false);
  assert.equal(inventory.entries[0].diagnostic.code, 'runtime_config_invalid');
});

test('global model first paint skips inventory and destructive ID changes share the aggregate preflight', () => {
  const loadStart = modelSettingsSource.indexOf('const load = useCallback');
  const loadEnd = modelSettingsSource.indexOf('useEffect(() => {', loadStart);
  const initialLoad = modelSettingsSource.slice(loadStart, loadEnd);

  assert.equal(initialLoad.includes('loadModelProfileReferences'), false);
  assert.equal(modelSettingsSource.includes('loadModelProfileReferences(undefined'), true);
  assert.equal(modelSettingsSource.includes('skipReferencePreflight'), false);
  assert.equal(modelSettingsSource.includes('globalProfileMutationDecision'), true);
  assert.equal(modelSettingsSource.includes('profile.id !== originalId'), true);
  assert.equal(modelSettingsSource.includes('disabled={isEdit}'), false);
});
