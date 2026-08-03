import React, { useEffect, useMemo, useState } from 'react';

import {
  listCandidateRevisions,
  previewCandidateRevision,
  respondCandidateRevision,
} from './authoring-api.js';
import ArtifactDiff from './ArtifactDiff.jsx';
import { safeUiError } from './revision-state.js';

const STATUS_LABEL = {
  pending: '待确认',
  accepted: '已采用',
  revision_requested: '已退回',
  rejected: '已拒绝',
  superseded: '已失效',
};

function normalizedGroups(groups = []) {
  return groups.map((group) => ({
    ...group,
    id: String(group.id ?? ''),
    dependsOn: Array.isArray(group.dependsOn) ? group.dependsOn.map(String) : [],
  })).filter((group) => group.id);
}

export function resolveCandidateGroupSelection(groups, requestedGroupIds) {
  const values = normalizedGroups(groups);
  const byId = new Map(values.map((group) => [group.id, group]));
  const requested = [...new Set((requestedGroupIds ?? []).map(String))]
    .filter((id) => byId.has(id));
  const selected = new Set();
  const include = (id) => {
    if (selected.has(id)) return;
    const group = byId.get(id);
    if (!group) return;
    group.dependsOn.forEach(include);
    selected.add(id);
  };
  requested.forEach(include);
  const selectedGroupIds = values.filter((group) => selected.has(group.id)).map((group) => group.id);
  return {
    requestedGroupIds: requested,
    selectedGroupIds,
    autoIncludedGroupIds: selectedGroupIds.filter((id) => !requested.includes(id)),
  };
}

export async function respondCandidateWithRefresh(operation, refresh) {
  try {
    const result = await operation();
    await refresh?.();
    return result;
  } catch (cause) {
    try { await refresh?.(); } catch { /* 保留原始候选处理错误 */ }
    throw cause;
  }
}

function validationCount(group, key) {
  const value = group.validation?.[key];
  if (Array.isArray(value)) return value.length;
  return Number.isFinite(Number(value)) ? Number(value) : 0;
}

function CandidateGroup({ group, requested, autoIncluded, disabled, onToggle }) {
  const errors = validationCount(group, 'errors');
  const warnings = validationCount(group, 'warnings');
  const changed = group.changedPaths?.length ?? group.changeCount ?? 0;
  return (
    <label className={`candidate-revision-group${autoIncluded ? ' auto-included' : ''}`}>
      <input type="checkbox" checked={requested || autoIncluded} disabled={disabled || autoIncluded}
        onChange={() => onToggle(group.id)} />
      <span className="candidate-revision-group-body">
        <span className="candidate-revision-group-title">
          <strong>{group.summary}</strong>
          <small>{changed} 个文件</small>
        </span>
        {group.dependsOn?.length > 0 && <span className="candidate-revision-deps">依赖：{group.dependsOn.join('、')}</span>}
        {autoIncluded && <span className="candidate-revision-auto">为保持引用完整，已自动包含</span>}
        {(errors > 0 || warnings > 0) && (
          <span className="candidate-revision-validation">
            {errors > 0 && <em className="error">错误 {errors}</em>}
            {warnings > 0 && <em className="warning">警告 {warnings}</em>}
          </span>
        )}
      </span>
    </label>
  );
}

