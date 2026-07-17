import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  acceptsEvolutionResponse,
  buildEvolutionKnowledgeRefresh,
  buildLineWordDiff,
  buildSkillApprovalPayload,
  canCancelSkillTask,
  canDeleteSkillTask,
  canLoadSkillDiff,
  filterOperationExport,
  evolutionScopeKey,
  groupOperationRecords,
  groupSkillTasksByOntology,
  isSkillDiffReady,
  isActiveSkillTask,
  operationExportFilename,
  permitsEvolutionWrite,
  reconcileSkillTaskSelection,
  selectionBoundValue,
  SKILL_NEW_VERSION_ACTIVATION_COPY,
} from './model';
import './evolution.css';
import './workbench.css';
import { useAuthContext } from '../auth';
import { authFetch } from '../../shared/api/auth';
import { EmptyState, SectionCard, ScopeTabs, StatusTag, Table } from '../../shared/ui/primitives';

const STATUS_LABELS = {
  pending: '待积累',
  promoted: '已晋级',
  applied: '已应用',
  rejected: '已拒绝',
  submitted: '已提交',
  running: '运行中',
  downloading: '下载中',
  completed: '待审批',
  approved: '已批准',
  no_change: '无变更',
  failed: '失败',
  interrupted: '已中断',
  cancelled: '已取消',
};

const statusTone = (status) => {
  if (['submitted', 'running', 'downloading'].includes(status)) return 'active';
  if (status === 'completed' || status === 'promoted') return 'warning';
  return status;
};

const fmt = (value) => {
  if (!value) return '—';
  const parsed = typeof value === 'number' ? value * 1000 : value;
  const date = new Date(parsed);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
};

const pathFor = (ontologyId, operationKey) => (
  `${encodeURIComponent(ontologyId)}/${encodeURIComponent(operationKey)}`
);

async function responsePayload(response) {
  return response.json().catch(() => ({}));
}

function errorMessage(response, payload, fallback = '操作失败') {
  return `${fallback}：${payload?.error || `HTTP ${response.status}`}`;
}

function isAbort(error) {
  return error?.name === 'AbortError';
}

function downloadJson(payload, filename) {
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json;charset=utf-8' });
  const objectUrl = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(objectUrl);
}

function Notice({ notice }) {
  if (!notice?.text) return null;
  return (
    <div className={`evo-notice ${notice.kind === 'error' ? 'error' : ''}`} role={notice.kind === 'error' ? 'alert' : 'status'}>
      {notice.text}
    </div>
  );
}

export default function EvolutionWorkbench({
  ontologyId,
  onKnowledgeChanged,
  readOnly = false,
  viewVersion = '',
  activeVersion = '',
}) {
  const [kind, setKind] = useState('operation');
  return (
    <div className="kb-tab-body evolution-workbench">
      {readOnly && (
        <div className="ro-banner" role="status">
          历史版本 {viewVersion || '—'} 只读。进化数据对应当前工作区，写操作会影响激活版本
          {activeVersion ? ` ${activeVersion}` : ''}，因此已全部禁用；设为激活版本后可继续操作。
        </div>
      )}
      <ScopeTabs value={kind} onChange={setKind} ariaLabel="记忆进化类型" items={[
        { id: 'operation', label: 'Operation 进化' },
        { id: 'skill', label: 'Skill 进化' },
      ]} />
      {kind === 'operation'
        ? <OperationEvolution ontologyId={ontologyId} onKnowledgeChanged={onKnowledgeChanged} readOnly={readOnly} />
        : <SkillEvolution ontologyId={ontologyId} onKnowledgeChanged={onKnowledgeChanged} readOnly={readOnly} />}
    </div>
  );
}

