import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { authFetch } from '../../../shared/api/auth.js';
import {
  EmptyState,
  FieldRow,
  Modal,
  SectionCard,
  StatusTag,
  Table,
} from '../../../shared/ui/primitives.jsx';
import {
  loadGlobalModels,
  loadModelProfileReferences,
  loadWorkspaceModels,
  loadWorkspacePrivateModels,
  loadWorkspaceRuntime,
  saveGlobalModels,
  saveWorkspaceModelRefs,
  saveWorkspaceModels,
  testGlobalModel,
  testWorkspaceModel,
} from './api.js';
import {
  buildGlobalModelPayload,
  buildWorkspaceModelPayload,
  bindProfileTestResult,
  createProfileDraft,
  deletionSnapshotDecision,
  encodeProfileRef,
  globalProfileDestructiveChanges,
  globalProfileDeleteDecision,
  globalProfileMutationDecision,
  isProfileTestResultCurrent,
  markGlobalProfileRename,
  modelConfigFingerprint,
  normalizeGlobalModelConfig,
  parseProfileRef,
  removeProfile,
  replaceProfile,
  validateProfile,
  workspaceProfileDeleteDecision,
} from './modelConfig.js';
import './modelSettings.css';

const PROVIDERS = {
  glm: {
    label: 'GLM（智谱）',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
  },
  deepseek: { label: 'DeepSeek', baseUrl: 'https://api.deepseek.com' },
  openai_compatible: { label: 'OpenAI 兼容', baseUrl: '' },
};
const ON_DEMAND_REFERENCE_INVENTORY = Object.freeze({
  complete: false,
  entries: [],
});

function ReadOnlyNotice({ children }) {
  return <div className="mw-model-readonly">🔒 {children}</div>;
}

function Feedback({ notice }) {
  if (!notice?.message) {
    return null;
  }
  return (
    <div
      className={`mw-model-feedback ${notice.type || 'info'}`}
      role={notice.type === 'error' ? 'alert' : 'status'}
    >
      {notice.message}
    </div>
  );
}

function ConnectionStatus({ result, profile }) {
  if (!result) {
    return <span className="mw-model-muted">未测试</span>;
  }
  if (!isProfileTestResultCurrent(result, profile)) {
    return <StatusTag status="warning">已失效</StatusTag>;
  }
  return (
    <span className="mw-model-test-result">
      <StatusTag status={result.success ? 'success' : 'error'}>
        {result.success ? '通过' : '失败'}
      </StatusTag>
      {result.tested_at && (
        <time dateTime={result.tested_at}>
          {new Date(result.tested_at).toLocaleString()}
        </time>
      )}
    </span>
  );
}