function CandidateCard({
  projectId,
  candidate,
  busy,
  confirmAction,
  onBusy,
  onPreview,
  onChanged,
  onError,
}) {
  const groups = useMemo(() => normalizedGroups(candidate.groups), [candidate.groups]);
  const [requested, setRequested] = useState(() => groups.map((group) => group.id));
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  useEffect(() => { setRequested(groups.map((group) => group.id)); }, [candidate.id, groups]);
  const selection = useMemo(
    () => resolveCandidateGroupSelection(groups, requested),
    [groups, requested],
  );
  const requestedSet = new Set(selection.requestedGroupIds);
  const autoSet = new Set(selection.autoIncludedGroupIds);
  const pending = candidate.status === 'pending';

  const toggle = (id) => {
    setRequested((current) => current.includes(id)
      ? current.filter((value) => value !== id)
      : current.concat(id));
  };

  const run = async (kind, operation) => {
    if (busy) return;
    if (confirmAction && !confirmAction(kind)) return;
    onBusy(candidate.id); setError('');
    try { await operation(); }
    catch (cause) {
      const message = safeUiError(cause, '待确认的修改处理失败，请重试');
      setError(message);
      onError?.(message);
    }
    finally { onBusy(''); }
  };

  const preview = () => run('preview_candidate_revision', async () => {
    const result = await previewCandidateRevision(projectId, candidate.id, selection.requestedGroupIds);
    onPreview?.(result);
  });

  const respond = (action) => run(`${action}_candidate_revision`, async () => {
    if (action === 'revise' && !note.trim()) throw new Error('请填写需要调整的内容');
    await respondCandidateWithRefresh(
      () => respondCandidateRevision(projectId, candidate.id, {
        action,
        ...(action !== 'reject' ? { selectedGroupIds: selection.requestedGroupIds } : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      }),
      onChanged,
    );
    setNote('');
  });

  return (
    <article className={`candidate-revision-card ${candidate.status}`} data-candidate-review={candidate.id}>
      <header className="candidate-revision-card-head">
        <div>
          <span className="candidate-revision-eyebrow">候选修订</span>
          <strong>{candidate.summary}</strong>
          <small>{candidate.actor?.displayName || '知识包建模助手'} · {groups.length} 个原子变更组</small>
        </div>
        <span className="candidate-revision-status">{STATUS_LABEL[candidate.status] ?? candidate.status}</span>
      </header>
      <div className="candidate-revision-groups">
        {groups.map((group) => <CandidateGroup key={group.id} group={group}
          requested={requestedSet.has(group.id)} autoIncluded={autoSet.has(group.id)}
          disabled={!pending || Boolean(busy)} onToggle={toggle} />)}
      </div>
      {pending && <>
        <textarea rows={2} value={note} disabled={Boolean(busy)}
          placeholder="退回修改时请说明调整要求；采用或拒绝时可补充备注"
          onChange={(event) => setNote(event.target.value)} />
        <div className="candidate-revision-actions">
          <button type="button" className="btn" disabled={Boolean(busy) || !selection.selectedGroupIds.length}
            onClick={preview}>{busy === candidate.id ? '处理中…' : '查看差异'}</button>
          <button type="button" className="btn btn-p" data-action="accept_candidate_revision"
            disabled={Boolean(busy) || !selection.requestedGroupIds.length}
            onClick={() => respond('accept')}>接受所选</button>
          <button type="button" className="btn" disabled={Boolean(busy) || !note.trim()}
            onClick={() => respond('revise')}>退回修改</button>
          <button type="button" className="btn candidate-revision-reject" disabled={Boolean(busy)}
            onClick={() => respond('reject')}>拒绝</button>
        </div>
      </>}
      {candidate.status === 'accepted' && candidate.acceptedRevision && (
        <small>正式修订：{candidate.acceptedRevision}</small>
      )}
      {candidate.decision?.note && <small>处理说明：{candidate.decision.note}</small>}
      {error && <div className="candidate-revision-error" role="alert">{error}</div>}
    </article>
  );
}

export function CandidateRevisionCanvas({ preview, onOpenFile, onClose }) {
  if (!preview) return null;
  const candidate = preview.candidate ?? {};
  const selection = preview.selection ?? {};
  return (
    <section className="candidate-revision-canvas" aria-label="待确认的修改差异">
      <header>
        <div>
          <span>候选修订差异</span>
          <h3>{candidate.summary || '待确认修改'}</h3>
        </div>
        <div className="candidate-revision-selection-summary">
          已选 {selection.selectedGroupIds?.length ?? 0} 组
          {(selection.autoIncludedGroupIds?.length ?? 0) > 0 && ` · 自动包含 ${selection.autoIncludedGroupIds.length} 组依赖`}
        </div>
        {onClose && <button type="button" className="btn" onClick={onClose}>关闭差异</button>}
      </header>
      <ArtifactDiff diff={{
        fromRevision: candidate.baseRevision,
        toRevision: candidate.id,
        files: preview.files ?? [],
      }} onOpenFile={onOpenFile} />
    </section>
  );
}

export default function CandidateRevisionPanel({
  projectId,
  candidates: suppliedCandidates,
  refreshKey,
  onPreview,
  onChanged,
  onError,
  confirmAction,
  title = '待确认的修改',
  subtitle = '修改不会在您确认前进入当前知识包',
}) {
  const [loaded, setLoaded] = useState(suppliedCandidates ?? null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const candidates = suppliedCandidates ?? loaded ?? [];

  const load = async () => {
    if (!projectId || suppliedCandidates) return;
    try {
      const result = await listCandidateRevisions(projectId, 'pending');
      setLoaded(result.candidates ?? []); setError('');
    } catch (cause) { setError(safeUiError(cause, '待确认的修改读取失败，请重试')); }
  };
  useEffect(() => { void load(); }, [projectId, suppliedCandidates, refreshKey]);

  const changed = async () => {
    await load();
    await onChanged?.();
  };

  if (!candidates.length && !error) return null;
  return (
    <section className="candidate-revision-panel" aria-label={title}>
      <div className="candidate-revision-panel-title">
        <div><strong>{title}</strong><span>{subtitle}</span></div>
        <em>{candidates.length}</em>
      </div>
      {candidates.map((candidate) => <CandidateCard key={candidate.id}
        projectId={projectId} candidate={candidate} busy={busy} onBusy={setBusy}
        confirmAction={confirmAction}
        onPreview={onPreview} onChanged={changed}
        onError={(message) => { setError(message); onError?.(message); }} />)}
      {error && <div className="candidate-revision-error" role="alert">{error}</div>}
    </section>
  );
}
