import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildGlobalModelPayload,
  buildWorkspaceModelPayload,
  bindProfileTestResult,
  createProfileDraft,
  deletionSnapshotDecision,
  encodeProfileRef,
  globalProfileDeleteDecision,
  isProfileTestResultCurrent,
  normalizeGlobalModelConfig,
  parseProfileRef,
  profileFingerprint,
  profileToPayload,
  workspaceProfileDeleteDecision,
} from './modelConfig.js';

const profile = (id, extra = {}) => ({
  id,
  name: `Model ${id}`,
  provider: 'openai_compatible',
  base_url: `https://${id}.example/v1`,
  model: `${id}-model`,
  timeout: 60,
  max_tokens: 4096,
  temperature: 0.7,
  enabled: true,
  ...extra,
});

test('profile refs accept scoped objects, scoped strings, and legacy global ids', () => {
  assert.deepEqual(parseProfileRef({ scope: 'workspace', id: 'private' }), { scope: 'workspace', id: 'private' });
  assert.deepEqual(parseProfileRef('global:fast'), { scope: 'global', id: 'fast' });
  assert.deepEqual(parseProfileRef('fast'), { scope: 'global', id: 'fast' });
  assert.equal(parseProfileRef({ scope: 'team', id: 'fast' }), null);
  assert.equal(encodeProfileRef({ scope: 'workspace', id: 'private' }), 'workspace:private');
});

test('new profile drafts default max tokens to 8192', () => {
  assert.equal(createProfileDraft().max_tokens, 8192);
});

test('draft and payload never reuse or emit a public secret placeholder', () => {
  const draft = createProfileDraft(profile('fast', { has_api_key: true, api_key: 'server-should-not-appear' }));
  assert.equal(draft.api_key, '');
  const payload = profileToPayload(draft);
  assert.equal(payload.api_key, undefined);
  assert.equal(payload.has_api_key, undefined);

  const withReplacement = profileToPayload({ ...draft, api_key: 'new-secret' });
  assert.equal(withReplacement.api_key, 'new-secret');
});

test('global unsaved connection test sends full llm_profiles contract and target profile id', () => {
  const config = {
    profiles: [profile('fast'), profile('task', { api_key: 'draft-secret' })],
    default_main_profile_ref: { scope: 'global', id: 'fast' },
    default_task_profile_ref: { scope: 'global', id: 'task' },
  };
  const payload = buildGlobalModelPayload(config, { profileId: 'task' });
  assert.deepEqual(payload.default_main_profile_ref, { scope: 'global', id: 'fast' });
  assert.deepEqual(payload.default_task_profile_ref, { scope: 'global', id: 'task' });
  assert.equal(payload.profile_id, 'task');
  assert.equal(payload.profiles.length, 2);
  assert.equal(payload.profiles[1].api_key, 'draft-secret');
});

test('global payload rejects dangling main and task refs', () => {
  const config = {
    profiles: [profile('fast')],
    default_main_profile_ref: { scope: 'global', id: 'missing' },
    default_task_profile_ref: null,
  };
  assert.throws(() => buildGlobalModelPayload(config), /引用无效/);
  assert.throws(() => buildGlobalModelPayload({
    ...config,
    default_main_profile_ref: { scope: 'global', id: 'fast' },
    default_task_profile_ref: { scope: 'global', id: 'missing' },
  }), /引用无效/);
});

test('workspace test payload includes unsaved profiles and profile_id while normal save permits an empty array', () => {
  const testPayload = buildWorkspaceModelPayload([profile('private', { api_key: 'draft-secret' })], { profileId: 'private' });
  assert.equal(testPayload.profile_id, 'private');
  assert.equal(testPayload.profiles[0].api_key, 'draft-secret');
  assert.deepEqual(buildWorkspaceModelPayload([]), { profiles: [] });
});

test('global normalization repairs a missing main ref and clears a dangling task ref', () => {
  const normalized = normalizeGlobalModelConfig({
    profiles: [profile('disabled', { enabled: false }), profile('enabled')],
    default_main_profile_ref: { scope: 'global', id: 'missing' },
    default_task_profile_ref: { scope: 'global', id: 'missing' },
  });
  assert.deepEqual(normalized.default_main_profile_ref, { scope: 'global', id: 'enabled' });
  assert.equal(normalized.default_task_profile_ref, null);
});

test('global deletion protects the last model and global default roles', () => {
  const last = globalProfileDeleteDecision({
    profileId: 'fast',
    config: { profiles: [profile('fast')], default_main_profile_ref: { scope: 'global', id: 'fast' } },
    referenceInventory: { complete: true, entries: [] },
  });
  assert.equal(last.code, 'last_global_model');

  const referenced = globalProfileDeleteDecision({
    profileId: 'fast',
    config: {
      profiles: [profile('fast'), profile('other')],
      default_main_profile_ref: { scope: 'global', id: 'fast' },
      default_task_profile_ref: null,
    },
    referenceInventory: { complete: true, entries: [] },
  });
  assert.equal(referenced.code, 'profile_referenced');
  assert.match(referenced.message, /重绑定/);
});

