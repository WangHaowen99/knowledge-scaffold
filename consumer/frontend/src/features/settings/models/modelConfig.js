export const MODEL_PROFILE_DEFAULTS = Object.freeze({
  provider: 'openai_compatible',
  base_url: '',
  model: '',
  timeout: 60,
  max_tokens: 32768,
  temperature: 0.7,
  enabled: true,
});

const PROFILE_PAYLOAD_FIELDS = [
  'id',
  'name',
  'provider',
  'base_url',
  'model',
  'timeout',
  'max_tokens',
  'temperature',
  'enabled',
];

const ROLE_FIELDS = [
  ['main', 'default_main_profile_ref', '主 Agent'],
  ['task', 'default_task_profile_ref', '任务 Agent'],
];

const text = (value) => String(value ?? '').trim();

export function parseProfileRef(value) {
  if (value == null || value === '') return null;
  if (typeof value === 'object') {
    const scope = text(value.scope).toLowerCase();
    const id = text(value.id);
    return (scope === 'global' || scope === 'workspace') && id ? { scope, id } : null;
  }
  const raw = text(value);
  if (!raw) return null;
  if (raw.startsWith('global:') || raw.startsWith('workspace:')) {
    const separator = raw.indexOf(':');
    const scope = raw.slice(0, separator);
    const id = text(raw.slice(separator + 1));
    return id ? { scope, id } : null;
  }
  return { scope: 'global', id: raw };
}

export function encodeProfileRef(value) {
  const ref = parseProfileRef(value);
  return ref ? `${ref.scope}:${ref.id}` : '';
}

export function profileRefEquals(value, scope, id) {
  const ref = parseProfileRef(value);
  return Boolean(ref && ref.scope === scope && ref.id === id);
}

export function createProfileDraft(initial = {}, { scope = 'global' } = {}) {
  const draft = {
    ...MODEL_PROFILE_DEFAULTS,
    ...initial,
    id: text(initial.id),
    name: text(initial.name),
    provider: text(initial.provider) || MODEL_PROFILE_DEFAULTS.provider,
    base_url: text(initial.base_url),
    model: text(initial.model),
    api_key: '',
    timeout: Number(initial.timeout ?? MODEL_PROFILE_DEFAULTS.timeout),
    max_tokens: Number(initial.max_tokens ?? MODEL_PROFILE_DEFAULTS.max_tokens),
    temperature: Number(initial.temperature ?? MODEL_PROFILE_DEFAULTS.temperature),
    enabled: initial.enabled !== false,
    scope,
  };
  delete draft.is_default;
  delete draft.profile_id;
  return draft;
}

export function validateProfile(profile, { scope = 'global', existingIds = [], originalId = '' } = {}) {
  const id = text(profile?.id);
  if (!id) return '请填写模型标识';
  if (/\s/.test(id) || id.length > 80) return '模型标识不能含空格且不能超过 80 个字符';
  if (scope === 'workspace' && id.includes(':')) return '工作空间私有模型标识不能包含冒号';
  if (existingIds.includes(id) && id !== originalId) return `模型标识「${id}」已存在`;
  if (!text(profile?.name)) return '请填写显示名称';
  if (!text(profile?.base_url)) return '请填写接口地址';
  if (!text(profile?.model)) return '请填写模型名称';
  if (!Number.isFinite(Number(profile?.timeout)) || Number(profile.timeout) < 1) return '超时必须是大于 0 的数字';
  if (!Number.isFinite(Number(profile?.max_tokens)) || Number(profile.max_tokens) < 1) return '最大输出长度必须是大于 0 的数字';
  if (!Number.isFinite(Number(profile?.temperature))) return '回答随机性必须是数字';
  return '';
}

