import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { useAuthContext } from '../auth';
import { authFetch } from '../../shared/api/auth';
import {
  APPEARANCES,
  THEMES,
  applyAppearance,
  applyTheme,
  getAppearance,
  getTheme,
} from '../../shared/styles/theme';
import {
  FieldRow as Field,
  ScopeTabs,
  SectionCard,
} from '../../shared/ui/primitives';
import { ConfirmDialog } from '../../shared/ui/feedback';
import {
  WORKSPACE_ACCESS_STATE,
  configSectionDisabled,
  configSectionRequiresWorkspace,
  resolveWorkspaceAccess,
} from './configAccess';
import { CONFIG_CENTER_SCOPES as SCOPES } from './configCenterScopes';
import {
  saveWorkspaceMembers,
  validateWorkspaceMember,
} from './workspaceMembers/api';
import './settings.css';

const ChangePasswordDialog = lazy(() =>
  import('../auth').then((module) => ({
    default: module.ChangePasswordDialog,
  })),
);
const McpSettingsPanel = lazy(() => import('./mcp/McpSettingsPanel'));
const SystemSettingsPanel = lazy(() => import('./system/SystemSettingsPanel'));
const UserAdminPanel = lazy(() => import('./admin/UserAdminPanel'));
const WorkspaceAdminPanel = lazy(() => import('./admin/WorkspaceAdminPanel'));
const GlobalModelsPanel = lazy(() =>
  import('./models/ModelSettings').then((module) => ({
    default: module.GlobalModelsPanel,
  })),
);
const WorkspaceModelsPanel = lazy(() =>
  import('./models/ModelSettings').then((module) => ({
    default: module.WorkspaceModelsPanel,
  })),
);

export default function ConfigCenter({
  scope,
  section,
  onNavigate,
  ontologies = [],
  ontologiesLoading = false,
  ontologiesError = '',
  onRetryOntologies,
}) {
  const {
    workspaceDataReady,
    workspaceId,
    workspaces,
    workspacesError,
    workspacesLoaded,
    workspacesLoading,
  } = useAuthContext();
  const sc = SCOPES[scope] || SCOPES.global;
  const { sections } = sc;
  const [firstSection] = sections;
  const active = sections.find((s) => s.id === section) || firstSection;
  const workspaceScopedSection = configSectionRequiresWorkspace(
    scope,
    active.id,
  );
  const workspaceAccess = resolveWorkspaceAccess({
    workspaceDataReady,
    workspacesLoaded,
    workspacesLoading,
    workspacesError,
    workspaceId,
    workspaces,
  });

  return (
    <div>
      <ScopeTabs
        value={scope}
        onChange={(key) =>
          onNavigate({ scope: key, sec: SCOPES[key].sections[0].id })
        }
        items={Object.entries(SCOPES).map(([key, value]) => ({
          id: key,
          label: value.label,
          badge: value.lv,
          badgeClass: value.badge,
        }))}
      />
      <div className="scope-hint">{sc.hint}</div>
      <div className="cfg">
        <div className="subnav">
          {sections.map((s) => (
            <button
              key={s.id}
              className={s.id === active.id ? 'on' : ''}
              disabled={configSectionDisabled(scope, s.id, workspaceAccess)}
              title={
                configSectionDisabled(scope, s.id, workspaceAccess) ?
                  '需要先创建或选择工作空间' :
                  undefined
              }
              onClick={() => onNavigate({ scope, sec: s.id })}
            >
              {s.name}
            </button>
          ))}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Suspense
            fallback={
              <SectionCard>
                <div className="empty" role="status">
                  正在加载配置模块…
                </div>
              </SectionCard>
            }
          >
            <SectionBody
              key={
                workspaceScopedSection ?
                  `${workspaceId}:${active.id}` :
                  `${scope}:${active.id}`
              }
              scope={scope}
              section={active.id}
              workspaceAccess={workspaceAccess}
              workspaceError={workspacesError}
              onNavigate={onNavigate}
              ontologies={ontologies}
              ontologiesLoading={ontologiesLoading}
              ontologiesError={ontologiesError}
              onRetryOntologies={onRetryOntologies}
            />
          </Suspense>
        </div>
      </div>
    </div>
  );
}