function SecretStatus({ profile }) {
  if (profile.api_key) {
    return <StatusTag status="warning">待保存</StatusTag>;
  }
  return (
    <StatusTag status={profile.has_api_key ? 'success' : 'warning'}>
      {profile.has_api_key ? '已配置' : '未配置'}
    </StatusTag>
  );
}

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
function ProfileEditor({
  scope,
  initial,
  profiles,
  busy,
  allowIdEdit = false,
  onClose,
  onSaveDraft,
  onTestDraft,
}) {
  const originalId = initial?.id || '';
  const [draft, setDraft] = useState(() => ({
    ...createProfileDraft(initial || {}, { scope }),
    api_key: initial?.api_key || '',
  }));
  const [error, setError] = useState('');
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const existingIds = profiles.map((profile) => profile.id);
  const isEdit = Boolean(originalId);

  const update = (field, value) => {
    setTestResult(null);
    setDraft((current) => ({ ...current, [field]: value }));
  };
  const pickProvider = (provider) => {
    setTestResult(null);
    setDraft((current) => {
      const knownPreset = Object.values(PROVIDERS).some(
        (item) => item.baseUrl === current.base_url,
      );
      return {
        ...current,
        provider,
        base_url:
          !current.base_url || knownPreset ?
            PROVIDERS[provider].baseUrl :
            current.base_url,
      };
    });
  };
  const validate = () =>
    validateProfile(draft, { scope, existingIds, originalId });
  const save = async () => {
    const nextError = validate();
    if (nextError) {
      setError(nextError);
      return;
    }
    setError('');
    try {
      await onSaveDraft(
        { ...draft, id: draft.id.trim(), name: draft.name.trim() },
        originalId,
      );
    } catch (saveError) {
      setError(saveError?.message || '应用模型草稿失败');
    }
  };
  const test = async () => {
    const nextError = validate();
    if (nextError) {
      setError(nextError);
      return;
    }
    setError('');
    setTesting(true);
    try {
      const result = await onTestDraft(
        { ...draft, id: draft.id.trim(), name: draft.name.trim() },
        originalId,
      );
      setTestResult(result);
    } catch (testError) {
      setTestResult({
        success: false,
        message: testError?.message || '测试失败',
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Modal
      title={
        isEdit ?
          `编辑模型 · ${initial.name || originalId}` :
          `新增${scope === 'global' ? '全局' : '空间私有'}模型`
      }
      description="连接测试直接使用当前草稿；密钥留空沿用已保存密钥。"
      onClose={onClose}
      wide
      closeOnOverlayClick={false}
      closeOnEscape={false}
      actions={
        <>
          <button
            type="button"
            className="btn"
            disabled={busy || testing}
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy || testing}
            onClick={test}
          >
            {testing ? '测试中…' : '测试'}
          </button>
          <button
            type="button"
            className="btn btn-p"
            disabled={busy || testing}
            onClick={save}
          >
            应用
          </button>
        </>
      }
    >
      <div className="mw-model-form">
        <FieldRow
          label="标识 *"
          hint={
            allowIdEdit ?
              '修改已有标识会在保存前核验引用；不允许空格，最多 80 个字符' :
              '创建后不可修改；不允许空格，最多 80 个字符'
          }
        >
          <input
            aria-label="模型标识"
            autoFocus
            disabled={isEdit && !allowIdEdit}
            value={draft.id}
            onChange={(event) => update('id', event.target.value)}
          />
        </FieldRow>
        <FieldRow label="显示名称 *">
          <input
            aria-label="模型显示名称"
            value={draft.name}
            onChange={(event) => update('name', event.target.value)}
          />
        </FieldRow>
        <FieldRow label="模型厂商">
          <select
            aria-label="模型厂商"
            value={draft.provider}
            onChange={(event) => pickProvider(event.target.value)}
          >
            {Object.entries(PROVIDERS).map(([id, provider]) => (
              <option key={id} value={id}>
                {provider.label}
              </option>
            ))}
          </select>
        </FieldRow>
        <FieldRow label="接口地址 *">
          <input
            aria-label="模型接口地址"
            value={draft.base_url}
            onChange={(event) => update('base_url', event.target.value)}
          />
        </FieldRow>
        <FieldRow label="模型名称 *">
          <input
            aria-label="模型调用名称"
            value={draft.model}
            onChange={(event) => update('model', event.target.value)}
          />
        </FieldRow>
        <FieldRow
          label="API 密钥"
          hint={
            initial?.has_api_key ?
              '已配置；留空保留原密钥，密钥不会回显' :
              '密钥不会回显；可先填写后测试'
          }
        >
          <input
            aria-label="模型 API 密钥"
            type="password"
            autoComplete="new-password"
            value={draft.api_key}
            placeholder={initial?.has_api_key ? '••••••（留空保留）' : ''}
            onChange={(event) => update('api_key', event.target.value)}
          />
        </FieldRow>
        <div className="mw-model-form-grid">
          <label>
            超时（秒）
            <input
              aria-label="模型请求超时秒数"
              type="number"
              min="1"
              value={draft.timeout}
              onChange={(event) => update('timeout', event.target.value)}
            />
          </label>
          <label>
            最大输出长度
            <input
              aria-label="模型最大输出长度"
              type="number"
              min="1"
              value={draft.max_tokens}
              onChange={(event) => update('max_tokens', event.target.value)}
            />
          </label>
          <label>
            回答随机性
            <input
              aria-label="模型回答随机性"
              type="number"
              step="0.1"
              value={draft.temperature}
              onChange={(event) => update('temperature', event.target.value)}
            />
          </label>
          <label className="mw-model-check">
            <input
              aria-label="启用模型"
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) => update('enabled', event.target.checked)}
            />{' '}
            启用
          </label>
        </div>
        {error && (
          <div className="mw-model-inline-error" role="alert">
            {error}
          </div>
        )}
        {testResult && (
          <div
            className={`mw-model-test-message ${testResult.success ? 'success' : 'error'}`}
            role="status"
          >
            {testResult.message ||
              (testResult.success ? '连接测试成功' : '连接测试失败')}
          </div>
        )}
        {testResult && !testResult.success && (
          <div className="mw-model-muted" role="note">
            上次测试未通过，仍可应用后到列表中重新测试。
          </div>
        )}
      </div>
    </Modal>
  );
}