function OperationEvolution({ ontologyId, onKnowledgeChanged, readOnly }) {
  const { isAdmin, workspaceId } = useAuthContext();
  const scopeKey = evolutionScopeKey(workspaceId, ontologyId);
  const scopeRef = useRef(scopeKey);
  const selectedKeyRef = useRef('');
  const indexRequestRef = useRef(null);
  const detailRequestRef = useRef(null);
  const [items, setItems] = useState([]);
  const [loadedScopeKey, setLoadedScopeKey] = useState('');
  const [selectedKey, setSelectedKey] = useState('');
  const [detailSelectionKey, setDetailSelectionKey] = useState('');
  const [proposals, setProposals] = useState([]);
  const [records, setRecords] = useState([]);
  const [staged, setStaged] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);
  const [editing, setEditing] = useState('');
  const [draft, setDraft] = useState('');

  scopeRef.current = scopeKey;
  selectedKeyRef.current = selectedKey;

  const scopeReady = loadedScopeKey === scopeKey;
  const visibleItems = useMemo(
    () => scopeReady ? items : [],
    [items, scopeReady],
  );

  const selected = useMemo(
    () => visibleItems.find((item) => `${item.ontology_id}/${item.operation_key}` === selectedKey) || null,
    [selectedKey, visibleItems],
  );
  const boundDetail = selectionBoundValue(selectedKey, detailSelectionKey, { proposals, records, staged });
  const detailMatchesSelection = Boolean(boundDetail);
  const canWriteEvolution = permitsEvolutionWrite({ readOnly });
  const canWriteSelectedDetail = permitsEvolutionWrite({
    readOnly,
    currentSelection: selectedKey,
    boundSelection: detailSelectionKey,
  });
  const visibleProposals = boundDetail?.proposals || [];
  const visibleStaged = boundDetail?.staged || null;
  const recordsByFingerprint = useMemo(
    () => groupOperationRecords(detailMatchesSelection ? records : []),
    [detailMatchesSelection, records],
  );

  const loadIndex = useCallback(async ({ clear = false } = {}) => {
    indexRequestRef.current?.abort();
    const controller = new AbortController();
    const requestScope = scopeKey;
    indexRequestRef.current = controller;
    setLoading(true);
    setLoadError('');
    if (clear) {
      setItems([]);
      setSelectedKey('');
      setDetailSelectionKey('');
      setProposals([]);
      setRecords([]);
      setStaged(null);
      setNotice(null);
      setBusy('');
      setEditing('');
      setDraft('');
    }
    try {
      const response = await authFetch('/agentic/operations/proposals?status=all', { signal: controller.signal });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload, '加载 Operation 建议失败'));
      if (controller.signal.aborted || !acceptsEvolutionResponse({ currentScope: scopeRef.current, requestScope })) return;
      const nextItems = (payload.items || []).filter(
        (item) => !ontologyId || item.ontology_id === ontologyId,
      );
      setLoadedScopeKey(requestScope);
      setItems(nextItems);
      setSelectedKey((current) => (
        current && nextItems.some((item) => `${item.ontology_id}/${item.operation_key}` === current)
          ? current
          : nextItems[0] ? `${nextItems[0].ontology_id}/${nextItems[0].operation_key}` : ''
      ));
    } catch (error) {
      if (!isAbort(error) && acceptsEvolutionResponse({ currentScope: scopeRef.current, requestScope })) {
        setLoadedScopeKey(requestScope);
        setLoadError(error.message || '加载失败');
      }
    } finally {
      if (!controller.signal.aborted && acceptsEvolutionResponse({ currentScope: scopeRef.current, requestScope })) setLoading(false);
    }
  }, [ontologyId, scopeKey]);

  const loadDetail = useCallback(async ({ quiet = false } = {}) => {
    detailRequestRef.current?.abort();
    if (!selected) {
      setDetailSelectionKey('');
      setProposals([]);
      setRecords([]);
      setStaged(null);
      setDetailLoading(false);
      return;
    }
    const controller = new AbortController();
    const requestScope = scopeKey;
    const requestKey = `${selected.ontology_id}/${selected.operation_key}`;
    detailRequestRef.current = controller;
    setDetailSelectionKey('');
    if (!quiet) {
      setProposals([]);
      setRecords([]);
      setStaged(null);
      setEditing('');
      setDraft('');
    }
    setDetailLoading(true);
    setDetailError('');
    const base = `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}`;
    try {
      const [proposalResponse, stagedResponse] = await Promise.all([
        authFetch(`${base}/proposals?status=all&include_records=1`, { signal: controller.signal }),
        authFetch(`${base}/staged`, { signal: controller.signal }),
      ]);
      const [proposalPayload, stagedPayload] = await Promise.all([
        responsePayload(proposalResponse),
        responsePayload(stagedResponse),
      ]);
      if (!proposalResponse.ok) {
        throw new Error(errorMessage(proposalResponse, proposalPayload, '加载建议详情失败'));
      }
      if (!stagedResponse.ok && stagedResponse.status !== 404) {
        throw new Error(errorMessage(stagedResponse, stagedPayload, '加载暂存补丁失败'));
      }
      if (
        controller.signal.aborted
        || !acceptsEvolutionResponse({
          currentScope: scopeRef.current,
          requestScope,
          currentSelection: selectedKeyRef.current,
          requestSelection: requestKey,
        })
      ) return;
      setProposals(proposalPayload.proposals || []);
      setRecords(proposalPayload.proposal_records || []);
      setStaged(stagedResponse.ok ? stagedPayload : null);
      setDetailSelectionKey(requestKey);
    } catch (error) {
      if (!isAbort(error) && acceptsEvolutionResponse({
        currentScope: scopeRef.current,
        requestScope,
        currentSelection: selectedKeyRef.current,
        requestSelection: requestKey,
      })) {
        setDetailSelectionKey('');
        setDetailError(error.message || '加载失败');
      }
    } finally {
      if (!controller.signal.aborted && acceptsEvolutionResponse({
        currentScope: scopeRef.current,
        requestScope,
        currentSelection: selectedKeyRef.current,
        requestSelection: requestKey,
      })) setDetailLoading(false);
    }
  }, [scopeKey, selected]);

  useEffect(() => {
    const timer = setTimeout(() => loadIndex({ clear: true }), 0);
    return () => {
      clearTimeout(timer);
      indexRequestRef.current?.abort();
      detailRequestRef.current?.abort();
    };
  }, [loadIndex]);

  useEffect(() => {
    const timer = setTimeout(() => loadDetail(), 0);
    return () => {
      clearTimeout(timer);
      detailRequestRef.current?.abort();
    };
  }, [loadDetail]);

  useEffect(() => {
    if (!readOnly) return;
    setEditing('');
    setDraft('');
  }, [readOnly]);

  const proposalAction = async (fingerprint, action) => {
    if (!canWriteSelectedDetail || !selected || !detailMatchesSelection
      || !visibleProposals.some((proposal) => proposal.fingerprint === fingerprint)) return;
    const token = `${action}:${fingerprint}`;
    const requestScope = scopeKey;
    const requestSelection = `${selected.ontology_id}/${selected.operation_key}`;
    const accepted = () => acceptsEvolutionResponse({
      currentScope: scopeRef.current,
      requestScope,
      currentSelection: selectedKeyRef.current,
      requestSelection,
    });
    setBusy(token);
    setNotice(null);
    try {
      const response = await authFetch(
        `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}/proposals/${encodeURIComponent(fingerprint)}/${action}`,
        { method: 'POST' },
      );
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload));
      if (!accepted()) return;
      setNotice({
        kind: 'success',
        text: action === 'endorse' ? '已增加认可证据。' : '建议已拒绝，不再进入当前补丁积累。',
      });
      await Promise.all([loadDetail({ quiet: true }), loadIndex()]);
    } catch (error) {
      if (accepted()) setNotice({ kind: 'error', text: error.message || '操作失败' });
    } finally {
      if (accepted()) setBusy('');
    }
  };

  const saveEdit = async (fingerprint) => {
    if (!canWriteSelectedDetail || !selected || !detailMatchesSelection
      || !visibleProposals.some((proposal) => proposal.fingerprint === fingerprint)) return;
    const requestScope = scopeKey;
    const requestSelection = `${selected.ontology_id}/${selected.operation_key}`;
    const accepted = () => acceptsEvolutionResponse({
      currentScope: scopeRef.current,
      requestScope,
      currentSelection: selectedKeyRef.current,
      requestSelection,
    });
    const content = draft.trim();
    if (content.length < 4) {
      setNotice({ kind: 'error', text: '建议内容至少需要 4 个字符。' });
      return;
    }
    setBusy(`edit:${fingerprint}`);
    setNotice(null);
    try {
      const response = await authFetch(
        `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}/proposals/${encodeURIComponent(fingerprint)}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        },
      );
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload, '保存建议失败'));
      if (!accepted()) return;
      setEditing('');
      setDraft('');
      setNotice({ kind: 'success', text: '建议内容已保存。' });
      await loadDetail({ quiet: true });
    } catch (error) {
      if (accepted()) setNotice({ kind: 'error', text: error.message || '保存失败' });
    } finally {
      if (accepted()) setBusy('');
    }
  };

  const rescan = async () => {
    if (!canWriteEvolution || !selected) return;
    const requestScope = scopeKey;
    const requestSelection = `${selected.ontology_id}/${selected.operation_key}`;
    const accepted = () => acceptsEvolutionResponse({
      currentScope: scopeRef.current,
      requestScope,
      currentSelection: selectedKeyRef.current,
      requestSelection,
    });
    setBusy('rescan');
    setNotice(null);
    try {
      const response = await authFetch(
        `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}/rescan`,
        { method: 'POST' },
      );
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload, '重新扫描失败'));
      if (!accepted()) return;
      setNotice({ kind: 'success', text: '重新扫描已进入后台队列，可稍后刷新查看补丁结果。' });
    } catch (error) {
      if (accepted()) setNotice({ kind: 'error', text: error.message || '重新扫描失败' });
    } finally {
      if (accepted()) setBusy('');
    }
  };

  const exportProposals = async () => {
    const requestScope = scopeKey;
    const accepted = () => acceptsEvolutionResponse({ currentScope: scopeRef.current, requestScope });
    setBusy('export');
    setNotice(null);
    try {
      const response = await authFetch('/agentic/operations/proposals/export?status=all&records_limit=10000');
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload, '导出失败'));
      if (!accepted()) return;
      const scoped = filterOperationExport(payload, ontologyId);
      if (!scoped.operations.length) throw new Error('当前范围没有可导出的建议与证据');
      downloadJson(scoped, operationExportFilename(ontologyId || payload.workspace_id));
      setNotice({
        kind: 'success',
        text: `已导出 ${scoped.operation_count} 个 Operation、${scoped.aggregate_count} 条建议和 ${scoped.record_count} 条原始证据。`,
      });
    } catch (error) {
      if (accepted()) setNotice({ kind: 'error', text: error.message || '导出失败' });
    } finally {
      if (accepted()) setBusy('');
    }
  };

  const stagedAction = async (action, rejectProposals = true) => {
    if (!canWriteSelectedDetail || !selected || !isAdmin || !detailMatchesSelection || !visibleStaged) return;
    const token = `staged:${action}:${rejectProposals}`;
    const requestScope = scopeKey;
    const requestSelection = `${selected.ontology_id}/${selected.operation_key}`;
    const accepted = () => acceptsEvolutionResponse({
      currentScope: scopeRef.current,
      requestScope,
      currentSelection: selectedKeyRef.current,
      requestSelection,
    });
    setBusy(token);
    setNotice(null);
    try {
      const response = await authFetch(
        `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}/staged/${action}`,
        {
          method: 'POST',
          headers: action === 'reject' ? { 'Content-Type': 'application/json' } : undefined,
          body: action === 'reject' ? JSON.stringify({ reject_proposals: rejectProposals }) : undefined,
        },
      );
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload));
      if (!accepted()) return;
      setNotice({
        kind: 'success',
        text: action === 'apply'
          ? '暂存补丁已应用，原 Operation 文件已归档。'
          : rejectProposals
            ? '暂存补丁已丢弃，源建议已标记为拒绝。'
            : '暂存补丁已丢弃，源建议已恢复为待积累。',
      });
      if (action === 'apply') {
        await onKnowledgeChanged?.(buildEvolutionKnowledgeRefresh('operation-apply', selected.ontology_id));
        if (!accepted()) return;
      }
      await Promise.all([loadDetail(), loadIndex()]);
    } catch (error) {
      if (accepted()) setNotice({ kind: 'error', text: error.message || '操作失败' });
    } finally {
      if (accepted()) setBusy('');
    }
  };

  return (
    <div className="evolution-grid">
      <SectionCard title="Operation 建议" description="按业务操作聚合主动反馈与运行证据。">
        <div className="evo-toolbar">
          <button className="btn" type="button" disabled={loading} onClick={() => loadIndex()}>
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button className="btn" type="button" disabled={Boolean(busy)} onClick={exportProposals}>
            {busy === 'export' ? '导出中…' : '导出建议与证据'}
          </button>
        </div>
        {scopeReady && loadError && <div className="evo-notice error" role="alert">{loadError}</div>}
        {(!scopeReady || loading) && !visibleItems.length && <div className="empty">加载中…</div>}
        {scopeReady && !loading && !visibleItems.length && !loadError && (
          <EmptyState compact title="暂无进化建议" description="Agent 运行后，建议会按 Operation 汇总到这里。" />
        )}
        <div className="evolution-list">
          {visibleItems.map((item) => {
            const key = `${item.ontology_id}/${item.operation_key}`;
            return (
              <button type="button" key={key} className={key === selectedKey ? 'on' : ''} onClick={() => {
                setSelectedKey(key);
                setDetailSelectionKey('');
                setProposals([]);
                setRecords([]);
                setStaged(null);
                setDetailError('');
                setEditing('');
                setDraft('');
                setBusy('');
                setNotice(null);
              }}>
                <strong>{item.operation_key}</strong>
                <span>{item.aggregate_count} 条建议 · {item.occurrences} 次证据</span>
                <small>{item.top_content}</small>
              </button>
            );
          })}
        </div>
        <Notice notice={scopeReady ? notice : null} />
      </SectionCard>
      <div className="evolution-detail">
        {!selected && <SectionCard><EmptyState title="选择 Operation 查看建议" /></SectionCard>}
        {selected && (
          <>
            <SectionCard
              title={selected.operation_key}
              description={`${selected.ontology_id} · 最近更新 ${fmt(selected.last_seen)}`}
              actions={(
                <div className="evo-actions">
                  <StatusTag status={selected.has_staged || visibleStaged ? 'warning' : 'active'}>
                    {selected.has_staged || visibleStaged ? '待审批补丁' : '建议积累中'}
                  </StatusTag>
                  <button className="btn" type="button" disabled={Boolean(busy) || readOnly} onClick={rescan}
                    title={readOnly ? '历史版本下不会修改激活版本' : ''}>
                    {busy === 'rescan' ? '提交中…' : '重新扫描'}
                  </button>
                </div>
              )}
            >
              {(detailLoading || !detailMatchesSelection) && !visibleProposals.length && <div className="empty">加载详情中…</div>}
              {detailError && <div className="evo-notice error" role="alert">{detailError}</div>}
              {!detailLoading && detailMatchesSelection && !visibleProposals.length && !detailError && <EmptyState compact title="当前筛选下暂无建议" />}
              <div className="proposal-cards">
                {visibleProposals.map((proposal) => {
                  const evidence = recordsByFingerprint.get(proposal.fingerprint) || [];
                  const editable = !['applied', 'rejected', 'promoted'].includes(proposal.status);
                  const isEditing = editing === proposal.fingerprint;
                  return (
                    <article className="evo-proposal" key={proposal.fingerprint}>
                      <header>
                        <StatusTag status={statusTone(proposal.status)}>{STATUS_LABELS[proposal.status] || proposal.status}</StatusTag>
                        <span>{proposal.kind}</span>
                        <span className="evo-proposal-stats">
                          {proposal.occurrences || 0} 次出现 · {proposal.active_endorsements || 0} 次认可 · {evidence.length} 条证据
                        </span>
                      </header>
                      {isEditing ? (
                        <>
                          <textarea value={draft} onChange={(event) => setDraft(event.target.value)} aria-label="编辑建议内容" />
                          <div className="evo-actions">
                            <button className="btn btn-p" type="button" disabled={Boolean(busy) || readOnly} onClick={() => saveEdit(proposal.fingerprint)}>保存</button>
                            <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => { setEditing(''); setDraft(''); }}>取消</button>
                          </div>
                        </>
                      ) : <p>{proposal.canonical_content}</p>}
                      <footer>
                        <span>{proposal.entity_key ? `实体 ${proposal.entity_key}` : `指纹 ${proposal.fingerprint.slice(0, 10)}`}</span>
                        {editable && !isEditing && (
                          <>
                            <button className="btn" type="button" disabled={Boolean(busy) || readOnly} onClick={() => proposalAction(proposal.fingerprint, 'endorse')}>认可</button>
                            <button className="btn" type="button" disabled={Boolean(busy) || readOnly} onClick={() => { setEditing(proposal.fingerprint); setDraft(proposal.canonical_content || ''); }}>编辑</button>
                            <button className="btn btn-danger" type="button" disabled={Boolean(busy) || readOnly} onClick={() => proposalAction(proposal.fingerprint, 'reject')}>拒绝建议</button>
                          </>
                        )}
                      </footer>
                      {evidence.length > 0 && (
                        <details className="evo-evidence">
                          <summary>查看 {evidence.length} 条原始证据</summary>
                          {evidence.map((record, index) => (
                            <article key={record.id || `${record.fingerprint}-${index}`}>
                              <small>{record.source || '未知来源'} · {record.run_id || '无 run id'} · {fmt(record.ts)}</small>
                              <p>{record.content}</p>
                              {record.rationale && <small>依据：{record.rationale}</small>}
                            </article>
                          ))}
                        </details>
                      )}
                    </article>
                  );
                })}
              </div>
            </SectionCard>
            {visibleStaged && (
              <SectionCard
                title="暂存变更对比"
                description={visibleStaged.meta?.change_summary || '审批前核对 Operation 原文与候选内容。'}
              >
                {!isAdmin && <div className="ro-banner">🔒 只有平台管理员可以应用或拒绝暂存补丁。</div>}
                {isAdmin && (
                  <div className="evo-staged-actions">
                    <button className="btn btn-p" type="button" disabled={Boolean(busy) || readOnly} onClick={() => stagedAction('apply')}>应用补丁</button>
                    <button className="btn btn-danger" type="button" disabled={Boolean(busy) || readOnly} onClick={() => stagedAction('reject', true)}>丢弃并拒绝源建议</button>
                    <button className="btn" type="button" disabled={Boolean(busy) || readOnly} onClick={() => stagedAction('reject', false)}>仅丢弃补丁，保留待积累</button>
                  </div>
                )}
                <FileDiff
                  file={{ path: `${selected.operation_key}.md`, change_type: 'modified', original: visibleStaged.original, evolved: visibleStaged.staged }}
                  defaultOpen
                />
              </SectionCard>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SkillEvolution({ ontologyId, onKnowledgeChanged, readOnly }) {
  const { isAdmin, user, workspaceId } = useAuthContext();
  const scopeKey = evolutionScopeKey(workspaceId, ontologyId);
  const scopeRef = useRef(scopeKey);
  const selectedRef = useRef('');
  const tasksRequestRef = useRef(null);
  const diffRequestRef = useRef(null);
  const [tasks, setTasks] = useState([]);
  const [loadedScopeKey, setLoadedScopeKey] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [diffTaskId, setDiffTaskId] = useState('');
  const [diff, setDiff] = useState(null);
  const [loading, setLoading] = useState(true);
  const [diffLoading, setDiffLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [diffError, setDiffError] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);

  scopeRef.current = scopeKey;
  selectedRef.current = selectedId;

  const scopeReady = loadedScopeKey === scopeKey;
  const visibleTasks = useMemo(
    () => scopeReady ? tasks : [],
    [scopeReady, tasks],
  );

  const selected = useMemo(
    () => visibleTasks.find((task) => task.task_id === selectedId) || null,
    [selectedId, visibleTasks],
  );
  const groups = useMemo(() => groupSkillTasksByOntology(visibleTasks), [visibleTasks]);
  const hasActive = visibleTasks.some(isActiveSkillTask);
  const selectedTaskId = selected?.task_id || '';
  const selectedStatus = selected?.status || '';
  const selectedCanLoadDiff = canLoadSkillDiff(selected);
  const canWriteEvolution = permitsEvolutionWrite({ readOnly });
  const visibleDiff = selectionBoundValue(selectedTaskId, diffTaskId, diff);
  const diffReady = isSkillDiffReady({
    selectedTaskId,
    diffTaskId,
    diff,
    loading: diffLoading,
    error: diffError,
  });

  const loadTasks = useCallback(async ({ clear = false, background = false } = {}) => {
    tasksRequestRef.current?.abort();
    const controller = new AbortController();
    const requestScope = scopeKey;
    tasksRequestRef.current = controller;
    if (!background) setLoading(true);
    setLoadError('');
    if (clear) {
      setTasks([]);
      setSelectedId('');
      setDiffTaskId('');
      setDiff(null);
      setDiffError('');
      setNotice(null);
      setBusy('');
    }
    try {
      const query = ontologyId ? `?ontology_id=${encodeURIComponent(ontologyId)}` : '';
      const response = await authFetch(`/agentic/skill-evolution/tasks${query}`, { signal: controller.signal });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload, '加载 Skill 进化任务失败'));
      if (controller.signal.aborted || !acceptsEvolutionResponse({ currentScope: scopeRef.current, requestScope })) return;
      const nextTasks = payload.tasks || [];
      setLoadedScopeKey(requestScope);
      setTasks(nextTasks);
      setSelectedId((current) => reconcileSkillTaskSelection(nextTasks, current));
    } catch (error) {
      if (!isAbort(error) && acceptsEvolutionResponse({ currentScope: scopeRef.current, requestScope })) {
        setLoadedScopeKey(requestScope);
        setLoadError(error.message || '加载失败');
      }
    } finally {
      if (!controller.signal.aborted && acceptsEvolutionResponse({ currentScope: scopeRef.current, requestScope }) && !background) setLoading(false);
    }
  }, [ontologyId, scopeKey]);

  useEffect(() => {
    const timer = setTimeout(() => loadTasks({ clear: true }), 0);
    return () => {
      clearTimeout(timer);
      tasksRequestRef.current?.abort();
      diffRequestRef.current?.abort();
    };
  }, [loadTasks]);

  useEffect(() => {
    if (!hasActive) return undefined;
    const interval = window.setInterval(() => loadTasks({ background: true }), 5000);
    return () => window.clearInterval(interval);
  }, [hasActive, loadTasks]);

  useEffect(() => {
    diffRequestRef.current?.abort();
    const taskId = selectedTaskId;
    const requestScope = scopeKey;
    const timer = setTimeout(async () => {
      setDiffTaskId('');
      setDiff(null);
      setDiffError('');
      if (!taskId || !selectedCanLoadDiff) {
        setDiffLoading(false);
        return;
      }
      const controller = new AbortController();
      diffRequestRef.current = controller;
      setDiffLoading(true);
      try {
        const response = await authFetch(
          `/agentic/skill-evolution/tasks/${encodeURIComponent(taskId)}/diff`,
          { signal: controller.signal },
        );
        const payload = await responsePayload(response);
        if (!response.ok) throw new Error(errorMessage(response, payload, '加载文件差异失败'));
        if (
          controller.signal.aborted
          || !acceptsEvolutionResponse({
            currentScope: scopeRef.current,
            requestScope,
            currentSelection: selectedRef.current,
            requestSelection: taskId,
          })
        ) return;
        setDiff(payload);
        setDiffTaskId(taskId);
      } catch (error) {
        if (!isAbort(error) && acceptsEvolutionResponse({
          currentScope: scopeRef.current,
          requestScope,
          currentSelection: selectedRef.current,
          requestSelection: taskId,
        })) {
          setDiffTaskId('');
          setDiffError(error.message || '加载文件差异失败');
        }
      } finally {
        if (!controller.signal.aborted && acceptsEvolutionResponse({
          currentScope: scopeRef.current,
          requestScope,
          currentSelection: selectedRef.current,
          requestSelection: taskId,
        })) {
          setDiffLoading(false);
        }
      }
    }, 0);
    return () => {
      clearTimeout(timer);
      diffRequestRef.current?.abort();
    };
  }, [scopeKey, selectedCanLoadDiff, selectedStatus, selectedTaskId]);

  const act = async (action, body) => {
    if (!canWriteEvolution || !selected) return;
    if (action === 'approve' && (!diffReady || visibleDiff?.drift?.drifted)) return;
    const taskId = selected.task_id;
    const requestScope = scopeKey;
    const accepted = () => acceptsEvolutionResponse({
      currentScope: scopeRef.current,
      requestScope,
      currentSelection: selectedRef.current,
      requestSelection: taskId,
    });
    setBusy(action);
    setNotice(null);
    try {
      const response = await authFetch(
        `/agentic/skill-evolution/tasks/${encodeURIComponent(taskId)}/${action}`,
        {
          method: 'POST',
          headers: body ? { 'Content-Type': 'application/json' } : undefined,
          body: body ? JSON.stringify(body) : undefined,
        },
      );
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload));
      if (!accepted()) return;
      setTasks((current) => current.map((task) => task.task_id === taskId ? payload : task));
      const labels = {
        approve: body?.mode === 'overwrite' ? '任务已批准并覆盖激活版本。' : `任务已批准并创建新版本${payload.new_version ? ` ${payload.new_version}` : ''}。`,
        reject: '任务已拒绝，原本体保持不变。',
        cancel: '任务已取消。',
      };
      setNotice({ kind: 'success', text: labels[action] || '操作成功。' });
      if (action === 'approve') {
        await onKnowledgeChanged?.(buildEvolutionKnowledgeRefresh('skill-approve', selected.ontology_id));
        if (!accepted()) return;
      }
      await loadTasks({ background: true });
    } catch (error) {
      if (accepted()) setNotice({ kind: 'error', text: error.message || '操作失败' });
    } finally {
      if (accepted()) setBusy('');
    }
  };

  const deleteTask = async () => {
    if (!canWriteEvolution || !selected || !isAdmin || !canDeleteSkillTask(selected)) return;
    const taskId = selected.task_id;
    const requestScope = scopeKey;
    const accepted = () => acceptsEvolutionResponse({
      currentScope: scopeRef.current,
      requestScope,
      currentSelection: selectedRef.current,
      requestSelection: taskId,
    });
    setBusy('delete');
    setNotice(null);
    try {
      const response = await authFetch(`/agentic/skill-evolution/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(errorMessage(response, payload, '删除任务失败'));
      if (!accepted()) return;
      setBusy('');
      setTasks((current) => current.filter((task) => task.task_id !== taskId));
      setSelectedId((current) => current === taskId ? '' : current);
      setDiffTaskId('');
      setDiff(null);
      setNotice({ kind: 'success', text: `终态任务 ${taskId} 已删除；已批准的本体版本不会回滚。` });
      await loadTasks({ background: true });
    } catch (error) {
      if (accepted()) setNotice({ kind: 'error', text: error.message || '删除失败' });
    } finally {
      if (accepted()) setBusy('');
    }
  };

  return (
    <div className="evolution-grid">
      <SectionCard title="Skill 进化任务" description="按本体分组；运行中任务每 5 秒刷新，切换 Workspace 会立即清空旧结果。">
        <div className="evo-toolbar">
          <button className="btn" type="button" disabled={loading} onClick={() => loadTasks()}>{loading ? '刷新中…' : '刷新'}</button>
          {hasActive && <StatusTag status="active">自动刷新中</StatusTag>}
        </div>
        {scopeReady && loadError && <div className="evo-notice error" role="alert">{loadError}</div>}
        {(!scopeReady || loading) && !visibleTasks.length && <div className="empty">加载中…</div>}
        {scopeReady && !loading && !visibleTasks.length && !loadError && <EmptyState compact title="暂无 Skill 进化任务" />}
        {groups.map((group, groupIndex) => (
          <SkillTaskGroup
            group={group}
            key={group.ontologyId}
            initiallyOpen={groupIndex === 0 || group.activeCount > 0 || group.approvalCount > 0}
            selectedId={selectedId}
            onSelect={(taskId) => {
              setSelectedId(taskId);
              setDiffTaskId('');
              setDiff(null);
              setDiffLoading(false);
              setDiffError('');
              setBusy('');
              setNotice(null);
            }}
          />
        ))}
        <Notice notice={scopeReady ? notice : null} />
      </SectionCard>
      <div className="evolution-detail">
        {!selected && <SectionCard><EmptyState title="选择任务查看详情" /></SectionCard>}
        {selected && (
          <SectionCard
            title={selected.ontology_name || selected.ontology_id}
            description={`任务 ${selected.task_id} · 发起人 ${selected.triggered_by}`}
            actions={<StatusTag status={statusTone(selected.status)}>{STATUS_LABELS[selected.status] || selected.status}</StatusTag>}
          >
            <Table><tbody>
              <tr><th>提交版本</th><td>{selected.active_version || '—'}</td><th>运行</th><td>{selected.run_id || '—'}</td></tr>
              <tr><th>阶段</th><td>{selected.stage || '—'}</td><th>更新时间</th><td>{fmt(selected.updated_at)}</td></tr>
              <tr><th>决策</th><td>{selected.decision || '—'}</td><th>决策人</th><td>{selected.decided_by || '—'}</td></tr>
            </tbody></Table>
            {selected.error_message && <div className="evo-notice error" role="alert">{selected.error_message}</div>}
            {selected.status === 'no_change' && <div className="evo-notice">进化结果与提交版本一致，没有可审批的文件变更。</div>}
            {selected.status === 'interrupted' && <div className="evo-notice error">服务重启中断了任务，需要从原会话重新发起。</div>}
            {selected.status === 'cancelled' && <div className="evo-notice">任务已取消，未修改本体。</div>}
            {selected.decision && (
              <div className="evo-decision">
                {selected.decision === 'approve_overwrite' && `已覆盖提交时激活版本 ${selected.active_version}`}
                {selected.decision === 'approve_new_version' && `已创建版本 ${selected.new_version || '—'}`}
                {selected.decision === 'reject' && '审批已拒绝，原版本未修改'}
                {selected.decided_at && ` · ${fmt(selected.decided_at)}`}
              </div>
            )}
            {visibleDiff?.drift?.drifted && (
              <div className="evo-notice error" role="alert">
                当前版本已漂移：{visibleDiff.drift.message || visibleDiff.drift.reason || '请重新发起进化'}。批准操作已锁定，仍可拒绝本次结果。
              </div>
            )}
            {diffLoading && <div className="empty">正在生成文件差异…</div>}
            {diffError && <div className="evo-notice error" role="alert">{diffError}</div>}
            {!diffLoading && visibleDiff?.files?.length > 0 && visibleDiff.files.map((file) => (
              <FileDiff file={file} key={file.path} defaultOpen={visibleDiff.files.length === 1} />
            ))}
            {!diffLoading && canLoadSkillDiff(selected) && visibleDiff && !visibleDiff.files?.length && (
              <EmptyState compact title="没有文件差异" description={selected.change_summary || visibleDiff.change_summary} />
            )}
            {!canLoadSkillDiff(selected) && !canDeleteSkillTask(selected) && (
              <EmptyState compact title="任务完成后显示文件差异" description={selected.change_summary || selected.stage} />
            )}
            <div className="evo-actions evo-detail-actions">
              {canCancelSkillTask(selected, { isAdmin, username: user?.username }) && (
                <button className="btn btn-danger" type="button" disabled={Boolean(busy) || readOnly} onClick={() => act('cancel')}>
                  {busy === 'cancel' ? '取消中…' : '取消任务'}
                </button>
              )}
              {selected.status === 'completed' && isAdmin && (
                <SkillApprovalControls
                  key={selectedTaskId}
                  busy={Boolean(busy)}
                  disabled={readOnly}
                  drifted={Boolean(visibleDiff?.drift?.drifted)}
                  ready={diffReady}
                  onApprove={(payload) => act('approve', payload)}
                  onReject={() => act('reject')}
                />
              )}
              {isAdmin && canDeleteSkillTask(selected) && (
                <button className="btn btn-danger" type="button" disabled={Boolean(busy) || readOnly} onClick={deleteTask}>
                  {busy === 'delete' ? '删除中…' : '删除终态任务'}
                </button>
              )}
            </div>
          </SectionCard>
        )}
      </div>
    </div>
  );
}