function ReadOnlyBanner({ who }) {
  return (
    <div className="ro-banner">
      🔒{' '}
      <span>
        当前为<b>只读模式</b>——此层配置仅 <b>{who}</b>{' '}
        可修改。你可以查看全部配置项，如需变更请联系管理员。
      </span>
    </div>
  );
}

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
function SectionBody({
  scope,
  section,
  workspaceAccess,
  workspaceError,
  onNavigate,
  ontologies,
  ontologiesLoading,
  ontologiesError,
  onRetryOntologies,
}) {
  const {
    can,
    changePassword,
    reloadWorkspaces,
    switchWorkspace,
    user,
    workspaceId,
    workspaces,
  } = useAuthContext();
  const workspace = workspaces.find((item) => item.id === workspaceId);
  if (configSectionDisabled(scope, section, workspaceAccess)) {
    return (
      <WorkspaceAccessGate
        state={workspaceAccess}
        error={workspaceError}
        onNavigate={onNavigate}
        onRetry={reloadWorkspaces}
      />
    );
  }
  if (scope === 'global') {
    if (section === 'llm') {
      return (
        <GlobalModelsPanel
          canEdit={can('system_config.write')}
          workspaces={workspaces}
        />
      );
    }
    if (section === 'system') {
      return <SystemSettingsPanel canEdit={can('system_config.write')} />;
    }
    if (section === 'wsmgmt') {
      return (
        <WorkspaceAdminPanel
          currentUser={user}
          currentWorkspaceId={workspaceId}
          onWorkspaceChange={switchWorkspace}
          onWorkspacesChanged={reloadWorkspaces}
        />
      );
    }
    if (section === 'users') {
      return (
        <UserAdminPanel currentUser={user} canManage={can('users.manage')} />
      );
    }
    if (section === 'platform') {
      return <PlatformAccess />;
    }
  }
  if (scope === 'workspace') {
    if (section === 'info') {
      return <WorkspaceInfo />;
    }
    // key=workspaceId：切换空间时整体重挂，丢弃未保存的成员草稿，避免切回复活旧草稿覆盖他人变更
    if (section === 'members') {
      return <WorkspaceMembers key={workspaceId} />;
    }
    if (section === 'model') {
      return (
        <WorkspaceModelsPanel
          workspaceId={workspaceId}
          canEdit={can('ws.config.write')}
        />
      );
    }
    if (section === 'runtime') {
      return <WorkspaceRuntime />;
    }
    if (section === 'wsobs') {
      return <WorkspaceObservability />;
    }
    if (section === 'mcp') {
      return (
        <McpSettingsPanel
          workspaceId={workspaceId}
          workspaceName={workspace?.name || workspaceId}
          authenticated={Boolean(user?.username)}
          canManageTokens={Boolean(user?.username && workspaceId)}
          ontologies={ontologies}
          ontologiesLoading={ontologiesLoading}
          ontologiesError={ontologiesError}
          onRetryOntologies={onRetryOntologies}
        />
      );
    }
  }
  if (scope === 'personal') {
    if (section === 'prefs') {
      return <PersonalPrefs />;
    }
    if (section === 'memory') {
      return <PersonalMemory />;
    }
    if (section === 'account') {
      return <AccountSecurity user={user} changePassword={changePassword} />;
    }
  }
  return null;
}