function DeleteDialog({ title, profile, busy, onClose, onConfirm }) {
  return (
    <Modal
      title={title}
      description={`确认删除「${profile.name || profile.id}」？保存时会提交删除后的完整模型数组。`}
      onClose={onClose}
      actions={
        <>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? '删除中…' : '确认删除'}
          </button>
        </>
      }
    >
      <p className="mw-model-delete-warning">
        删除后无法恢复。已保存的模型引用必须先完成重绑定。
      </p>
    </Modal>
  );
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
function GlobalModelTable({
  config,
  canEdit,
  busy,
  tests,
  inventory,
  onEdit,
  onTest,
  onDelete,
}) {
  return (
    <div className="mw-model-table-wrap">
      <Table>
        <thead>
          <tr>
            <th>模型</th>
            <th>模型厂商 / 接口</th>
            <th>密钥</th>
            <th>状态</th>
            <th>连接测试</th>
            {canEdit && <th>操作</th>}
          </tr>
        </thead>
        <tbody>
          {config.profiles.map((profile) => {
            const decision = globalProfileDeleteDecision({
              profileId: profile.id,
              config,
              referenceInventory: inventory,
            });
            const canPreflight =
              decision.allowed || decision.code === 'references_incomplete';
            return (
              <tr key={profile.id}>
                <td>
                  <strong>{profile.name}</strong>
                  <span className="mw-model-id">
                    {profile.id} · {profile.model}
                  </span>
                </td>
                <td>
                  {profile.provider}
                  <span className="mw-model-id">
                    {profile.base_url || '未填写接口地址'}
                  </span>
                </td>
                <td>
                  <SecretStatus profile={profile} />
                </td>
                <td>
                  <StatusTag status={profile.enabled ? 'enabled' : 'disabled'}>
                    {profile.enabled ? '启用' : '停用'}
                  </StatusTag>
                </td>
                <td>
                  <ConnectionStatus
                    result={tests[profile.id]}
                    profile={profile}
                  />
                </td>
                {canEdit && (
                  <td className="mw-model-actions">
                    <button
                      type="button"
                      className="btn"
                      disabled={busy}
                      onClick={() => onTest(profile.id)}
                    >
                      测试
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={busy}
                      onClick={() => onEdit(profile)}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger"
                      disabled={busy || !canPreflight}
                      title={
                        canPreflight ?
                          '删除前核验全部工作空间引用' :
                          decision.message
                      }
                      onClick={() => onDelete(profile)}
                    >
                      删除
                    </button>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </Table>
    </div>
  );
}

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export function GlobalModelSettings({
  canEdit = false,
  request = authFetch,
  onChanged,
}) {
  const [baseline, setBaseline] = useState(null);
  const [baselineSnapshot, setBaselineSnapshot] = useState(null);
  const [draft, setDraft] = useState(null);
  const [editor, setEditor] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleteCandidate, setDeleteCandidate] = useState(null);
  const [deleteOpenedSnapshot, setDeleteOpenedSnapshot] = useState(null);
  const [tests, setTests] = useState({});
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState('');
  const requestSequence = useRef(0);

  const load = useCallback(
    async (signal, { preserveNotice = false } = {}) => {
      const sequence = ++requestSequence.current;
      setBusy('load');
      if (!preserveNotice) {
        setNotice(null);
      }
      try {
        const { config, snapshot } = await loadGlobalModels({
          request,
          signal,
        });
        if (sequence !== requestSequence.current) {
          return;
        }
        const normalized = normalizeGlobalModelConfig(config);
        setBaseline(normalized);
        setBaselineSnapshot(snapshot);
        setDraft(normalized);
        setTests({});
      } catch (error) {
        if (
          error?.name !== 'AbortError' &&
          sequence === requestSequence.current
        ) {
          setNotice({
            type: 'error',
            message: error?.message || '加载模型配置失败',
          });
        }
      } finally {
        if (sequence === requestSequence.current) {
          setBusy('');
        }
      }
    },
    [request],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => {
      requestSequence.current += 1;
      controller.abort();
    };
  }, [load]);

  const dirty = useMemo(
    () =>
      Boolean(
        baseline &&
          draft &&
          modelConfigFingerprint(baseline) !== modelConfigFingerprint(draft),
      ),
    [baseline, draft],
  );

  const latestGlobalMutationDecision = async (
    candidateConfig,
    { openedSnapshot = null } = {},
  ) => {
    const [serverBundle, nextInventory] = await Promise.all([
      loadGlobalModels({ request }),
      loadModelProfileReferences(undefined, { request }),
    ]);
    return globalProfileMutationDecision({
      baseline,
      baselineSnapshot,
      candidate: candidateConfig,
      serverConfig: normalizeGlobalModelConfig(serverBundle.config),
      serverSnapshot: serverBundle.snapshot,
      inventory: nextInventory,
      openedSnapshot,
    });
  };

  const runTest = async (candidateConfig, profileId) => {
    const testedProfile = candidateConfig.profiles.find(
      (profile) => profile.id === profileId,
    );
    setBusy(`test:${profileId}`);
    setNotice(null);
    try {
      const result = await testGlobalModel(
        buildGlobalModelPayload(candidateConfig, { profileId }),
        { request },
      );
      const boundResult = bindProfileTestResult(result, testedProfile);
      setTests((current) => ({ ...current, [profileId]: boundResult }));
      setNotice({
        type: result.success ? 'success' : 'error',
        message:
          result.message || (result.success ? '连接测试成功' : '连接测试失败'),
      });
      return boundResult;
    } catch (error) {
      setTests((current) => ({
        ...current,
        [profileId]: bindProfileTestResult({ success: false }, testedProfile),
      }));
      setNotice({ type: 'error', message: error?.message || '连接测试失败' });
      throw error;
    } finally {
      setBusy('');
    }
  };

  const save = async (
    nextConfig = draft,
    successMessage = '全局模型配置已保存',
    { precondition: suppliedPrecondition = null } = {},
  ) => {
    setBusy('save');
    setNotice(null);
    try {
      let precondition = suppliedPrecondition;
      const changes = globalProfileDestructiveChanges(baseline, nextConfig);
      if (changes.removedProfileIds.length && !precondition) {
        const decision = await latestGlobalMutationDecision(nextConfig);
        if (!decision.allowed) {
          setNotice({ type: 'error', message: decision.message });
          return false;
        }
        ({ precondition } = decision);
      }
      await saveGlobalModels(buildGlobalModelPayload(nextConfig), {
        request,
        precondition,
      });
      setNotice({ type: 'success', message: successMessage });
      onChanged?.({ scope: 'global' });
      await load(undefined, { preserveNotice: true });
      return true;
    } catch (error) {
      setNotice({
        type: 'error',
        message: error?.message || '保存全局模型失败',
      });
      return false;
    } finally {
      setBusy('');
    }
  };

  if (!draft) {
    return (
      <SectionCard title="模型池">
        <Feedback notice={notice} />
        {!notice && <div className="empty">加载中…</div>}
      </SectionCard>
    );
  }

  const editDraft = (profile, originalId) => {
    if (originalId && profile.id !== originalId) {
      setTests((current) => {
        const next = { ...current };
        delete next[originalId];
        return next;
      });
    }
    setDraft((current) => {
      const nextProfile = markGlobalProfileRename(
        profile,
        originalId,
        (baseline?.profiles || []).map((item) => item.id),
      );
      const profiles = replaceProfile(
        current.profiles,
        nextProfile,
        originalId,
      );
      return {
        ...current,
        profiles,
        default_main_profile_ref: current.default_main_profile_ref || {
          scope: 'global',
          id: nextProfile.id,
        },
      };
    });
    setEditor(null);
  };
  const testEditorDraft = (profile, originalId) => {
    const nextProfile = markGlobalProfileRename(
      profile,
      originalId,
      (baseline?.profiles || []).map((item) => item.id),
    );
    const candidate = {
      ...draft,
      profiles: replaceProfile(draft.profiles, nextProfile, originalId),
      default_main_profile_ref: draft.default_main_profile_ref || {
        scope: 'global',
        id: nextProfile.id,
      },
    };
    return runTest(candidate, nextProfile.id);
  };
  const requestDelete = async (profile) => {
    setBusy('check-delete');
    setNotice(null);
    try {
      const candidate = {
        ...draft,
        profiles: removeProfile(draft.profiles, profile.id),
      };
      const decision = await latestGlobalMutationDecision(candidate);
      if (!decision.allowed) {
        setNotice({ type: 'error', message: decision.message });
        return;
      }
      setDeleteCandidate(candidate);
      setDeleteOpenedSnapshot({
        configGeneration: decision.precondition.config_generation,
        configFingerprint: decision.precondition.config_fingerprint,
        referenceGeneration: decision.precondition.reference_generation,
      });
      setDeleteTarget(profile);
    } catch (error) {
      setNotice({
        type: 'error',
        message: `${error?.message || '模型引用核对失败'}；已阻止删除`,
      });
    } finally {
      setBusy('');
    }
  };
  const confirmDelete = async () => {
    setBusy('check-delete');
    let decision;
    try {
      decision = await latestGlobalMutationDecision(deleteCandidate, {
        openedSnapshot: deleteOpenedSnapshot,
      });
      if (!decision.allowed) {
        setNotice({ type: 'error', message: decision.message });
        setDeleteTarget(null);
        setDeleteCandidate(null);
        setDeleteOpenedSnapshot(null);
        setBusy('');
        return;
      }
    } catch (error) {
      setNotice({
        type: 'error',
        message: `${error?.message || '模型引用核对失败'}；已阻止删除`,
      });
      setDeleteTarget(null);
      setDeleteCandidate(null);
      setDeleteOpenedSnapshot(null);
      setBusy('');
      return;
    }
    const targetName = deleteTarget.name || deleteTarget.id;
    setDeleteTarget(null);
    setDeleteCandidate(null);
    setDeleteOpenedSnapshot(null);
    await save(deleteCandidate, `全局模型「${targetName}」已删除`, {
      precondition: decision.precondition,
    });
  };

  return (
    <SectionCard
      title="模型池"
      description="集中管理全局模型。草稿可在保存前测试，空密钥不会覆盖已保存密钥。"
      actions={
        canEdit && (
          <button
            type="button"
            className="btn btn-p"
            disabled={Boolean(busy)}
            onClick={() => setEditor({ profile: null })}
          >
            ＋ 新增模型
          </button>
        )
      }
      className="mw-model-settings"
    >
      {!canEdit && (
        <ReadOnlyNotice>仅平台管理员可修改全局模型。</ReadOnlyNotice>
      )}
      <div className="mw-model-defaults">
        <FieldRow label="主 Agent 默认模型" hint="工作空间未单独绑定时使用">
          <select
            aria-label="全局主 Agent 默认模型"
            disabled={!canEdit || Boolean(busy)}
            value={draft.default_main_profile_ref?.id || ''}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                default_main_profile_ref: {
                  scope: 'global',
                  id: event.target.value,
                },
              }))
            }
          >
            {draft.profiles.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.name}（{profile.model}）
              </option>
            ))}
          </select>
        </FieldRow>
        <FieldRow label="任务 Agent 默认模型" hint="留空时跟随主 Agent">
          <select
            aria-label="全局任务 Agent 默认模型"
            disabled={!canEdit || Boolean(busy)}
            value={draft.default_task_profile_ref?.id || ''}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                default_task_profile_ref: event.target.value ?
                  { scope: 'global', id: event.target.value } :
                  null,
              }))
            }
          >
            <option value="">跟随主 Agent</option>
            {draft.profiles.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.name}（{profile.model}）
              </option>
            ))}
          </select>
        </FieldRow>
      </div>
      {!draft.profiles.length ? (
        <EmptyState
          title="尚未配置模型"
          description="新增至少一个全局模型后才能保存。"
        />
      ) : (
        <GlobalModelTable
          config={draft}
          canEdit={canEdit}
          busy={Boolean(busy)}
          tests={tests}
          inventory={ON_DEMAND_REFERENCE_INVENTORY}
          onEdit={(profile) => setEditor({ profile })}
          onTest={(id) => runTest(draft, id)}
          onDelete={requestDelete}
        />
      )}
      <Feedback notice={notice} />
      {canEdit && (
        <div className="mw-model-footer">
          <span className="mw-model-muted">
            {dirty ? '有未保存修改' : '配置已同步'}
          </span>
          <button
            type="button"
            className="btn"
            disabled={Boolean(busy) || !dirty}
            onClick={() => setDraft(normalizeGlobalModelConfig(baseline))}
          >
            重置
          </button>
          <button
            type="button"
            className="btn btn-p"
            disabled={Boolean(busy) || !dirty || !draft.profiles.length}
            onClick={() => save()}
          >
            {busy === 'save' ? '保存中…' : '保存修改'}
          </button>
        </div>
      )}
      {editor && (
        <ProfileEditor
          key={editor.profile?.id || 'new-global'}
          scope="global"
          initial={editor.profile}
          profiles={draft.profiles}
          allowIdEdit
          busy={Boolean(busy)}
          onClose={() => setEditor(null)}
          onSaveDraft={editDraft}
          onTestDraft={testEditorDraft}
        />
      )}
      {deleteTarget && (
        <DeleteDialog
          title="删除全局模型"
          profile={deleteTarget}
          busy={busy === 'save' || busy === 'check-delete'}
          onClose={() => {
            setDeleteTarget(null);
            setDeleteCandidate(null);
            setDeleteOpenedSnapshot(null);
          }}
          onConfirm={confirmDelete}
        />
      )}
    </SectionCard>
  );
}