function SkillTaskGroup({ group, initiallyOpen, selectedId, onSelect }) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <details className="evo-group" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <strong>{group.ontologyName}</strong>
        {group.activeCount > 0 && <StatusTag status="active">进行中 {group.activeCount}</StatusTag>}
        {group.approvalCount > 0 && <StatusTag status="warning">待审批 {group.approvalCount}</StatusTag>}
        <span>{group.tasks.length} 个任务</span>
      </summary>
      <div className="evolution-list">
        {group.tasks.map((task) => (
          <button type="button" key={task.task_id} className={task.task_id === selectedId ? 'on' : ''} onClick={() => onSelect(task.task_id)}>
            <strong className="evo-task-meta"><StatusTag status={statusTone(task.status)}>{STATUS_LABELS[task.status] || task.status}</StatusTag>{task.active_version || '未知版本'}</strong>
            <span>{fmt(task.updated_at)} · {task.triggered_by || '未知发起人'}</span>
            <small>{task.change_summary || task.stage || task.error_message || task.task_id}</small>
          </button>
        ))}
      </div>
    </details>
  );
}

function SkillApprovalControls({ busy, disabled, drifted, ready, onApprove, onReject }) {
  const [mode, setMode] = useState('new_version');
  const [versionName, setVersionName] = useState('');
  const [error, setError] = useState('');

  const approve = () => {
    try {
      const payload = buildSkillApprovalPayload(mode, versionName);
      setError('');
      onApprove(payload);
    } catch (approvalError) {
      setError(approvalError.message);
    }
  };

  return (
    <div className="evo-approval">
      <strong>审批方式</strong>
      <div>
        <label><input type="radio" name="skill-approval-mode" value="new_version" checked={mode === 'new_version'} onChange={() => setMode('new_version')} />创建新版本并立即激活</label>
        <label><input type="radio" name="skill-approval-mode" value="overwrite" checked={mode === 'overwrite'} onChange={() => setMode('overwrite')} />覆盖激活版本</label>
      </div>
      {mode === 'new_version' && (
        <input
          type="text"
          value={versionName}
          onChange={(event) => setVersionName(event.target.value)}
          placeholder="版本名留空则自动生成"
          aria-label="新版本名称"
        />
      )}
      {mode === 'new_version' && <div className="evo-decision">{SKILL_NEW_VERSION_ACTIVATION_COPY}</div>}
      {mode === 'overwrite' && <div className="evo-decision">覆盖会原位替换当前激活版本内容；服务端仍会进行 drift 校验。</div>}
      {!ready && <div className="evo-decision">文件差异和 drift 校验完成后才可批准；拒绝路径始终可用。</div>}
      {error && <div className="evo-notice error" role="alert">{error}</div>}
      <div className="evo-actions">
        <button className="btn btn-danger" type="button" disabled={busy || disabled} onClick={onReject}>拒绝结果</button>
        <button className="btn btn-p" type="button" disabled={busy || disabled || drifted || !ready} onClick={approve} title={disabled ? '历史版本下不会修改激活版本' : drifted ? '当前版本已漂移，无法批准' : !ready ? '等待差异和 drift 校验' : ''}>
          {mode === 'overwrite' ? '批准并覆盖' : '批准为新版本'}
        </button>
      </div>
    </div>
  );
}