function WorkspaceAccessGate({ state, error, onNavigate, onRetry }) {
  if (state === WORKSPACE_ACCESS_STATE.LOADING) {
    return (
      <SectionCard title="正在加载工作空间">
        <div className="empty" role="status">
          工作空间加载完成后即可使用此配置。
        </div>
      </SectionCard>
    );
  }
  if (state === WORKSPACE_ACCESS_STATE.ERROR) {
    return (
      <SectionCard
        title="工作空间加载失败"
        description={error || '暂时无法确认当前工作空间。'}
      >
        <div className="empty">
          <button className="btn" type="button" onClick={onRetry}>
            重新加载
          </button>
        </div>
      </SectionCard>
    );
  }
  if (state === WORKSPACE_ACCESS_STATE.UNAVAILABLE) {
    return (
      <SectionCard
        title="当前工作空间不可用"
        description="当前选择已失效，请重新加载工作空间列表。"
      >
        <div className="empty">
          <button className="btn" type="button" onClick={onRetry}>
            重新加载
          </button>
        </div>
      </SectionCard>
    );
  }
  return (
    <SectionCard
      title="尚未创建工作空间"
      description="创建工作空间后，才能使用空间配置与个人长期记忆。"
    >
      <div className="empty">
        <button
          className="btn btn-p"
          type="button"
          onClick={() => onNavigate({ scope: 'global', sec: 'wsmgmt' })}
        >
          前往工作空间管理
        </button>
      </div>
    </SectionCard>
  );
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
function PlatformAccess() {
  const { can } = useAuthContext();
  const canEdit = can('system_config.write');
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [msg, setMsg] = useState('');
  const effective = { ...(config || {}), ...draft };
  const load = useCallback(async () => {
    const response = await authFetch('/api/platform-config');
    if (response.ok) {
      setConfig(await response.json());
      setDraft({});
    }
  }, []);
  useEffect(() => {
    const timer = setTimeout(load, 0);
    return () => clearTimeout(timer);
  }, [load]);
  const set = (key, value) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const request = async (path) => {
    const payload = { ...draft };
    if (!payload.api_key) {
      delete payload.api_key;
    }
    const response = await authFetch(path, {
      method: path.endsWith('/test') ? 'POST' : 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    setMsg(
      response.ok ?
        data.message || '已保存' :
        `操作失败：${data.detail || data.error || response.status}`,
    );
    if (response.ok && !path.endsWith('/test')) {
      await load();
    }
  };
  if (!config) {
    return (
      <SectionCard>
        <div className="empty">加载中…</div>
      </SectionCard>
    );
  }
  return (
    <SectionCard
      title="运维监控平台接入"
      description="统一配置运行实例注册、策略快照和 Trace 上报目标。"
    >
      {!canEdit && <ReadOnlyBanner who="平台管理员" />}
      <Field label="启用平台接入">
        <Toggle
          on={Boolean(effective.enabled)}
          disabled={!canEdit}
          onChange={(value) => set('enabled', value)}
        />
      </Field>
      <Field label="平台地址">
        <input
          disabled={!canEdit}
          value={effective.base_url || ''}
          onChange={(event) => set('base_url', event.target.value)}
        />
      </Field>
      <Field label="环境标识">
        <input
          disabled={!canEdit}
          value={effective.environment_id || ''}
          onChange={(event) => set('environment_id', event.target.value)}
        />
      </Field>
      <Field label="实例标识" hint="留空时启用后自动生成">
        <input
          disabled={!canEdit}
          value={effective.instance_id || ''}
          onChange={(event) => set('instance_id', event.target.value)}
        />
      </Field>
      <Field label="组织标识">
        <input
          disabled={!canEdit}
          value={effective.organization_id || ''}
          onChange={(event) => set('organization_id', event.target.value)}
        />
      </Field>
      <Field
        label="环境 Key"
        hint={config.has_api_key ? '已配置，留空保留' : '尚未配置'}
      >
        <input
          type="password"
          disabled={!canEdit}
          value={draft.api_key || ''}
          onChange={(event) => set('api_key', event.target.value)}
        />
      </Field>
      <Field label="批量上报条数">
        <input
          type="number"
          disabled={!canEdit}
          value={effective.batch_size || 20}
          onChange={(event) => set('batch_size', Number(event.target.value))}
        />
      </Field>
      <div className="foot">
        <span className="who">{msg}</span>
        {canEdit && (
          <>
            <button
              className="btn"
              onClick={() => request('/api/platform-config/test')}
            >
              测试连接
            </button>
            <button
              className="btn btn-p"
              disabled={!Object.keys(draft).length}
              onClick={() => request('/api/platform-config')}
            >
              保存
            </button>
          </>
        )}
      </div>
    </SectionCard>
  );
}

/* ---------------- L2 · 工作空间各面板 ---------------- */

function WorkspaceInfo() {
  const { can, workspaceId, workspaces, reloadWorkspaces } = useAuthContext();
  const canEdit = can('ws.info.write');
  const ws = workspaces.find((w) => w.id === workspaceId);
  const [draft, setDraft] = useState(null);
  const [msg, setMsg] = useState('');
  const current = draft ?? {
    name: ws?.name || '',
    description: ws?.description || '',
  };

  const save = async () => {
    const res = await authFetch(
      `/agentic/workspaces/${encodeURIComponent(workspaceId)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(current),
      },
    );
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已保存' : `失败：${data.detail || res.status}`);
    if (res.ok) {
      setDraft(null);
      reloadWorkspaces();
    }
  };

  return (
    <div className="card">
      <h3>空间信息</h3>
      <div className="cd">
        修改空间名称与描述；新建 / 删除空间请到 全局配置 → 工作空间管理
      </div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      <Field label="ID">
        <span style={{ fontSize: 13, color: 'var(--ink2)' }}>
          {workspaceId || '—'}
        </span>
      </Field>
      <Field label="名称">
        <input
          type="text"
          disabled={!canEdit}
          value={current.name}
          onChange={(e) => setDraft({ ...current, name: e.target.value })}
        />
      </Field>
      <Field label="描述">
        <input
          type="text"
          disabled={!canEdit}
          value={current.description}
          onChange={(e) =>
            setDraft({ ...current, description: e.target.value })
          }
        />
      </Field>
      {canEdit && (
        <div className="foot">
          <span className="who">{msg}</span>
          <button
            className="btn"
            disabled={!draft}
            onClick={() => setDraft(null)}
          >
            重置
          </button>
          <button className="btn btn-p" disabled={!draft} onClick={save}>
            保存修改
          </button>
        </div>
      )}
    </div>
  );
}

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
function WorkspaceMembers() {
  const { can, workspaceId, workspaces, reloadWorkspaces } = useAuthContext();
  const canEdit = can('ws.members.write');
  const [fetched, setFetched] = useState(null);
  const ws = workspaces.find((w) => w.id === workspaceId) || fetched;
  // draft 记录所属空间：切换空间后自动失效回退到服务端数据
  const [draftState, setDraftState] = useState(null);
  const draft =
    draftState && draftState.wsId === workspaceId ? draftState.members : null;
  const setDraft = (members) =>
    setDraftState(members === null ? null : { wsId: workspaceId, members });
  const [newName, setNewName] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState('');
  const members = draft ?? (ws?.members || []);
  const msgIsError = msg.startsWith('失败：');

  // 优先复用 AuthContext 已拉取的条目；找不到当前空间时兜底重拉一次列表
  useEffect(() => {
    if (workspaces.some((w) => w.id === workspaceId)) {
      return undefined;
    }
    const t = setTimeout(async () => {
      const res = await authFetch('/agentic/workspaces');
      if (!res.ok) {
        return;
      }
      const data = await res.json().catch(() => ({}));
      setFetched(
        (data.workspaces || []).find((w) => w.id === workspaceId) || null,
      );
    }, 0);
    return () => clearTimeout(t);
  }, [workspaces, workspaceId]);

  const add = async () => {
    const name = newName.trim();
    if (!name || busy) {
      return;
    }
    if (members.includes(name)) {
      setMsg(`「${name}」已在成员列表`);
      return;
    }
    setMsg('');
    setBusy('validate');
    try {
      await validateWorkspaceMember(workspaceId, name);
      setDraft([...members, name]);
      setNewName('');
    } catch (error) {
      setMsg(`失败：${error?.message || '成员校验失败'}`);
    } finally {
      setBusy('');
    }
  };
  const removeMember = (name) => {
    if (!busy) {
      setDraft(members.filter((m) => m !== name));
    }
  };

  const save = async () => {
    if (busy) {
      return;
    }
    setBusy('save');
    setMsg('');
    try {
      await saveWorkspaceMembers(workspaceId, members);
      setMsg('已保存');
      setDraft(null);
      await reloadWorkspaces();
    } catch (error) {
      setMsg(`失败：${error?.message || '保存成员失败'}`);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="card">
      <h3>成员</h3>
      <div className="cd">
        管理本空间的成员；成员可访问本空间的数据与知识。变更空间负责人需平台管理员操作
      </div>
      {!canEdit && <ReadOnlyBanner who="平台管理员或空间负责人" />}
      <Field label="空间负责人" hint="可管理本空间的成员与设置">
        <span style={{ fontSize: 13, color: 'var(--ink2)' }}>
          {ws?.owner || '—（公共空间，所有登录用户可访问）'}
        </span>
      </Field>
      {canEdit && (
        <div
          style={{
            display: 'flex',
            gap: 8,
            margin: '12px 0',
            alignItems: 'center',
          }}
        >
          <input
            type="text"
            placeholder="用户名"
            style={inputS(160)}
            value={newName}
            disabled={Boolean(busy)}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                add();
              }
            }}
          />
          <button
            className="btn btn-p"
            disabled={!newName.trim() || Boolean(busy)}
            onClick={add}
          >
            {busy === 'validate' ? '校验中…' : '＋ 添加成员'}
          </button>
        </div>
      )}
      {msg && (
        <div
          role={msgIsError ? 'alert' : 'status'}
          style={{
            fontSize: 12.5,
            color: msgIsError ? 'var(--status-error-fg)' : 'var(--ink2)',
            marginBottom: 8,
          }}
        >
          {msg}
        </div>
      )}
      <table className="dt">
        <thead>
          <tr>
            <th>成员</th>
            {canEdit && <th style={{ width: 100 }}>操作</th>}
          </tr>
        </thead>
        <tbody>
          {members.map((m) => (
            <tr key={m}>
              <td style={{ fontWeight: 600 }}>{m}</td>
              {canEdit && (
                <td>
                  <button
                    className="btn"
                    style={btnS}
                    disabled={Boolean(busy)}
                    onClick={() => removeMember(m)}
                  >
                    移除
                  </button>
                </td>
              )}
            </tr>
          ))}
          {!members.length && (
            <tr>
              <td colSpan={canEdit ? 2 : 1} className="empty">
                暂无成员{ws?.owner ? '（仅空间负责人与管理员可访问）' : ''}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {canEdit && (
        <div className="foot">
          <button
            className="btn"
            disabled={!draft || Boolean(busy)}
            onClick={() => setDraft(null)}
          >
            重置
          </button>
          <button
            className="btn btn-p"
            disabled={!draft || Boolean(busy)}
            onClick={save}
          >
            {busy === 'save' ? '保存中…' : '保存修改'}
          </button>
        </div>
      )}
    </div>
  );
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
function WorkspaceRuntime() {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.config.write');
  const [cfg, setCfg] = useState(null);
  const [draft, setDraft] = useState(null);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    (async () => {
      const res = await authFetch('/agentic/workspaces/current/runtime-config');
      if (res.ok) {
        setCfg(await res.json());
      }
    })();
  }, [workspaceId]);

  if (!cfg) {
    return (
      <div className="card">
        <div className="empty">加载中…</div>
      </div>
    );
  }
  const current = draft ?? {
    lang: { enabled: !!cfg.lang?.enabled, default: cfg.lang?.default || 'zh' },
    acp_suppress_script_patterns: Array.isArray(
      cfg.acp_suppress_script_patterns,
    ) ?
      cfg.acp_suppress_script_patterns.join(', ') :
      cfg.acp_suppress_script_patterns || '',
  };

  const save = async () => {
    const res = await authFetch(
      `/agentic/workspaces/${encodeURIComponent(workspaceId)}/runtime-config`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lang: current.lang,
          acp_suppress_script_patterns: current.acp_suppress_script_patterns,
        }),
      },
    );
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已保存' : `失败：${data.detail || res.status}`);
    if (res.ok) {
      setCfg(data);
      setDraft(null);
    }
  };

  return (
    <div className="card">
      <h3>空间运行参数</h3>
      <div className="cd">控制回复语言，以及哪些工具输出对用户隐藏</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      <Field label="启用语言控制">
        <Toggle
          on={current.lang.enabled}
          disabled={!canEdit}
          onChange={(v) =>
            setDraft({ ...current, lang: { ...current.lang, enabled: v } })
          }
        />
      </Field>
      <Field label="默认回复语言" hint="当请求和个人偏好都没指定语言时使用">
        <select
          disabled={!canEdit}
          value={current.lang.default}
          onChange={(e) =>
            setDraft({
              ...current,
              lang: { ...current.lang, default: e.target.value },
            })
          }
        >
          <option value="zh">zh</option>
          <option value="en">en</option>
        </select>
      </Field>
      <Field
        label="隐藏工具输出的关键词"
        hint="多个关键词用逗号分隔；命中的工具输出不展示给用户"
      >
        <input
          type="text"
          disabled={!canEdit}
          value={current.acp_suppress_script_patterns}
          onChange={(e) =>
            setDraft({
              ...current,
              acp_suppress_script_patterns: e.target.value,
            })
          }
        />
      </Field>
      {canEdit && (
        <div className="foot">
          <span className="who">{msg}</span>
          <button
            className="btn"
            disabled={!draft}
            onClick={() => setDraft(null)}
          >
            重置
          </button>
          <button className="btn btn-p" disabled={!draft} onClick={save}>
            保存修改
          </button>
        </div>
      )}
    </div>
  );
}

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
function WorkspaceObservability() {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.observability.write');
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [msg, setMsg] = useState('');
  const effective = {
    ...(config || {}),
    ...draft,
    local: { ...(config?.local || {}), ...(draft.local || {}) },
    speedscope: { ...(config?.speedscope || {}), ...(draft.speedscope || {}) },
    langfuse: { ...(config?.langfuse || {}), ...(draft.langfuse || {}) },
  };
  const load = useCallback(async () => {
    const response = await authFetch(
      `/agentic/workspaces/${encodeURIComponent(workspaceId)}/observability-config`,
    );
    if (response.ok) {
      setConfig(await response.json());
      setDraft({});
    }
  }, [workspaceId]);
  useEffect(() => {
    const timer = setTimeout(load, 0);
    return () => clearTimeout(timer);
  }, [load]);
  const set = (key, value) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const setNested = (section, key, value) =>
    setDraft((current) => ({
      ...current,
      [section]: { ...(current[section] || {}), [key]: value },
    }));
  const submit = async (testOnly = false) => {
    const payload = { ...draft };
    if (payload.langfuse && !payload.langfuse.secret_key) {
      payload.langfuse = { ...payload.langfuse };
      delete payload.langfuse.secret_key;
    }
    const suffix = testOnly ? '/test' : '';
    const response = await authFetch(
      `/agentic/workspaces/${encodeURIComponent(workspaceId)}/observability-config${suffix}`,
      {
        method: testOnly ? 'POST' : 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    const data = await response.json().catch(() => ({}));
    setMsg(
      response.ok ?
        data.message || '已保存' :
        `操作失败：${data.detail || data.error || response.status}`,
    );
    if (response.ok && !testOnly) {
      await load();
    }
  };
  if (!config) {
    return (
      <SectionCard>
        <div className="empty">加载中…</div>
      </SectionCard>
    );
  }
  return (
    <SectionCard
      title="工作空间观测"
      description="为当前空间选择本地日志或独立 Langfuse 项目。"
    >
      {!canEdit && <ReadOnlyBanner who="平台管理员" />}
      <Field label="启用观测">
        <Toggle
          on={Boolean(effective.enabled)}
          disabled={!canEdit}
          onChange={(value) => set('enabled', value)}
        />
      </Field>
      <Field label="观测模式">
        <select
          disabled={!canEdit}
          value={effective.mode || 'local'}
          onChange={(event) => set('mode', event.target.value)}
        >
          <option value="local">本地日志</option>
          <option value="langfuse">Langfuse</option>
        </select>
      </Field>
      <Field label="Speedscope 保留天数">
        <input
          type="number"
          disabled={!canEdit}
          value={effective.speedscope.retention_days || 30}
          onChange={(event) =>
            setNested(
              'speedscope',
              'retention_days',
              Number(event.target.value),
            )
          }
        />
      </Field>
      {effective.mode === 'local' ? (
        <Field label="日志保留天数">
          <input
            type="number"
            disabled={!canEdit}
            value={effective.local.retention_days || 30}
            onChange={(event) =>
              setNested('local', 'retention_days', Number(event.target.value))
            }
          />
        </Field>
      ) : (
        <>
          <Field label="Langfuse 地址">
            <input
              disabled={!canEdit}
              value={effective.langfuse.base_url || ''}
              onChange={(event) =>
                setNested('langfuse', 'base_url', event.target.value)
              }
            />
          </Field>
          <Field label="Public Key">
            <input
              disabled={!canEdit}
              value={effective.langfuse.public_key || ''}
              onChange={(event) =>
                setNested('langfuse', 'public_key', event.target.value)
              }
            />
          </Field>
          <Field
            label="Secret Key"
            hint={
              config.langfuse?.has_secret_key ? '已配置，留空保留' : '尚未配置'
            }
          >
            <input
              type="password"
              disabled={!canEdit}
              value={draft.langfuse?.secret_key || ''}
              onChange={(event) =>
                setNested('langfuse', 'secret_key', event.target.value)
              }
            />
          </Field>
        </>
      )}
      <div className="foot">
        <span className="who">{msg}</span>
        {canEdit && (
          <>
            <button className="btn" onClick={() => submit(true)}>
              测试
            </button>
            <button
              className="btn btn-p"
              disabled={!Object.keys(draft).length}
              onClick={() => submit(false)}
            >
              保存
            </button>
          </>
        )}
      </div>
    </SectionCard>
  );
}

/* ---------------- L3 · 个人设置 ---------------- */

function PersonalPrefs() {
  const { workspaces, workspaceId, switchWorkspace } = useAuthContext();
  const [lang, setLang] = useState(
    () => localStorage.getItem('mw_pref_reply_lang') || '',
  );
  const [theme, setTheme] = useState(() => getTheme());
  const [appearance, setAppearance] = useState(() => getAppearance());

  const pickTheme = (id) => setTheme(applyTheme(id));
  const pickAppearance = (id) =>
    setAppearance(applyAppearance(id, { animate: true }));
  const selectedWorkspaceId = workspaces.some(
    (workspace) => workspace.id === workspaceId,
  ) ?
    workspaceId :
    '';

  return (
    <div className="card" data-wiz="cfg-prefs">
      <h3>偏好</h3>
      <div className="cd">以下偏好当前保存在你的浏览器</div>
      <Field label="当前工作空间" hint="切换后全站生效">
        <select
          value={selectedWorkspaceId}
          disabled={!workspaces.length}
          onChange={(e) => switchWorkspace(e.target.value)}
        >
          {!workspaces.length && <option value="">暂无可用工作空间</option>}
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}（{w.id}）
            </option>
          ))}
        </select>
      </Field>
      <Field label="回复语言偏好" hint="优先于工作空间的语言设置">
        <select
          value={lang}
          onChange={(e) => {
            setLang(e.target.value);
            localStorage.setItem('mw_pref_reply_lang', e.target.value);
          }}
        >
          <option value="">跟随空间</option>
          <option value="zh">中文</option>
          <option value="en">English</option>
        </select>
      </Field>
      <Field label="主题配色" hint="即时生效，全站应用">
        <div className="theme-options">
          {THEMES.map((t) => (
            <button
              className="theme-choice"
              key={t.id}
              type="button"
              onClick={() => pickTheme(t.id)}
              title={t.label}
              aria-pressed={theme === t.id}
              style={{
                border:
                  theme === t.id ?
                    `2px solid ${t.color}` :
                    '1px solid var(--hair)',
                color: theme === t.id ? t.color : 'var(--ink2)',
              }}
            >
              <span
                style={{
                  width: 14,
                  height: 14,
                  borderRadius: '50%',
                  background: t.color,
                  flex: 'none',
                }}
              />
              {t.label}
            </button>
          ))}
        </div>
      </Field>
      <Field label="外观模式" hint="跟随系统会响应操作系统的浅色或深色变化">
        <div className="appearance-options">
          {APPEARANCES.map((item) => (
            <button
              className={appearance === item.id ? 'on' : ''}
              key={item.id}
              type="button"
              aria-pressed={appearance === item.id}
              onClick={() => pickAppearance(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </Field>
    </div>
  );
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
function PersonalMemory() {
  const { workspaceId } = useAuthContext();
  const [memories, setMemories] = useState([]);
  const [enabled, setEnabled] = useState(true);
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(null);
  const [msg, setMsg] = useState('');
  const [pendingRemoval, setPendingRemoval] = useState(null);
  const load = useCallback(async () => {
    const response = await authFetch('/agentic/memories?limit=200');
    const payload = await response.json().catch(() => ({}));
    if (response.ok) {
      setMemories(payload.memories || []);
      setEnabled(payload.memory_enabled !== false);
    } else {
      setMsg(payload.error || `加载失败：${response.status}`);
    }
  }, []);
  useEffect(() => {
    const timer = setTimeout(load, 0);
    return () => clearTimeout(timer);
  }, [load, workspaceId]);
  const save = async () => {
    const content = draft.trim();
    if (content.length < 8) {
      setMsg('记忆内容至少 8 个字');
      return;
    }
    const response = await authFetch(
      editing ? `/agentic/memories/${editing.id}` : '/agentic/memories',
      {
        method: editing ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      },
    );
    const payload = await response.json().catch(() => ({}));
    setMsg(
      response.ok ?
        '记忆已保存' :
        `保存失败：${payload.error || response.status}`,
    );
    if (response.ok) {
      setDraft('');
      setEditing(null);
      await load();
    }
  };
  const remove = async (memory) => {
    const response = await authFetch(`/agentic/memories/${memory.id}`, {
      method: 'DELETE',
    });
    setMsg(response.ok ? '记忆已删除' : '删除失败');
    if (response.ok) {
      await load();
    }
  };
  return (
    <SectionCard
      title="长期记忆"
      description="查看、补充和修订当前账号的偏好与画像记忆。"
    >
      {!enabled && (
        <div className="ro-banner">当前运行环境未启用长期记忆。</div>
      )}
      {enabled && (
        <div className="memory-editor">
          <textarea
            rows={3}
            value={draft}
            placeholder="例如：我偏好用中文输出，并把风险项放在结论前。"
            onChange={(event) => setDraft(event.target.value)}
          />
          <div>
            <button
              className="btn"
              disabled={!editing}
              onClick={() => {
                setEditing(null);
                setDraft('');
              }}
            >
              取消编辑
            </button>
            <button
              className="btn btn-p"
              disabled={draft.trim().length < 8}
              onClick={save}
            >
              {editing ? '保存修改' : '新增记忆'}
            </button>
          </div>
        </div>
      )}
      <table className="dt">
        <thead>
          <tr>
            <th>类型</th>
            <th>记忆内容</th>
            <th>置信度</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {memories.map((memory) => (
            <tr key={memory.id}>
              <td>
                <span className="tag tag-lang">
                  {memory.memory_type || 'preference'}
                </span>
              </td>
              <td>{memory.content}</td>
              <td>
                {memory.confidence !== undefined ?
                  Number(memory.confidence).toFixed(2) :
                  '—'}
              </td>
              <td>
                <button
                  className="btn"
                  style={btnS}
                  onClick={() => {
                    setEditing(memory);
                    setDraft(memory.content || '');
                  }}
                >
                  编辑
                </button>
                <button
                  className="btn btn-danger"
                  style={btnS}
                  onClick={() => setPendingRemoval(memory)}
                >
                  删除
                </button>
              </td>
            </tr>
          ))}
          {!memories.length && (
            <tr>
              <td className="empty" colSpan={4}>
                暂无长期记忆
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {msg && (
        <div className="who" style={{ marginTop: 10 }}>
          {msg}
        </div>
      )}
      <ConfirmDialog
        open={Boolean(pendingRemoval)}
        title="删除长期记忆"
        description="删除后将无法恢复这条长期记忆。"
        confirmLabel="删除记忆"
        danger
        onConfirm={() => {
          const memory = pendingRemoval;
          setPendingRemoval(null);
          if (memory) {
            remove(memory);
          }
        }}
        onCancel={() => setPendingRemoval(null)}
      />
    </SectionCard>
  );
}

function AccountSecurity({ user, changePassword }) {
  const [dialogOpen, setDialogOpen] = useState(false);
  return (
    <div className="card">
      <h3>账号安全</h3>
      <p className="muted">
        密码变更成功后会退出当前会话，需要使用新密码重新登录。
      </p>
      <Field label="用户名">
        <span style={{ fontSize: 13, color: 'var(--ink2)' }}>
          {user?.username}
        </span>
      </Field>
      <div className="foot">
        <button
          className="btn btn-p"
          type="button"
          onClick={() => setDialogOpen(true)}
        >
          修改密码
        </button>
      </div>
      {dialogOpen && (
        <ChangePasswordDialog
          currentUser={user}
          onChangePassword={changePassword}
          onClose={() => setDialogOpen(false)}
        />
      )}
    </div>
  );
}

/* ---------------- 小组件 ---------------- */

function Toggle({ on, disabled, onChange, ...accessibilityProps }) {
  return (
    <button
      type="button"
      disabled={disabled}
      aria-pressed={on}
      {...accessibilityProps}
      onClick={() => onChange(!on)}
      style={{
        width: 38,
        height: 22,
        borderRadius: 99,
        border: 'none',
        position: 'relative',
        flex: 'none',
        background: on ? 'var(--blue)' : '#d8d7d0',
        opacity: disabled ? 0.55 : 1,
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'background .15s',
      }}
    >
      <span
        style={{
          position: 'absolute',
          top: 2.5,
          left: on ? 18 : 3,
          width: 17,
          height: 17,
          borderRadius: '50%',
          background: '#fff',
          transition: 'left .15s',
        }}
      />
    </button>
  );
}

const inputS = (w) => ({
  border: '1px solid var(--control-border)',
  borderRadius: 8,
  padding: '7px 11px',
  width: w,
  background: 'var(--control-bg)',
  color: 'var(--control-fg)',
});
const btnS = { padding: '3px 10px', fontSize: 12, marginRight: 6 };