function availableRefOptions(profiles, currentValue) {
  const options = (Array.isArray(profiles) ? profiles : []).filter(
    (profile) => profile.enabled !== false,
  );
  if (currentValue && !options.some((profile) => profile.id === currentValue)) {
    return [
      {
        id: currentValue,
        name: '当前引用不可用',
        model: currentValue,
        disabled: true,
      },
      ...options,
    ];
  }
  return options;
}

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export function WorkspaceModelSettings({
  workspaceId,
  canEdit = false,
  request = authFetch,
  onChanged,
}) {
  const [loadedScope, setLoadedScope] = useState('');
  const [availableProfiles, setAvailableProfiles] = useState([]);
  const [baselineProfiles, setBaselineProfiles] = useState(null);
  const [draftProfiles, setDraftProfiles] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [runtimeKnown, setRuntimeKnown] = useState(false);
  const [mainRef, setMainRef] = useState('');
  const [taskRef, setTaskRef] = useState('');
  const [savedMainRef, setSavedMainRef] = useState('');
  const [savedTaskRef, setSavedTaskRef] = useState('');
  const [editor, setEditor] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleteServerFingerprint, setDeleteServerFingerprint] = useState('');
  const [tests, setTests] = useState({});
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState('');
  const requestSequence = useRef(0);
  const scopeRef = useRef(workspaceId);
  scopeRef.current = workspaceId;
  // 组件卸载后失效 scopeRef，阻止异步闭包（如删除核验返回后）继续向旧工作空间执行写入。
  // ConfigCenter 用 key={workspaceId:section} 挂载，切换空间会卸载旧实例而非更新 props，
  // 所以 scopeRef.current 会停留在旧空间 ID，仅靠 workspaceId 比较无法识别已卸载的实例。
  useEffect(
    () => () => {
      scopeRef.current = null;
    },
    [],
  );

  const load = useCallback(
    async (signal, { preserveNotice = false } = {}) => {
      const scope = workspaceId;
      const sequence = ++requestSequence.current;
      if (!scope) {
        setLoadedScope('');
        setNotice({ type: 'error', message: '缺少工作空间标识' });
        setDraftProfiles([]);
        setBaselineProfiles([]);
        return;
      }
      setBusy('load');
      if (!preserveNotice) {
        setNotice(null);
      }
      setRuntimeKnown(false);
      try {
        const bundle = await loadWorkspaceModels(scope, { request, signal });
        if (
          sequence !== requestSequence.current ||
          scopeRef.current !== scope
        ) {
          return;
        }
        const privateProfiles = bundle.privateProfiles.map((profile) =>
          createProfileDraft(profile, { scope: 'workspace' }),
        );
        const nextMainRef = encodeProfileRef(
          bundle.runtime?.llm?.default_main_profile_ref,
        );
        const nextTaskRef = encodeProfileRef(
          bundle.runtime?.llm?.default_task_profile_ref,
        );
        setAvailableProfiles(bundle.availableProfiles);
        setBaselineProfiles(privateProfiles);
        setDraftProfiles(privateProfiles);
        setRuntime(bundle.runtime);
        setRuntimeKnown(true);
        setMainRef(nextMainRef);
        setTaskRef(nextTaskRef);
        setSavedMainRef(nextMainRef);
        setSavedTaskRef(nextTaskRef);
        setTests({});
        setLoadedScope(scope);
      } catch (error) {
        if (
          error?.name !== 'AbortError' &&
          sequence === requestSequence.current &&
          scopeRef.current === scope
        ) {
          setLoadedScope(scope);
          setNotice({
            type: 'error',
            message: error?.message || '加载空间模型失败',
          });
          setRuntimeKnown(false);
        }
      } finally {
        if (
          sequence === requestSequence.current &&
          scopeRef.current === scope
        ) {
          setBusy('');
        }
      }
    },
    [request, workspaceId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoadedScope('');
    setAvailableProfiles([]);
    setBaselineProfiles(null);
    setDraftProfiles(null);
    setRuntime(null);
    setRuntimeKnown(false);
    setMainRef('');
    setTaskRef('');
    setSavedMainRef('');
    setSavedTaskRef('');
    setNotice(null);
    setTests({});
    setEditor(null);
    setDeleteTarget(null);
    setDeleteServerFingerprint('');
    load(controller.signal);
    return () => {
      requestSequence.current += 1;
      controller.abort();
    };
  }, [load]);

  const privateDirty = useMemo(
    () =>
      baselineProfiles && draftProfiles ?
        modelConfigFingerprint({ profiles: baselineProfiles }) !==
          modelConfigFingerprint({ profiles: draftProfiles }) :
        false,
    [baselineProfiles, draftProfiles],
  );
  const refsDirty = mainRef !== savedMainRef || taskRef !== savedTaskRef;

  const runTest = async (profiles, profileId) => {
    const scope = workspaceId;
    const testedProfile = profiles.find((profile) => profile.id === profileId);
    setBusy(`test:${profileId}`);
    setNotice(null);
    try {
      const result = await testWorkspaceModel(
        scope,
        buildWorkspaceModelPayload(profiles, { profileId }),
        { request },
      );
      if (scopeRef.current !== scope) {
        return result;
      }
      const boundResult = bindProfileTestResult(result, testedProfile);
      setTests((current) => ({ ...current, [profileId]: boundResult }));
      setNotice({
        type: result.success ? 'success' : 'error',
        message:
          result.message || (result.success ? '连接测试成功' : '连接测试失败'),
      });
      return boundResult;
    } catch (error) {
      if (scopeRef.current === scope) {
        setTests((current) => ({
          ...current,
          [profileId]: bindProfileTestResult({ success: false }, testedProfile),
        }));
        setNotice({ type: 'error', message: error?.message || '连接测试失败' });
      }
      throw error;
    } finally {
      if (scopeRef.current === scope) {
        setBusy('');
      }
    }
  };

  const persistProfiles = async (
    profiles = draftProfiles,
    successMessage = '空间私有模型已保存',
  ) => {
    const scope = workspaceId;
    if (scopeRef.current !== scope) {
      return false;
    }
    setBusy('save-profiles');
    setNotice(null);
    try {
      await saveWorkspaceModels(scope, buildWorkspaceModelPayload(profiles), {
        request,
      });
      if (scopeRef.current !== scope) {
        return false;
      }
      setNotice({ type: 'success', message: successMessage });
      onChanged?.({ scope: 'workspace', workspaceId: scope });
      await load(undefined, { preserveNotice: true });
      return true;
    } catch (error) {
      if (scopeRef.current === scope) {
        setNotice({
          type: 'error',
          message: error?.message || '保存空间私有模型失败',
        });
      }
      return false;
    } finally {
      if (scopeRef.current === scope) {
        setBusy('');
      }
    }
  };

  const persistRefs = async () => {
    const scope = workspaceId;
    setBusy('save-refs');
    setNotice(null);
    try {
      const nextRuntime = await saveWorkspaceModelRefs(
        scope,
        {
          default_main_profile_ref: parseProfileRef(mainRef),
          default_task_profile_ref: parseProfileRef(taskRef),
        },
        { request },
      );
      if (scopeRef.current !== scope) {
        return;
      }
      setRuntime(nextRuntime);
      setRuntimeKnown(true);
      setSavedMainRef(mainRef);
      setSavedTaskRef(taskRef);
      setNotice({ type: 'success', message: '空间模型引用已保存' });
      onChanged?.({ scope: 'workspace-runtime', workspaceId: scope });
    } catch (error) {
      if (scopeRef.current === scope) {
        setNotice({
          type: 'error',
          message: error?.message || '保存空间模型引用失败',
        });
      }
    } finally {
      if (scopeRef.current === scope) {
        setBusy('');
      }
    }
  };

  if (loadedScope !== workspaceId || !draftProfiles) {
    const currentNotice = loadedScope === workspaceId ? notice : null;
    return (
      <SectionCard title="模型选用">
        <Feedback notice={currentNotice} />
        {!currentNotice && <div className="empty">加载中…</div>}
      </SectionCard>
    );
  }

  const mainOptions = availableRefOptions(availableProfiles, mainRef);
  const taskOptions = availableRefOptions(availableProfiles, taskRef);
  const editDraft = (profile, originalId) => {
    setDraftProfiles((current) => replaceProfile(current, profile, originalId));
    setEditor(null);
  };
  const testEditorDraft = (profile, originalId) =>
    runTest(replaceProfile(draftProfiles, profile, originalId), profile.id);
  const latestPrivateDeleteDecision = async (
    profile,
    openedFingerprint = '',
  ) => {
    const scope = workspaceId;
    const [serverProfilesPayload, latestRuntime] = await Promise.all([
      loadWorkspacePrivateModels(scope, { request }),
      loadWorkspaceRuntime(scope, { request }),
    ]);
    if (scopeRef.current !== scope) {
      return { allowed: false, message: '工作空间已切换，已阻止删除' };
    }
    const serverProfiles = serverProfilesPayload.map((item) =>
      createProfileDraft(item, { scope: 'workspace' }),
    );
    const serverFingerprint = modelConfigFingerprint({
      profiles: serverProfiles,
    });
    const snapshotDecision = deletionSnapshotDecision({
      currentFingerprint: serverFingerprint,
      baselineFingerprint: modelConfigFingerprint({
        profiles: baselineProfiles,
      }),
      openedFingerprint,
    });
    setRuntime(latestRuntime);
    setRuntimeKnown(true);
    if (!snapshotDecision.allowed) {
      return snapshotDecision;
    }
    return {
      ...workspaceProfileDeleteDecision({
        profileId: profile.id,
        profiles: draftProfiles,
        runtime: latestRuntime,
        runtimeKnown: true,
      }),
      serverFingerprint,
    };
  };
  const requestDelete = async (profile) => {
    setBusy('check-delete');
    setNotice(null);
    try {
      const decision = await latestPrivateDeleteDecision(profile);
      if (!decision.allowed) {
        setNotice({ type: 'error', message: decision.message });
        return;
      }
      setDeleteServerFingerprint(decision.serverFingerprint);
      setDeleteTarget(profile);
    } catch (error) {
      if (scopeRef.current === workspaceId) {
        setNotice({
          type: 'error',
          message: `${error?.message || '空间模型引用核对失败'}；已阻止删除`,
        });
      }
    } finally {
      if (scopeRef.current === workspaceId) {
        setBusy('');
      }
    }
  };
  const confirmDelete = async () => {
    const scope = workspaceId;
    setBusy('check-delete');
    try {
      const decision = await latestPrivateDeleteDecision(
        deleteTarget,
        deleteServerFingerprint,
      );
      if (!decision.allowed) {
        if (scopeRef.current === scope) {
          setNotice({ type: 'error', message: decision.message });
          setDeleteTarget(null);
          setDeleteServerFingerprint('');
          setBusy('');
        }
        return;
      }
    } catch (error) {
      if (scopeRef.current === scope) {
        setNotice({
          type: 'error',
          message: `${error?.message || '空间模型引用核对失败'}；已阻止删除`,
        });
        setDeleteTarget(null);
        setDeleteServerFingerprint('');
        setBusy('');
      }
      return;
    }
    const targetName = deleteTarget.name || deleteTarget.id;
    const nextProfiles = removeProfile(draftProfiles, deleteTarget.id);
    if (scopeRef.current !== scope) {
      return;
    }
    setDeleteTarget(null);
    setDeleteServerFingerprint('');
    await persistProfiles(nextProfiles, `空间私有模型「${targetName}」已删除`);
  };

  return (
    <SectionCard
      title="模型选用"
      description="绑定当前工作空间的主/任务 Agent 模型，并管理仅本空间可用的私有模型。"
      actions={
        canEdit && (
          <button
            type="button"
            className="btn btn-p"
            disabled={Boolean(busy)}
            onClick={() => setEditor({ profile: null })}
          >
            ＋ 新增私有模型
          </button>
        )
      }
      className="mw-model-settings"
    >
      {!canEdit && (
        <ReadOnlyNotice>
          仅平台管理员可修改空间模型和运行时引用。
        </ReadOnlyNotice>
      )}
      <div className="mw-model-defaults">
        <FieldRow label="主 Agent 模型" hint="留空时跟随全局主模型">
          <select
            aria-label="工作空间主 Agent 模型"
            disabled={!canEdit || Boolean(busy)}
            value={mainRef}
            onChange={(event) => setMainRef(event.target.value)}
          >
            <option value="">跟随全局</option>
            {mainOptions.map((profile) => (
              <option
                key={profile.id}
                value={profile.id}
                disabled={profile.disabled}
              >
                {profile.name}（{profile.model}）
              </option>
            ))}
          </select>
        </FieldRow>
        <FieldRow label="任务 Agent 模型" hint="留空时跟随主 Agent">
          <select
            aria-label="工作空间任务 Agent 模型"
            disabled={!canEdit || Boolean(busy)}
            value={taskRef}
            onChange={(event) => setTaskRef(event.target.value)}
          >
            <option value="">跟随主 Agent</option>
            {taskOptions.map((profile) => (
              <option
                key={profile.id}
                value={profile.id}
                disabled={profile.disabled}
              >
                {profile.name}（{profile.model}）
              </option>
            ))}
          </select>
        </FieldRow>
        {canEdit && (
          <div className="mw-model-role-actions">
            <span className="mw-model-muted">
              {refsDirty ? '引用有未保存修改' : '引用已同步'}
            </span>
            <button
              type="button"
              className="btn"
              disabled={Boolean(busy) || !refsDirty}
              onClick={() => {
                setMainRef(savedMainRef);
                setTaskRef(savedTaskRef);
              }}
            >
              重置引用
            </button>
            <button
              type="button"
              className="btn btn-p"
              disabled={Boolean(busy) || !refsDirty}
              onClick={persistRefs}
            >
              保存引用
            </button>
          </div>
        )}
      </div>
      <h4 className="mw-model-subtitle">空间私有模型</h4>
      {!draftProfiles.length ? (
        <EmptyState
          title="当前空间暂无私有模型"
          description="可继续使用全局模型，或新增仅本空间可见的模型。"
        />
      ) : (
        <div className="mw-model-table-wrap">
          <Table>
            <thead>
              <tr>
                <th>模型</th>
                <th>模型厂商 / 接口</th>
                <th>密钥</th>
                <th>状态</th>
                <th>连接测试</th>
                {canEdit && <th>操作</th>}
              </tr>
            </thead>
            <tbody>
              {draftProfiles.map((profile) => {
                const decision = workspaceProfileDeleteDecision({
                  profileId: profile.id,
                  profiles: draftProfiles,
                  runtime,
                  runtimeKnown,
                });
                return (
                  <tr key={profile.id}>
                    <td>
                      <strong>{profile.name}</strong>
                      <span className="mw-model-id">
                        {profile.id} · {profile.model}
                      </span>
                    </td>
                    <td>
                      {profile.provider}
                      <span className="mw-model-id">
                        {profile.base_url || '未填写接口地址'}
                      </span>
                    </td>
                    <td>
                      <SecretStatus profile={profile} />
                    </td>
                    <td>
                      <StatusTag
                        status={profile.enabled ? 'enabled' : 'disabled'}
                      >
                        {profile.enabled ? '启用' : '停用'}
                      </StatusTag>
                    </td>
                    <td>
                      <ConnectionStatus
                        result={tests[profile.id]}
                        profile={profile}
                      />
                    </td>
                    {canEdit && (
                      <td className="mw-model-actions">
                        <button
                          type="button"
                          className="btn"
                          disabled={Boolean(busy)}
                          onClick={() => runTest(draftProfiles, profile.id)}
                        >
                          测试
                        </button>
                        <button
                          type="button"
                          className="btn"
                          disabled={Boolean(busy)}
                          onClick={() => setEditor({ profile })}
                        >
                          编辑
                        </button>
                        <button
                          type="button"
                          className="btn btn-danger"
                          disabled={Boolean(busy)}
                          aria-disabled={!decision.allowed}
                          title={
                            decision.allowed ? '删除模型' : decision.message
                          }
                          onClick={() => requestDelete(profile)}
                        >
                          删除
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </Table>
        </div>
      )}
      <Feedback notice={notice} />
      {canEdit && (
        <div className="mw-model-footer">
          <span className="mw-model-muted">
            {privateDirty ? '私有模型有未保存修改' : '私有模型已同步'}
          </span>
          <button
            type="button"
            className="btn"
            disabled={Boolean(busy) || !privateDirty}
            onClick={() =>
              setDraftProfiles(
                baselineProfiles.map((profile) => ({
                  ...profile,
                  api_key: '',
                })),
              )
            }
          >
            重置
          </button>
          <button
            type="button"
            className="btn btn-p"
            disabled={Boolean(busy) || !privateDirty}
            onClick={() => persistProfiles()}
          >
            {busy === 'save-profiles' ? '保存中…' : '保存私有模型'}
          </button>
        </div>
      )}
      {editor && (
        <ProfileEditor
          key={editor.profile?.id || `new-${workspaceId}`}
          scope="workspace"
          initial={editor.profile}
          profiles={draftProfiles}
          busy={Boolean(busy)}
          onClose={() => setEditor(null)}
          onSaveDraft={editDraft}
          onTestDraft={testEditorDraft}
        />
      )}
      {deleteTarget && (
        <DeleteDialog
          title="删除空间私有模型"
          profile={deleteTarget}
          busy={busy === 'save-profiles' || busy === 'check-delete'}
          onClose={() => {
            setDeleteTarget(null);
            setDeleteServerFingerprint('');
          }}
          onConfirm={confirmDelete}
        />
      )}
    </SectionCard>
  );
}

export const GlobalModelsPanel = GlobalModelSettings;
export const WorkspaceModelsPanel = WorkspaceModelSettings;