function DiffText({ parts, value, side }) {
  if (!parts) return value || ' ';
  return parts.map((part, index) => {
    const changed = side === 'before' ? part.type === 'removed' : part.type === 'added';
    return changed
      ? <mark key={`${part.type}-${index}`}>{part.text}</mark>
      : <span key={`${part.type}-${index}`}>{part.text}</span>;
  });
}

function DiffRows({ rows }) {
  return rows.map((row, index) => {
    if (row.type === 'collapsed') {
      return <div className="evo-diff-collapsed" key={`collapsed-${row.oldStart}-${row.newStart}`}>折叠 {row.count} 行未变化内容</div>;
    }
    return (
      <div className={`evo-diff-row ${row.type}`} key={`${row.type}-${row.oldNumber}-${row.newNumber}-${index}`}>
        <span className="ln">{row.oldNumber || ''}</span>
        <span className="before"><DiffText parts={row.words?.before} value={row.before} side="before" /></span>
        <span className="ln">{row.newNumber || ''}</span>
        <span className="after"><DiffText parts={row.words?.after} value={row.after} side="after" /></span>
      </div>
    );
  });
}

function FileDiff({ file, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  const [expanded, setExpanded] = useState(false);
  const rows = useMemo(
    () => buildLineWordDiff(file.original, file.evolved, {
      contextLines: 3,
      collapseThreshold: expanded ? Number.MAX_SAFE_INTEGER : 8,
    }),
    [expanded, file.evolved, file.original],
  );
  const hiddenCount = rows.reduce((sum, row) => sum + (row.type === 'collapsed' ? row.count : 0), 0);

  return (
    <details className="evo-file-diff" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{file.path} · {file.change_type || 'modified'} · +{file.added_lines || 0} / -{file.removed_lines || 0}</summary>
      <div className="evo-toolbar evo-diff-toolbar">
        <span>左侧为原文件，右侧为进化结果；行内高亮显示词级变化。</span>
        {hiddenCount > 0 && <button className="btn" type="button" onClick={() => setExpanded(true)}>展开 {hiddenCount} 行未变化内容</button>}
        {expanded && <button className="btn" type="button" onClick={() => setExpanded(false)}>重新折叠未变化内容</button>}
      </div>
      <div className="evo-diff-grid" role="table" aria-label={`${file.path} 文件差异`}>
        <DiffRows rows={rows} />
      </div>
    </details>
  );
}
