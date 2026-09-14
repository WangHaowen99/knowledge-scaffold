// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { Modal } from '../../../shared/ui/primitives';
import { authFetch } from '../../../shared/api/auth';
import {
  canCreateWorkspace,
  canDeleteWorkspace,
  canEditWorkspace,
  canManageWorkspaceCollection,
  chooseWorkspaceFallback,
  classifyWorkspaceDeleteResult,
  DEFAULT_WORKSPACE_PAGE_SIZE,
  filterAndPaginateWorkspaces,
  normalizeWorkspaceList,
  workspaceMutationPayload,
} from './workspaceModel';
import './adminSettings.css';

const EMPTY_DRAFT = Object.freeze({
  id: '',
  name: '',
  description: '',
  setAsDefault: false,
});

async function responsePayload(response) {
  return response.json().catch(() => ({}));
}

function responseMessage(response, payload, fallback) {
  return (
    payload?.error ||
    payload?.detail ||
    (response.ok ? '' : `${fallback}（${response.status}）`)
  );
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
function WorkspaceDialog({
  dialog,
  draft,
  setDraft,
  busy,
  canSetDefault,
  error,
  onClose,
  onSubmit,
}) {
  const isCreate = dialog.type === 'create';
  return (
    <Modal
      title={
        isCreate ?
          '新建工作空间' :
          `编辑 ${dialog.workspace?.name || dialog.workspace?.id}`
      }
      description={
        isCreate ?
          '工作空间是一个隔离知识库、Agent 与配置的独立工作环境' :
          '仅更新名称与描述，不改变成员和所有者。'
      }
      onClose={busy ? undefined : onClose}
      actions={
        <>
          <button
            className="btn"
            type="button"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button
            className="btn btn-p"
            type="submit"
            form="workspace-admin-form"
            disabled={busy || !draft.name.trim()}
          >
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error && (
        <div className="settings-admin-alert is-error" role="alert">
          {error}
        </div>
      )}
      <form
        id="workspace-admin-form"
        className="settings-admin-form"
        onSubmit={onSubmit}
      >
        <label>
          名称
          <input
            autoFocus
            required
            maxLength={120}
            placeholder="取一个有意义的空间名称..."
            value={draft.name}
            disabled={busy}
            onChange={(event) =>
              setDraft((current) => ({ ...current, name: event.target.value }))
            }
          />
        </label>
        <label>
          描述
          <textarea
            rows={4}
            maxLength={500}
            placeholder="说说这个空间是做什么的..."
            value={draft.description}
            disabled={busy}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                description: event.target.value,
              }))
            }
          />
        </label>
        {isCreate && canSetDefault && (
          <label className="settings-admin-check">
            <input
              type="checkbox"
              checked={draft.setAsDefault}
              disabled={busy}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  setAsDefault: event.target.checked,
                }))
              }
            />
            创建后设为默认工作空间
          </label>
        )}
      </form>
    </Modal>
  );
}

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export default function WorkspaceAdminPanel({
  currentUser,
  currentWorkspaceId = '',
  onWorkspaceChange,
  onWorkspacesChanged,
  pageSize = DEFAULT_WORKSPACE_PAGE_SIZE,
  request = authFetch,
}) {
  const [workspaces, setWorkspaces] = useState([]);
  const [defaultWorkspaceId, setDefaultWorkspaceId] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState(null);
  const [dialog, setDialog] = useState(null);
  const [draft, setDraft] = useState({ ...EMPTY_DRAFT });
  const loadSequenceRef = useRef(0);
  const loadControllerRef = useRef(null);
  const mutationControllerRef = useRef(null);
  const canCreate = canCreateWorkspace(currentUser);
  const canManageCollection = canManageWorkspaceCollection(currentUser);

  const loadWorkspaces = useCallback(
    async ({ quiet = false } = {}) => {
      loadControllerRef.current?.abort();
      const controller = new AbortController();
      const sequence = ++loadSequenceRef.current;
      loadControllerRef.current = controller;
      if (!quiet) {
        setLoading(true);
      }
      setError('');
      try {
        const response = await request('/agentic/workspaces', {
          signal: controller.signal,
        });
        const payload = await responsePayload(response);
        if (!response.ok) {
          throw new Error(
            responseMessage(response, payload, '加载工作空间失败'),
          );
        }
        const normalized = normalizeWorkspaceList(payload);
        if (controller.signal.aborted || sequence !== loadSequenceRef.current) {
          return null;
        }
        setWorkspaces(normalized.workspaces);
        setDefaultWorkspaceId(normalized.defaultWorkspaceId);
        await onWorkspacesChanged?.(normalized);
        return normalized;
      } catch (loadError) {
        if (
          loadError?.name !== 'AbortError' &&
          sequence === loadSequenceRef.current
        ) {
          setError(loadError?.message || '加载工作空间失败');
        }
        return null;
      } finally {
        if (!quiet && sequence === loadSequenceRef.current) {
          setLoading(false);
        }
      }
    },
    [onWorkspacesChanged, request],
  );

  useEffect(() => {
    loadWorkspaces();
    return () => {
      loadControllerRef.current?.abort();
      mutationControllerRef.current?.abort();
    };
  }, [loadWorkspaces]);

  const paged = useMemo(
    () =>
      filterAndPaginateWorkspaces(workspaces, {
        query,
        page,
        pageSize,
      }),
    [page, pageSize, query, workspaces],
  );

  const openCreate = () => {
    setDraft({ ...EMPTY_DRAFT });
    setDialog({ type: 'create' });
    setError('');
  };

  const openEdit = (workspace) => {
    setDraft({
      id: workspace.id,
      name: workspace.name,
      description: workspace.description,
      setAsDefault: false,
    });
    setDialog({ type: 'edit', workspace });
    setError('');
  };

  const closeDialog = () => {
    if (busy) {
      return;
    }
    setDialog(null);
    setDraft({ ...EMPTY_DRAFT });
  };

  const runMutation = async (actionKey, url, options, successMessage) => {
    mutationControllerRef.current?.abort();
    const controller = new AbortController();
    mutationControllerRef.current = controller;
    setBusy(actionKey);
    setError('');
    setNotice(null);
    try {
      const response = await request(url, {
        ...options,
        signal: controller.signal,
      });
      const payload = await responsePayload(response);
      if (!response.ok) {
        throw new Error(responseMessage(response, payload, successMessage));
      }
      if (!controller.signal.aborted) {
        setNotice({ tone: 'success', message: successMessage });
      }
      return { response, payload };
    } catch (mutationError) {
      if (mutationError?.name !== 'AbortError') {
        setError(mutationError?.message || `${successMessage}失败`);
      }
      return null;
    } finally {
      if (!controller.signal.aborted) {
        setBusy('');
      }
    }
  };

  const saveWorkspace = async (event) => {
    event.preventDefault();
    if (!dialog || busy) {
      return;
    }
    const isCreate = dialog.type === 'create';
    const id = isCreate ? '' : dialog.workspace.id;
    const body = isCreate ?
      {
        ...workspaceMutationPayload(draft),
        ...(canManageCollection ?
          { set_as_default: draft.setAsDefault } :
          {}),
      } :
      workspaceMutationPayload(draft);
    const result = await runMutation(
      `${dialog.type}:${id}`,
      isCreate ?
        '/agentic/workspaces' :
        `/agentic/workspaces/${encodeURIComponent(id)}`,
      {
        method: isCreate ? 'POST' : 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
      isCreate ? '工作空间已创建' : '工作空间已更新',
    );
    if (!result) {
      return;
    }
    setDialog(null);
    setDraft({ ...EMPTY_DRAFT });
    await loadWorkspaces({ quiet: true });
    if (isCreate) {
      const newId = result.payload?.id || id;
      await onWorkspaceChange?.(newId);
    }
  };

  const setDefault = async (workspace) => {
    const result = await runMutation(
      `default:${workspace.id}`,
      `/agentic/workspaces/${encodeURIComponent(workspace.id)}/default`,
      { method: 'POST' },
      `${workspace.name} 已设为默认工作空间`,
    );
    if (result) {
      await loadWorkspaces({ quiet: true });
    }
  };

  const deleteWorkspace = async () => {
    const workspace = dialog?.workspace;
    if (!workspace || busy) {
      return;
    }
    mutationControllerRef.current?.abort();
    const controller = new AbortController();
    mutationControllerRef.current = controller;
    setBusy(`delete:${workspace.id}`);
    setError('');
    setNotice(null);
    try {
      const response = await request(
        `/agentic/workspaces/${encodeURIComponent(workspace.id)}`,
        {
          method: 'DELETE',
          signal: controller.signal,
        },
      );
      const payload = await responsePayload(response);
      const result = classifyWorkspaceDeleteResult(response.status, payload);
      if (result.kind !== 'deleted') {
        setError(result.message);
        return;
      }
      setDialog(null);
      setNotice({ tone: 'success', message: `${workspace.name} 已删除` });
      const refreshed = await loadWorkspaces({ quiet: true });
      const next = refreshed || {
        workspaces: workspaces.filter((item) => item.id !== workspace.id),
        defaultWorkspaceId:
          defaultWorkspaceId === workspace.id ? '' : defaultWorkspaceId,
      };
      if (workspace.id === currentWorkspaceId) {
        const fallbackId = chooseWorkspaceFallback({
          deletedWorkspaceId: workspace.id,
          currentWorkspaceId,
          defaultWorkspaceId: next.defaultWorkspaceId,
          workspaces: next.workspaces,
        });
        await onWorkspaceChange?.(fallbackId);
      }
    } catch (deleteError) {
      if (deleteError?.name !== 'AbortError') {
        setError(deleteError?.message || '删除工作空间失败');
      }
    } finally {
      if (!controller.signal.aborted) {
        setBusy('');
      }
    }
  };

  return (
    <section
      className="settings-admin-panel"
      aria-labelledby="workspace-admin-title"
      data-wiz="cfg-wsmgmt"
    >
      <div className="settings-admin-heading">
        <div>
          <h3 id="workspace-admin-title">工作空间</h3>
          <p>浏览完整授权列表；搜索与分页在本地完成。</p>
        </div>
        <div className="settings-admin-actions">
          <button
            className="btn"
            type="button"
            disabled={loading || Boolean(busy)}
            onClick={() => loadWorkspaces()}
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
          {canCreate && (
            <button
              className="btn btn-p"
              type="button"
              disabled={Boolean(busy)}
              onClick={openCreate}
            >
              新建工作空间
            </button>
          )}
        </div>
      </div>

      <label className="settings-admin-search">
        <span className="sr-only">搜索工作空间</span>
        <input
          type="search"
          placeholder="搜索名称、ID、描述、所有者或成员"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setPage(1);
          }}
        />
      </label>

      {error && !dialog && (
        <div className="settings-admin-alert is-error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className={`settings-admin-alert is-${notice.tone}`} role="status">
          {notice.message}
        </div>
      )}

      {loading ? (
        <div className="settings-admin-state" role="status">
          正在加载工作空间…
        </div>
      ) : (
        <div className="settings-admin-table-wrap">
          <table className="dt settings-admin-table settings-workspace-table">
            <colgroup>
              <col className="settings-admin-col-identity" />
              <col className="settings-admin-col-owner" />
              <col className="settings-admin-col-members" />
              <col className="settings-admin-col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th>工作空间</th>
                <th>所有者</th>
                <th>成员</th>
                <th>
                  <span className="sr-only">操作</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {paged.items.map((workspace) => {
                const isDefault =
                  workspace.id === defaultWorkspaceId || workspace.is_default;
                const isCurrent = workspace.id === currentWorkspaceId;
                const canDelete = canDeleteWorkspace(currentUser, workspace);
                return (
                  <tr key={workspace.id}>
                    <td>
                      <div className="settings-admin-identity-line">
                        <strong>{workspace.name}</strong>
                        <div className="settings-admin-tags">
                          {isCurrent && <span className="tag tag-ok">当前</span>}
                          {isDefault && (
                            <span className="tag tag-lang">默认</span>
                          )}
                        </div>
                      </div>
                      <small>
                        {workspace.id}
                        {workspace.description ?
                          ` · ${workspace.description}` :
                          ''}
                      </small>
                    </td>
                    <td>{workspace.owner || '—'}</td>
                    <td>{workspace.members.length}</td>
                    <td>
                      <div className="settings-admin-row-actions">
                        {!isCurrent && (
                          <button
                            className="btn btn-sm"
                            type="button"
                            disabled={Boolean(busy)}
                            onClick={() => onWorkspaceChange?.(workspace.id)}
                          >
                            切换
                          </button>
                        )}
                        {canEditWorkspace(currentUser, workspace) && (
                          <button
                            className="btn btn-sm"
                            type="button"
                            disabled={Boolean(busy)}
                            onClick={() => openEdit(workspace)}
                          >
                            编辑
                          </button>
                        )}
                        {canManageCollection && !isDefault && (
                          <button
                            className="btn btn-sm"
                            type="button"
                            disabled={Boolean(busy)}
                            onClick={() => setDefault(workspace)}
                          >
                            设为默认
                          </button>
                        )}
                        {canDelete && (
                          <button
                            className="btn btn-sm btn-danger"
                            type="button"
                            disabled={
                              Boolean(busy) ||
                              (isDefault && canManageCollection)
                            }
                            onClick={() => {
                              setDialog({ type: 'delete', workspace });
                              setError('');
                            }}
                          >
                            删除
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
              {!paged.items.length && (
                <tr>
                  <td colSpan={4}>
                    <div className="settings-admin-state">
                      {query ? '没有匹配的工作空间' : '暂无可访问的工作空间'}
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {!loading && paged.total > 0 && (
        <nav className="settings-admin-pagination" aria-label="工作空间分页">
          <span>共 {paged.total} 项</span>
          <button
            className="btn btn-sm"
            type="button"
            disabled={paged.page <= 1}
            onClick={() => setPage(paged.page - 1)}
          >
            上一页
          </button>
          <span>
            {paged.page} / {paged.pageCount}
          </span>
          <button
            className="btn btn-sm"
            type="button"
            disabled={paged.page >= paged.pageCount}
            onClick={() => setPage(paged.page + 1)}
          >
            下一页
          </button>
        </nav>
      )}

      {(dialog?.type === 'create' || dialog?.type === 'edit') && (
        <WorkspaceDialog
          dialog={dialog}
          draft={draft}
          setDraft={setDraft}
          busy={Boolean(busy)}
          canSetDefault={canManageCollection}
          error={error}
          onClose={closeDialog}
          onSubmit={saveWorkspace}
        />
      )}
      {dialog?.type === 'delete' && (
        <Modal
          title={`删除 ${dialog.workspace.name}`}
          description="该操作会清理工作空间数据，不能撤销。删除当前工作空间后会自动切换到默认或首个可用工作空间。"
          onClose={busy ? undefined : closeDialog}
          actions={
            <>
              <button
                className="btn"
                type="button"
                disabled={Boolean(busy)}
                onClick={closeDialog}
              >
                取消
              </button>
              <button
                className="btn btn-danger"
                type="button"
                disabled={Boolean(busy)}
                onClick={deleteWorkspace}
              >
                {busy ? '删除中…' : '确认删除'}
              </button>
            </>
          }
        >
          {error && (
            <div className="settings-admin-alert is-error" role="alert">
              {error}
            </div>
          )}
          <p className="settings-admin-danger-copy">
            请输入确认不在本流程中保存；系统只会发送工作空间 ID。
          </p>
        </Modal>
      )}
    </section>
  );
}