test('global deletion fails closed when workspace references are incomplete', () => {
  const config = {
    profiles: [profile('fast'), profile('other')],
    default_main_profile_ref: { scope: 'global', id: 'other' },
    default_task_profile_ref: null,
  };
  assert.equal(globalProfileDeleteDecision({
    profileId: 'fast', config, referenceInventory: { complete: false, entries: [] },
  }).code, 'references_incomplete');
  assert.equal(globalProfileDeleteDecision({
    profileId: 'fast',
    config,
    referenceInventory: { complete: true, entries: [{ workspaceId: 'a', runtimeKnown: false }] },
  }).code, 'references_incomplete');
});

test('global deletion checks main and task refs across every workspace', () => {
  const config = {
    profiles: [profile('fast'), profile('other')],
    default_main_profile_ref: { scope: 'global', id: 'other' },
    default_task_profile_ref: null,
  };
  const decision = globalProfileDeleteDecision({
    profileId: 'fast',
    config,
    referenceInventory: {
      complete: true,
      entries: [
        { workspaceId: 'alpha', workspaceName: 'Alpha', runtimeKnown: true, runtime: { llm: { default_task_profile_ref: 'global:fast' } } },
        { workspaceId: 'beta', workspaceName: 'Beta', runtimeKnown: true, runtime: { llm: {} } },
      ],
    },
  });
  assert.equal(decision.code, 'profile_referenced');
  assert.match(decision.message, /Alpha.*任务 Agent/);

  const allowed = globalProfileDeleteDecision({
    profileId: 'fast',
    config,
    referenceInventory: {
      complete: true,
      entries: [{ workspaceId: 'beta', runtimeKnown: true, runtime: { llm: { default_main_profile_ref: 'global:other' } } }],
    },
  });
  assert.equal(allowed.allowed, true);
});

test('workspace deletion blocks unknown runtime and referenced private roles, then allows an unreferenced profile', () => {
  const profiles = [profile('private'), profile('other')];
  assert.equal(workspaceProfileDeleteDecision({ profileId: 'private', profiles, runtimeKnown: false }).code, 'references_incomplete');

  const referenced = workspaceProfileDeleteDecision({
    profileId: 'private',
    profiles,
    runtimeKnown: true,
    runtime: { llm: { default_main_profile_ref: { scope: 'workspace', id: 'private' } } },
  });
  assert.equal(referenced.code, 'profile_referenced');
  assert.match(referenced.message, /重绑定/);

  const allowed = workspaceProfileDeleteDecision({
    profileId: 'private',
    profiles,
    runtimeKnown: true,
    runtime: { llm: { default_main_profile_ref: { scope: 'workspace', id: 'other' } } },
  });
  assert.equal(allowed.allowed, true);
});

test('profile test fingerprint changes with endpoint, model, or draft key', () => {
  const base = profile('fast');
  const fingerprint = profileFingerprint(base);
  assert.notEqual(profileFingerprint({ ...base, base_url: 'https://other.example/v1' }), fingerprint);
  assert.notEqual(profileFingerprint({ ...base, model: 'other-model' }), fingerprint);
  assert.notEqual(profileFingerprint({ ...base, api_key: 'new-secret' }), fingerprint);
  assert.equal(profileFingerprint({ ...base }), fingerprint);

  const result = bindProfileTestResult({ success: true }, base);
  assert.equal(isProfileTestResultCurrent(result, base), true);
  assert.equal(isProfileTestResultCurrent(result, { ...base, model: 'other-model' }), false);
  assert.equal(isProfileTestResultCurrent(result, { ...base, api_key: 'rotated-secret' }), false);
});

test('delete snapshot comparison fails closed for missing, stale, and confirmation-period changes', () => {
  assert.equal(deletionSnapshotDecision({ currentFingerprint: '', baselineFingerprint: 'base' }).code, 'snapshot_unknown');
  assert.equal(deletionSnapshotDecision({ currentFingerprint: 'server-new', baselineFingerprint: 'base' }).code, 'snapshot_changed');
  assert.equal(deletionSnapshotDecision({
    currentFingerprint: 'base', baselineFingerprint: 'base', openedFingerprint: 'opened-old',
  }).code, 'snapshot_changed');
  assert.equal(deletionSnapshotDecision({
    currentFingerprint: 'base', baselineFingerprint: 'base', openedFingerprint: 'base',
  }).allowed, true);
});
