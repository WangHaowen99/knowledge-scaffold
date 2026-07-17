import { useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Database, Edit3, Loader2, Plus, RefreshCw, Star, Trash2, X } from 'lucide-react';

import { authFetch } from '../shared/api/auth';
import DataConnectionManagement from './DataConnectionManagement';

const RESERVED_WORKSPACE_IDS = new Set(['default']);

function formatDate(isoString) {
  if (!isoString) return '-';
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    const hours = String(d.getHours()).padStart(2, '0');
    const minutes = String(d.getMinutes()).padStart(2, '0');
    const seconds = String(d.getSeconds()).padStart(2, '0');
    
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
  } catch {
    return isoString;
  }
}

function WorkspaceManagement({
  workspaces,
  currentWorkspaceId,
  onWorkspacesRefresh,
  onOntologiesRefresh,
  onWorkspaceChange,
  onBack,
  currentUser,
  embedded = false,
  tabs = ['connections', 'bindings'],
  showDataConnections = true,
}) {
  const [activeTab, setActiveTab] = useState('connections');
  const [showCreateWorkspaceModal, setShowCreateWorkspaceModal] = useState(false);
  const [id, setId] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [setAsDefault, setSetAsDefault] = useState(false);
  const [editingWorkspaceId, setEditingWorkspaceId] = useState(null);
  const [editingName, setEditingName] = useState('');
  const [editingDescription, setEditingDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [createError, setCreateError] = useState('');
  const [success, setSuccess] = useState('');

  const workspaceCount = useMemo(() => workspaces.length, [workspaces]);

  useEffect(() => {
    if (!editingWorkspaceId) {
      setEditingName('');
      setEditingDescription('');
    }
  }, [editingWorkspaceId]);

  const handleCreate = async () => {
    const workspaceId = id.trim().toLowerCase();
    if (!workspaceId) {
      setCreateError('工作空间 ID 不能为空');
      return;
    }
    if (RESERVED_WORKSPACE_IDS.has(workspaceId)) {
      setCreateError('工作空间 ID “default” 是系统保留 ID，请使用其他 ID');
      return;
    }
    if (!name.trim()) {
      setCreateError('展示名称不能为空');
      return;
    }

    setBusy(true);
    setCreateError('');
    setSuccess('');
    try {
      const response = await authFetch('/agentic/workspaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: workspaceId,
          name: name.trim(),
          description: description.trim(),
          set_as_default: setAsDefault,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.detail || '创建工作空间失败');
      }

      await onWorkspacesRefresh(data.id);
      onWorkspaceChange(data.id);
      setName('');
      setId('');
      setDescription('');
      setSetAsDefault(false);
      setShowCreateWorkspaceModal(false);
      setActiveTab('connections');
      setSuccess(`已创建工作空间 ${data.name}`);
    } catch (err) {
      setCreateError(err?.message || '创建工作空间失败');
    } finally {
      setBusy(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    setError('');
    try {
      await onWorkspacesRefresh();
    } catch (err) {
      setError(err?.message || '刷新失败');
    } finally {
      setRefreshing(false);
    }
  };

  const beginEdit = (workspace) => {
    setEditingWorkspaceId(workspace.id);
    setEditingName(workspace.name || '');
    setEditingDescription(workspace.description || '');
    setError('');
    setSuccess('');
  };

  const cancelEdit = () => {
    setEditingWorkspaceId(null);
  };

  const handleSaveEdit = async (workspaceId) => {
    if (!editingName.trim()) {
      setError('工作空间名称不能为空');
      return;
    }

    setBusy(true);
    setError('');
    setSuccess('');
    try {
      const response = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: editingName.trim(),
          description: editingDescription.trim(),
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.detail || '更新工作空间失败');
      }

      await onWorkspacesRefresh(workspaceId);
      setEditingWorkspaceId(null);
      setSuccess(`已更新工作空间 ${data.name}`);
    } catch (err) {
      setError(err?.message || '更新工作空间失败');
    } finally {
      setBusy(false);
    }
  };

  const handleSetDefault = async (workspaceId) => {
    setBusy(true);
    setError('');
    setSuccess('');
    try {
      const response = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}/default`, {
        method: 'POST',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.detail || '设置默认工作空间失败');
      }

      await onWorkspacesRefresh(workspaceId);
      setSuccess(`已将 ${data?.workspace?.name || workspaceId} 设为默认工作空间`);
    } catch (err) {
      setError(err?.message || '设置默认工作空间失败');
    } finally {
      setBusy(false);
    }
  };

  const handleSelectWorkspace = async (workspace) => {
    if (!workspace?.id || workspace.id === currentWorkspaceId) return;

    setBusy(true);
    setError('');
    setSuccess('');
    try {
      await onWorkspaceChange?.(workspace.id);
      setSuccess(`已切换到工作空间 ${workspace.name || workspace.id}`);
    } catch (err) {
      setError(err?.message || '切换工作空间失败');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (workspaceId) => {
    if (!window.confirm('确认删除该工作空间吗？这会移除该空间下的会话、执行记录和场景资源。')) {
      return;
    }

    setBusy(true);
    setError('');
    setSuccess('');
    try {
      const response = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}`, {
        method: 'DELETE',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.detail || '删除工作空间失败');
      }

      await onWorkspacesRefresh();
      setSuccess(`已删除工作空间 ${workspaceId}`);
    } catch (err) {
      setError(err?.message || '删除工作空间失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`workspace-management-page ${embedded ? 'is-embedded' : ''}`}>
      {!embedded && (
        <div className="workspace-management-header">
          <div className="workspace-management-heading">
            <button
              className="workspace-management-back"
              onClick={onBack}
              type="button"
              aria-label="返回对话"
              title="返回对话"
            >
              <ArrowLeft size={16} />
            </button>
            <div>
              <h2>工作空间管理</h2>
              <p>管理当前场景下的数据连接与本体绑定。</p>
            </div>
          </div>
          <div className="workspace-management-actions">
            <button
              className="btn btn-secondary workspace-management-refresh"
              onClick={handleRefresh}
              disabled={refreshing}
              type="button"
            >
              {refreshing ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
              <span>刷新</span>
            </button>
            <button
              className="btn btn-secondary"
              type="button"
              onClick={() => {
                setCreateError('');
                setShowCreateWorkspaceModal(true);
              }}
            >
              <Plus size={14} />
              <span>新建工作空间</span>
            </button>
          </div>
        </div>
      )}
      {embedded && (
        <div className="configuration-section-title configuration-section-title-actions">
          <div>
            <h3>工作空间</h3>
            <p>{showDataConnections ? '管理当前空间信息和业务数据连接。' : '管理当前空间的名称、描述和默认空间状态。'}</p>
          </div>
          <div className="workspace-management-actions">
            <button
              className="btn btn-secondary workspace-management-refresh"
              onClick={handleRefresh}
              disabled={refreshing}
              type="button"
            >
              {refreshing ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
              <span>刷新</span>
            </button>
            <button
              className="btn btn-primary"
              type="button"
              onClick={() => {
                setCreateError('');
                setShowCreateWorkspaceModal(true);
              }}
            >
              <Plus size={14} />
              <span>新建工作空间</span>
            </button>
          </div>
        </div>
      )}

      {error && <div className="workspace-message error">{error}</div>}
      {success && <div className="workspace-message success">{success}</div>}

      <section className="workspace-summary-panel">
        {workspaceCount > 0 ? workspaces.map((workspace) => {
          const isEditing = editingWorkspaceId === workspace.id;
          const isDefault = Boolean(workspace.is_default);
          const isCurrent = workspace.id === currentWorkspaceId;
          return (
            <div className={`workspace-summary-strip ${isCurrent ? 'is-current' : ''}`} key={workspace.id}>
              <div className="workspace-summary-main">
                <div className="workspace-summary-head">
                  <div className="workspace-summary-title-row">
                    <h3>{workspace.name}</h3>
                    <span className="workspace-summary-id">{workspace.id}</span>
                    {isCurrent && <span className="workspace-current-badge">当前空间</span>}
                    {isDefault && <span className="workspace-default-badge">默认空间</span>}
                  </div>
                </div>

                {isEditing ? (
                  <div className="workspace-edit-form workspace-summary-edit-form">
                    <input
                      value={editingName}
                      onChange={(event) => setEditingName(event.target.value)}
                      placeholder="显示名称"
                    />
                    <textarea
                      value={editingDescription}
                      onChange={(event) => setEditingDescription(event.target.value)}
                      rows={3}
                      placeholder="描述"
                    />
                  </div>
                ) : (
                  <>
                    {workspace.description ? (
                      <div className="workspace-summary-description">
                        {workspace.description}
                      </div>
                    ) : null}
                    <div className="workspace-summary-meta">
                      <span>创建于 {formatDate(workspace.created_at)}</span>
                      <span>更新于 {formatDate(workspace.updated_at)}</span>
                    </div>
                  </>
                )}
              </div>

              <div className="workspace-summary-actions">
                {isEditing ? (
                  <>
                    <button className="btn btn-secondary btn-compact" onClick={cancelEdit} type="button" disabled={busy}>
                      取消
                    </button>
                    <button className="btn btn-primary btn-compact" onClick={() => handleSaveEdit(workspace.id)} type="button" disabled={busy}>
                      保存
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      className="btn btn-secondary btn-compact"
                      onClick={() => handleSelectWorkspace(workspace)}
                      disabled={busy || isCurrent}
                      type="button"
                    >
                      <span>切换</span>
                    </button>
                    <button
                      className="btn btn-secondary btn-compact"
                      onClick={() => handleSetDefault(workspace.id)}
                      disabled={busy || isDefault}
                      type="button"
                    >
                      <Star size={14} />
                      <span>设为默认</span>
                    </button>
                    <button className="btn btn-secondary btn-compact" onClick={() => beginEdit(workspace)} type="button" disabled={busy}>
                      <Edit3 size={14} />
                      <span>编辑</span>
                    </button>
                    <button
                      className="btn btn-secondary btn-compact workspace-delete-btn"
                      onClick={() => handleDelete(workspace.id)}
                      disabled={busy || isDefault}
                      type="button"
                    >
                      <Trash2 size={14} />
                      <span>删除</span>
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        }) : (
          <div className="workspace-summary-strip workspace-summary-empty">暂无可用工作空间，请新建工作空间</div>
        )}
      </section>

      {showDataConnections && tabs.length > 1 && (
      <div className="workspace-tabs">
        {[
          { id: 'connections', label: '数据连接' },
          { id: 'bindings', label: '本体绑定' },
        ].filter((tab) => tabs.includes(tab.id)).map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={`workspace-tab ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            <Database size={15} />
            <span>{tab.label}</span>
          </button>
        ))}
      </div>
      )}

      {showDataConnections ? (
        <DataConnectionManagement
          currentWorkspaceId={currentWorkspaceId}
          canEdit={currentUser?.role === 'admin'}
          view={activeTab === 'bindings' ? 'bindings' : 'connections'}
          onOntologiesChange={onOntologiesRefresh}
        />
      ) : null}

      {showCreateWorkspaceModal && (
        <div
          className="modal-overlay"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              setCreateError('');
              setShowCreateWorkspaceModal(false);
            }
          }}
        >
          <div className="modal-content workspace-create-modal" role="dialog" aria-modal="true" aria-labelledby="workspace-create-title">
            <div className="modal-header">
              <h3 id="workspace-create-title">新建工作空间</h3>
              <button
                className="modal-close"
                type="button"
                onClick={() => {
                  setCreateError('');
                  setShowCreateWorkspaceModal(false);
                }}
                aria-label="关闭"
              >
                <X size={18} />
              </button>
            </div>
            <div className="modal-body">
              {createError && <div className="workspace-modal-message error">{createError}</div>}
              <label className="workspace-field">
                <span>工作空间 ID <span className="data-required-mark">*</span></span>
                <input
                  value={id}
                  onChange={(event) => {
                    setCreateError('');
                    setId(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ''));
                  }}
                  placeholder="例如：example_project"
                />
              </label>
              <label className="workspace-field">
                <span>展示名称 <span className="data-required-mark">*</span></span>
                <input
                  value={name}
                  onChange={(event) => {
                    setCreateError('');
                    setName(event.target.value);
                  }}
                  placeholder="例如：示例场景工作空间"
                />
              </label>
              <label className="workspace-field">
                <span>描述</span>
                <textarea
                  value={description}
                  onChange={(event) => {
                    setCreateError('');
                    setDescription(event.target.value);
                  }}
                  placeholder="可选，描述该工作空间的用途"
                  rows={4}
                />
              </label>
              <label className="workspace-checkbox">
                <input
                  type="checkbox"
                  checked={setAsDefault}
                  onChange={(event) => setSetAsDefault(event.target.checked)}
                />
                <span>创建后设为默认工作空间</span>
              </label>
            </div>
            <div className="modal-footer">
              <button
                className="modal-btn-cancel"
                type="button"
                onClick={() => {
                  setCreateError('');
                  setShowCreateWorkspaceModal(false);
                }}
              >
                取消
              </button>
              <button className="modal-btn-confirm" type="button" onClick={handleCreate} disabled={busy}>
                {busy ? <Loader2 size={16} className="spin" /> : <Plus size={16} />}
                <span>创建</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default WorkspaceManagement;