export function profileToPayload(profile) {
  const payload = {};
  for (const field of PROFILE_PAYLOAD_FIELDS) {
    if (profile?.[field] === undefined) continue;
    payload[field] = profile[field];
  }
  payload.id = text(payload.id);
  payload.name = text(payload.name);
  payload.provider = text(payload.provider) || MODEL_PROFILE_DEFAULTS.provider;
  payload.base_url = text(payload.base_url);
  payload.model = text(payload.model);
  payload.timeout = Number(payload.timeout);
  payload.max_tokens = Number(payload.max_tokens);
  payload.temperature = Number(payload.temperature);
  payload.enabled = payload.enabled !== false;
  const apiKey = text(profile?.api_key);
  if (apiKey) payload.api_key = apiKey;
  if (profile?.clear_api_key === true) payload.clear_api_key = true;
  return payload;
}

function assertProfiles(profiles, scope) {
  if (!Array.isArray(profiles) || !profiles.length) {
    throw new Error(scope === 'global' ? '全局至少保留一个模型' : '未找到要测试的空间私有模型');
  }
  const ids = profiles.map((profile) => text(profile?.id));
  for (const profile of profiles) {
    const error = validateProfile(profile, { scope, existingIds: ids.filter((id) => id !== text(profile?.id)) });
    if (error) throw new Error(error);
  }
  if (new Set(ids).size !== ids.length) throw new Error('模型标识不能重复');
}

function normalizedGlobalRef(value, profiles, { required = false } = {}) {
  const ref = parseProfileRef(value);
  if (!ref) {
    if (!required) return null;
    throw new Error('主 Agent 默认模型不能为空');
  }
  if (ref.scope !== 'global' || !profiles.some((profile) => text(profile.id) === ref.id)) {
    throw new Error(`默认模型引用无效：${encodeProfileRef(ref) || '未知引用'}`);
  }
  return ref;
}

export function buildGlobalModelPayload(config, { profileId = '' } = {}) {
  const profiles = Array.isArray(config?.profiles) ? config.profiles : [];
  assertProfiles(profiles, 'global');
  const payload = {
    profiles: profiles.map(profileToPayload),
    default_main_profile_ref: normalizedGlobalRef(config.default_main_profile_ref, profiles, { required: true }),
    default_task_profile_ref: normalizedGlobalRef(config.default_task_profile_ref, profiles),
  };
  if (profileId) {
    if (!profiles.some((profile) => text(profile.id) === text(profileId))) {
      throw new Error(`未找到要测试的模型：${profileId}`);
    }
    payload.profile_id = text(profileId);
  }
  return payload;
}

export function buildWorkspaceModelPayload(profiles, { profileId = '' } = {}) {
  const list = Array.isArray(profiles) ? profiles : [];
  if (profileId) assertProfiles(list, 'workspace');
  const payload = { profiles: list.map(profileToPayload) };
  if (profileId) {
    if (!list.some((profile) => text(profile.id) === text(profileId))) {
      throw new Error(`未找到要测试的空间私有模型：${profileId}`);
    }
    payload.profile_id = text(profileId);
  }
  return payload;
}

export function normalizeGlobalModelConfig(section = {}) {
  const profiles = Array.isArray(section.profiles)
    ? section.profiles.map((profile) => createProfileDraft(profile, { scope: 'global' }))
    : [];
  let mainRef = parseProfileRef(section.default_main_profile_ref);
  if (!mainRef || mainRef.scope !== 'global' || !profiles.some((profile) => profile.id === mainRef.id)) {
    const fallback = profiles.find((profile) => profile.enabled) || profiles[0];
    mainRef = fallback ? { scope: 'global', id: fallback.id } : null;
  }
  let taskRef = parseProfileRef(section.default_task_profile_ref);
  if (!taskRef || taskRef.scope !== 'global' || !profiles.some((profile) => profile.id === taskRef.id)) taskRef = null;
  return {
    profiles,
    default_main_profile_ref: mainRef,
    default_task_profile_ref: taskRef,
  };
}

export function modelConfigFingerprint(config) {
  const profiles = (config?.profiles || []).map((profile) => profileToPayload(profile));
  return JSON.stringify({
    profiles,
    default_main_profile_ref: parseProfileRef(config?.default_main_profile_ref),
    default_task_profile_ref: parseProfileRef(config?.default_task_profile_ref),
  });
}

export function profileFingerprint(profile) {
  const serialized = JSON.stringify(profileToPayload(profile));
  let hash = 2166136261;
  for (let index = 0; index < serialized.length; index += 1) {
    hash ^= serialized.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${text(profile?.id)}:${serialized.length}:${(hash >>> 0).toString(16)}`;
}

export function bindProfileTestResult(result, profile) {
  return { ...(result || {}), fingerprint: profileFingerprint(profile) };
}

export function isProfileTestResultCurrent(result, profile) {
  return Boolean(result?.fingerprint && result.fingerprint === profileFingerprint(profile));
}

export function deletionSnapshotDecision({
  currentFingerprint,
  baselineFingerprint,
  openedFingerprint = '',
  currentGeneration = '',
  openedGeneration = '',
  requireGeneration = false,
} = {}) {
  if (!currentFingerprint || !baselineFingerprint) {
    return blocked('snapshot_unknown', '无法确认服务器模型快照，已阻止删除，请刷新后重试');
  }
  if (requireGeneration && !currentGeneration) {
    return blocked('snapshot_unknown', '无法确认模型引用快照，已阻止删除，请刷新后重试');
  }
  if (currentFingerprint !== baselineFingerprint) {
    return blocked('snapshot_changed', '模型配置已被其他操作修改，已阻止删除，请刷新后重试');
  }
  if (openedFingerprint && currentFingerprint !== openedFingerprint) {
    return blocked('snapshot_changed', '删除确认期间模型配置发生变化，已阻止删除，请刷新后重试');
  }
  if (openedGeneration && currentGeneration !== openedGeneration) {
    return blocked('snapshot_changed', '删除确认期间模型引用发生变化，已阻止删除，请刷新后重试');
  }
  return { allowed: true, code: 'ok', message: '服务器快照一致', usages: [] };
}

function roleUsages(llm, scope, profileId, workspaceLabel = '') {
  const usages = [];
  for (const [role, field, roleLabel] of ROLE_FIELDS) {
    const rawRef = llm?.[field];
    if (rawRef && !parseProfileRef(rawRef)) {
      usages.push({ kind: 'unknown', role, label: `${workspaceLabel}${roleLabel}引用格式无法识别` });
    } else if (profileRefEquals(rawRef, scope, profileId)) {
      usages.push({ kind: 'reference', role, label: `${workspaceLabel}${roleLabel}` });
    }
  }
  return usages;
}

export function markGlobalProfileRename(profile, originalId, persistedProfileIds = []) {
  const candidate = { ...(profile || {}) };
  const persisted = new Set((persistedProfileIds || []).map(text).filter(Boolean));
  const inheritedOrigin = text(candidate.rename_from);
  const editedOrigin = text(originalId);
  const origin = persisted.has(inheritedOrigin)
    ? inheritedOrigin
    : (persisted.has(editedOrigin) ? editedOrigin : '');
  if (origin && text(candidate.id) !== origin) candidate.rename_from = origin;
  else delete candidate.rename_from;
  return candidate;
}

export function globalProfileDestructiveChanges(baseline, candidate) {
  const baselineIds = (baseline?.profiles || []).map((profile) => text(profile?.id)).filter(Boolean);
  const candidateIds = new Set(
    (candidate?.profiles || []).map((profile) => text(profile?.id)).filter(Boolean),
  );
  const removedProfileIds = baselineIds.filter((profileId) => !candidateIds.has(profileId));
  const removedSet = new Set(removedProfileIds);
  const renames = [];
  for (const profile of candidate?.profiles || []) {
    const originalId = text(profile?.rename_from);
    const newId = text(profile?.id);
    if (!originalId || !newId || originalId === newId || !removedSet.has(originalId)) continue;
    renames.push({ original_id: originalId, new_id: newId });
  }
  return { removedProfileIds, renames };
}

export function globalProfileMutationDecision({
  baseline,
  baselineSnapshot,
  candidate,
  serverConfig,
  serverSnapshot,
  inventory,
  openedSnapshot = null,
} = {}) {
  const changes = globalProfileDestructiveChanges(baseline, candidate);
  if (!changes.removedProfileIds.length) {
    return { allowed: true, code: 'ok', message: '无需破坏性核验', precondition: null };
  }
  const baselineGeneration = baselineSnapshot?.generation;
  const serverGeneration = serverSnapshot?.generation;
  const baselineFingerprint = text(baselineSnapshot?.fingerprint);
  const serverFingerprint = text(serverSnapshot?.fingerprint);
  if (
    !Number.isSafeInteger(baselineGeneration)
    || baselineGeneration < 0
    || !Number.isSafeInteger(serverGeneration)
    || serverGeneration < 0
    || !baselineFingerprint
    || !serverFingerprint
    || !text(inventory?.generation)
  ) {
    return blocked('snapshot_unknown', '无法确认服务器模型或引用快照，已阻止保存，请刷新后重试');
  }
  if (
    baselineGeneration !== serverGeneration
    || baselineFingerprint !== serverFingerprint
  ) {
    return blocked('snapshot_changed', '模型配置已被其他操作修改，已阻止保存，请刷新后重试');
  }
  if (openedSnapshot && (
    openedSnapshot.configGeneration !== serverGeneration
    || openedSnapshot.configFingerprint !== serverFingerprint
    || openedSnapshot.referenceGeneration !== inventory.generation
  )) {
    return blocked('snapshot_changed', '确认期间模型配置或引用发生变化，已阻止保存，请刷新后重试');
  }

  for (const profileId of changes.removedProfileIds) {
    const renamedSource = changes.renames.some((rename) => rename.original_id === profileId);
    const decision = globalProfileDeleteDecision({
      profileId,
      config: {
        ...serverConfig,
        default_main_profile_ref: candidate?.default_main_profile_ref,
        default_task_profile_ref: candidate?.default_task_profile_ref,
      },
      referenceInventory: inventory,
      preservesProfileCount: renamedSource,
    });
    if (!decision.allowed) return decision;
  }
  return {
    allowed: true,
    code: 'ok',
    message: '服务器模型与引用快照一致',
    precondition: {
      config_generation: serverGeneration,
      config_fingerprint: serverFingerprint,
      reference_generation: inventory.generation,
      removed_profile_ids: changes.removedProfileIds,
      renames: changes.renames,
    },
  };
}

function aggregateRoleUsages(references, scope, profileId, workspaceLabel = '') {
  const usages = [];
  for (const reference of references) {
    const roleField = ROLE_FIELDS.find(([, field]) => field === reference?.field);
    const referenceScope = text(reference?.scope);
    const referenceId = text(reference?.profile_id);
    if (!roleField || !['global', 'workspace'].includes(referenceScope) || !referenceId) {
      usages.push({ kind: 'unknown', role: '', label: `${workspaceLabel}模型引用格式无法识别` });
      continue;
    }
    if (referenceScope === scope && referenceId === profileId) {
      usages.push({ kind: 'reference', role: roleField[0], label: `${workspaceLabel}${roleField[2]}` });
    }
  }
  return usages;
}

function blocked(code, message, usages = []) {
  return { allowed: false, code, message, usages };
}

export function globalProfileDeleteDecision({
  profileId,
  config,
  referenceInventory,
  preservesProfileCount = false,
} = {}) {
  const id = text(profileId);
  const profiles = Array.isArray(config?.profiles) ? config.profiles : [];
  if (!id || !profiles.some((profile) => text(profile.id) === id)) {
    return blocked('profile_missing', '未找到要删除的全局模型');
  }
  if (profiles.length <= 1 && !preservesProfileCount) {
    return blocked('last_global_model', '全局至少需要保留一个模型，请先新增替代模型');
  }

  const globalUsages = roleUsages({
    default_main_profile_ref: config?.default_main_profile_ref,
    default_task_profile_ref: config?.default_task_profile_ref,
  }, 'global', id, '全局');
  const unknownGlobal = globalUsages.filter((usage) => usage.kind === 'unknown');
  if (unknownGlobal.length) {
    return blocked('references_unknown', '全局默认引用无法确认，已阻止删除', unknownGlobal);
  }
  const directGlobal = globalUsages.filter((usage) => usage.kind === 'reference');
  if (directGlobal.length) {
    return blocked(
      'profile_referenced',
      `该模型仍被${directGlobal.map((usage) => usage.label).join('、')}引用，请先重绑定并保存`,
      directGlobal,
    );
  }

  if (!referenceInventory?.complete || !Array.isArray(referenceInventory.entries)) {
    return blocked('references_incomplete', '尚未完整核对所有工作空间的运行时引用，已阻止删除');
  }
  const workspaceUsages = [];
  for (const entry of referenceInventory.entries) {
    if (!entry || entry.runtimeKnown === false) {
      return blocked('references_incomplete', '部分工作空间的运行时引用加载失败，已阻止删除');
    }
    const label = `${text(entry.workspaceName) || text(entry.workspaceId) || '未知工作空间'} · `;
    if (Array.isArray(entry.references)) {
      workspaceUsages.push(...aggregateRoleUsages(entry.references, 'global', id, label));
    } else if (entry.runtime && typeof entry.runtime === 'object') {
      workspaceUsages.push(...roleUsages(entry.runtime.llm || {}, 'global', id, label));
    } else {
      return blocked('references_incomplete', '部分工作空间的运行时引用加载失败，已阻止删除');
    }
  }
  if (workspaceUsages.some((usage) => usage.kind === 'unknown')) {
    return blocked('references_unknown', '部分工作空间的模型引用无法识别，已阻止删除', workspaceUsages);
  }
  if (workspaceUsages.length) {
    return blocked(
      'profile_referenced',
      `该模型仍被${workspaceUsages.map((usage) => usage.label).join('、')}引用，请先在对应空间重绑定并保存`,
      workspaceUsages,
    );
  }
  return { allowed: true, code: 'ok', message: '可以删除', usages: [] };
}

export function workspaceProfileDeleteDecision({ profileId, profiles, runtime, runtimeKnown = false } = {}) {
  const id = text(profileId);
  if (!id || !(profiles || []).some((profile) => text(profile.id) === id)) {
    return blocked('profile_missing', '未找到要删除的空间私有模型');
  }
  if (!runtimeKnown || !runtime || typeof runtime !== 'object') {
    return blocked('references_incomplete', '当前空间运行时引用尚未核对完成，已阻止删除');
  }
  const usages = roleUsages(runtime.llm || {}, 'workspace', id);
  if (usages.some((usage) => usage.kind === 'unknown')) {
    return blocked('references_unknown', '当前空间模型引用无法识别，已阻止删除', usages);
  }
  if (usages.length) {
    return blocked(
      'profile_referenced',
      `该模型仍被${usages.map((usage) => usage.label).join('、')}引用，请先重绑定并保存`,
      usages,
    );
  }
  return { allowed: true, code: 'ok', message: '可以删除', usages: [] };
}

export function replaceProfile(profiles, profile, originalId = '') {
  const list = Array.isArray(profiles) ? profiles : [];
  const targetId = text(originalId || profile?.id);
  const exists = list.some((item) => text(item.id) === targetId);
  return exists
    ? list.map((item) => (text(item.id) === targetId ? profile : item))
    : [...list, profile];
}

export function removeProfile(profiles, profileId) {
  const id = text(profileId);
  return (Array.isArray(profiles) ? profiles : []).filter((profile) => text(profile.id) !== id);
}
