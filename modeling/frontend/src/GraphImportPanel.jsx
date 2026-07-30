import React, { useEffect, useRef, useState } from 'react';
import {
  cancelGraphImport,
  executeGraphImport,
  listGraphImports,
  uploadGraphImport,
  validateGraphImport,
} from './api.js';
import {
  GRAPH_ISOLATION_EXISTS_PROMPT,
  activeGraphImportRun,
  addRelationMapping,
  graphImportContextKey,
  graphImportMappingPayload,
  graphRelationLabel,
  hasRunningGraphImport,
  joinPropertyOptions,
  objectImportIssue,
  relationCanMatchSameRow,
  relationDraftFromKnown,
  runGraphImportContextRequest,
  startGraphImportExecution,
  updateRelationMapping,
} from './graph-import-form.js';

const statusLabel = { running: '执行中', done: '成功', failed: '失败' };

function initialRelationDraft(preview) {
  const objects = (preview?.schema?.objects || []).filter((object) => !objectImportIssue(preview, object));
  return {
    sourceObject: objects[0]?.key || '',
    relationName: '',
    targetObject: objects[1]?.key || objects[0]?.key || '',
  };
}

export default function GraphImportPanel({ projectId, dataSource }) {
  const [preview, setPreview] = useState(null);
  const [mappings, setMappings] = useState([]);
  const [relationDraft, setRelationDraft] = useState(initialRelationDraft());
  const [knownRelationId, setKnownRelationId] = useState('');
  const [validation, setValidation] = useState(null);
  const [password, setPassword] = useState('');
  const [history, setHistory] = useState([]);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [replacementRequest, setReplacementRequest] = useState(null);
  const [activeRunId, setActiveRunId] = useState('');
  const currentImportContextKey = graphImportContextKey(projectId, dataSource);
  const importContextRef = useRef(currentImportContextKey);
  const replacementInFlightRef = useRef('');
  const passwordInputRef = useRef(null);
  const replacementDialogRef = useRef(null);
  const restorePasswordFocusRef = useRef(false);
  importContextRef.current = currentImportContextKey;
  const hasRunningImport = hasRunningGraphImport(history);
  const activeRun = activeGraphImportRun(history, activeRunId);
  const controlsDisabled = Boolean(busy || hasRunningImport || activeRunId || replacementRequest);

  const loadHistory = async (
    contextKey = importContextRef.current,
    requestProjectId = projectId,
    isCurrent = (key) => importContextRef.current === key,
  ) => {
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent,
        request: () => listGraphImports(requestProjectId),
      });
      if (outcome.kind === 'current') setHistory(outcome.value.imports || []);
      return outcome;
    } catch (err) {
      if (isCurrent(contextKey)) setError(err.message);
      return { kind: 'error', error: err };
    }
  };

  useEffect(() => {
    setPreview(null);
    setMappings([]);
    setRelationDraft(initialRelationDraft());
    setKnownRelationId('');
    setValidation(null);
    setPassword('');
    setError('');
    setMessage('');
    setReplacementRequest(null);
    setActiveRunId('');
    setHistory([]);
    setBusy('');
    replacementInFlightRef.current = '';
    restorePasswordFocusRef.current = false;
    void loadHistory(currentImportContextKey, projectId);
  }, [currentImportContextKey]);

  useEffect(() => {
    if (busy === 'replace') replacementDialogRef.current?.focus();
  }, [busy]);

  useEffect(() => {
    if (busy || replacementRequest || !restorePasswordFocusRef.current) return;
    restorePasswordFocusRef.current = false;
    passwordInputRef.current?.focus();
  }, [busy, replacementRequest]);

  useEffect(() => {
    if (!activeRunId && !hasRunningImport) return undefined;
    let active = true;
    const contextKey = currentImportContextKey;
    const timer = window.setInterval(() => {
      void loadHistory(
        contextKey,
        projectId,
        (key) => active && importContextRef.current === key,
      );
    }, 2_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [activeRunId, currentImportContextKey, hasRunningImport, projectId]);

  useEffect(() => {
    if (!activeRunId || !activeRun || activeRun.status === 'running') return;
    setActiveRunId('');
    setPassword('');
    setReplacementRequest(null);
    if (activeRun.status === 'done') {
      setPreview(null);
      setMappings([]);
      setValidation(null);
      setRelationDraft(initialRelationDraft());
      setKnownRelationId('');
      setError('');
      setMessage(`后台导入成功：${activeRun.nodeCount} 个实例 / ${activeRun.relationshipCount} 条关系`);
      return;
    }
    setError(`后台导入失败${activeRun.error ? `：${activeRun.error}` : ''}`);
    setMessage('临时 Excel 和映射已保留，可重新输入密码后重试');
  }, [activeRun, activeRunId]);

  const invalidate = () => {
    setValidation(null);
    setPassword('');
    setMessage('');
  };

  const chooseFile = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    const contextKey = importContextRef.current;
    const requestProjectId = projectId;
    const previousUploadId = preview?.id;
    setBusy('upload'); setError(''); setMessage('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: (key) => importContextRef.current === key,
        request: () => uploadGraphImport(requestProjectId, file),
        onStale: (result) => result?.upload?.id
          ? cancelGraphImport(requestProjectId, result.upload.id)
          : undefined,
      });
      if (outcome.kind === 'stale') return;
      if (previousUploadId) void cancelGraphImport(requestProjectId, previousUploadId).catch(() => {});
      setPreview(outcome.value.upload);
      setMappings([]);
      setValidation(null);
      setPassword('');
      setRelationDraft(initialRelationDraft(outcome.value.upload));
      setKnownRelationId('');
      setMessage('Excel 已解析，可校验实体数据并按需新增关系');
    } catch (err) {
      if (importContextRef.current === contextKey) setError(err.message);
    } finally {
      if (importContextRef.current === contextKey) setBusy('');
    }
  };

  const addRelation = () => {
    try {
      const next = addRelationMapping(mappings, relationDraft, preview);
      setMappings(next);
      invalidate();
      setRelationDraft((current) => ({ ...current, relationName: '' }));
      setKnownRelationId('');
    } catch (err) {
      setError(err.message);
    }
  };

  const chooseKnownRelation = (relationId) => {
    setKnownRelationId(relationId);
    const relation = preview?.schema?.relations?.find((item) => item.id === relationId);
    if (relation) setRelationDraft(relationDraftFromKnown(relation));
  };

  const changeMapping = (index, field, value) => {
    setMappings((current) => updateRelationMapping(current, index, field, value, preview));
    setError('');
    invalidate();
  };

  const removeMapping = (index) => {
    setMappings((current) => current.filter((_, currentIndex) => currentIndex !== index));
    invalidate();
  };

  const validate = async () => {
    const contextKey = importContextRef.current;
    const requestProjectId = projectId;
    const uploadId = preview.id;
    setBusy('validate'); setError(''); setMessage('');
    try {
      const payload = graphImportMappingPayload(mappings);
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: (key) => importContextRef.current === key,
        request: () => validateGraphImport(requestProjectId, uploadId, payload),
      });
      if (outcome.kind === 'stale') return;
      setValidation(outcome.value.validation);
      setMessage('映射校验通过，可输入本次执行密码并写入 Neo4j');
    } catch (err) {
      if (importContextRef.current !== contextKey) return;
      setValidation(null);
      setError(err.message);
    } finally {
      if (importContextRef.current === contextKey) setBusy('');
    }
  };

  const finishExecution = async (contextKey, run) => {
    if (importContextRef.current !== contextKey) return;
    setReplacementRequest(null);
    setPassword('');
    setActiveRunId(run.id);
    setMessage('后台写入已开始；若任务失败，临时 Excel 和映射会保留以便重试');
    await loadHistory(contextKey);
  };

  const execute = async () => {
    const contextKey = importContextRef.current;
    setBusy('execute'); setError(''); setMessage('');
    try {
      if (!password) throw new Error('请输入本次导入使用的 Neo4j Password');
      const payload = graphImportMappingPayload(mappings);
      const outcome = await startGraphImportExecution({
        execute: executeGraphImport,
        request: { projectId, uploadId: preview.id, payload, password, contextKey },
        isCurrent: (requestContextKey) => importContextRef.current === requestContextKey,
      });
      if (outcome.kind === 'stale') return;
      if (outcome.kind === 'conflict') {
        setReplacementRequest(outcome.request);
        return;
      }
      await finishExecution(contextKey, outcome.run);
    } catch (err) {
      if (importContextRef.current !== contextKey) return;
      setPassword('');
      setError(err.message);
      await loadHistory(contextKey);
    } finally {
      if (importContextRef.current === contextKey) setBusy('');
    }
  };

  const dismissReplacement = () => {
    if (busy === 'replace') return;
    setReplacementRequest(null);
    setPassword('');
    setMessage('已取消覆盖，图数据库中的旧数据未发生变化');
    restorePasswordFocusRef.current = true;
  };

  const confirmReplacement = async () => {
    const request = replacementRequest;
    if (!request || busy || replacementInFlightRef.current
      || request.contextKey !== importContextRef.current) return;
    replacementInFlightRef.current = request.contextKey;
    setBusy('replace'); setError(''); setMessage('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey: request.contextKey,
        isCurrent: (key) => importContextRef.current === key,
        request: () => executeGraphImport(
          request.projectId,
          request.uploadId,
          request.payload,
          request.password,
          true,
        ),
      });
      if (outcome.kind === 'stale') return;
      await finishExecution(request.contextKey, outcome.value.run);
    } catch (err) {
      if (importContextRef.current !== request.contextKey) return;
      setReplacementRequest(null);
      setPassword('');
      setError(err.message);
      restorePasswordFocusRef.current = true;
      await loadHistory(request.contextKey);
    } finally {
      if (replacementInFlightRef.current === request.contextKey) {
        replacementInFlightRef.current = '';
      }
      if (importContextRef.current === request.contextKey) setBusy('');
    }
  };

  const cancel = async () => {
    if (!preview?.id) return;
    const contextKey = importContextRef.current;
    const requestProjectId = projectId;
    const uploadId = preview.id;
    setBusy('cancel'); setError('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: (key) => importContextRef.current === key,
        request: () => cancelGraphImport(requestProjectId, uploadId),
      });
      if (outcome.kind === 'stale') return;
      setPreview(null);
      setMappings([]);
      setValidation(null);
      setPassword('');
      setReplacementRequest(null);
      replacementInFlightRef.current = '';
      setRelationDraft(initialRelationDraft());
      setKnownRelationId('');
      setMessage('已取消本次导入并删除临时 Excel');
    } catch (err) {
      if (importContextRef.current === contextKey) setError(err.message);
    } finally {
      if (importContextRef.current === contextKey) setBusy('');
    }
  };

  const config = dataSource?.config || {};
  const isolationReady = config.workspace_property && config.workspace_value
    && config.ontology_property && config.ontology_value;

  return (
    <section className="gi-panel">
      <div className="gi-heading">
        <div>
          <div className="gi-title">Excel 图数据导入</div>
          <div className="gi-subtitle">
            原始资料不会长期保存，请在这里重新上传 Excel。一个 Sheet 可以映射多个实体；文件仅短期暂存。
          </div>
        </div>
        <label className={`btn ${controlsDisabled ? 'disabled' : ''}`}>
          {activeRunId || hasRunningImport ? '后台导入执行中' : busy === 'upload' ? '上传解析中…' : preview ? '更换 Excel' : '选择 Excel'}
          <input type="file" accept=".xlsx,.xls" hidden disabled={controlsDisabled} onChange={chooseFile} />
        </label>
      </div>

      {!isolationReady && (
        <div className="lerr">请先完整配置 Workspace 与 Ontology 两组隔离字段及对应值。</div>
      )}
      {error && <div className="lerr">{error}</div>}
          {message && <div className="gi-message">{message}</div>}
      {(activeRunId || hasRunningImport) && (
        <div className="gi-message">图数据正在后台写入，切换步骤或刷新页面不会中断任务。</div>
      )}

      {preview && (
        <>
          <div className="gi-filebar">
            <div><b>{preview.filename}</b><span>{formatBytes(preview.sizeBytes)}</span></div>
            <div>{preview.sheets.map((sheet) => `${sheet.name}（${sheet.rowCount} 行）`).join(' · ')}</div>
            <button className="tc-del" disabled={controlsDisabled} onClick={cancel}>取消导入</button>
          </div>

          {!preview.schema.relations.length && (
            <div className="gi-message">当前知识包未预定义关系，可在此直接新增导入关系。</div>
          )}
          <div className="gi-add">
            {preview.schema.relations.length > 0 && (
              <select value={knownRelationId} disabled={controlsDisabled}
                onChange={(event) => chooseKnownRelation(event.target.value)}>
                <option value="">选择知识包中的关系（可选）</option>
                {preview.schema.relations.map((relation) => (
                  <option key={relation.id} value={relation.id}>{graphRelationLabel(relation, preview)}</option>
                ))}
              </select>
            )}
            <select value={relationDraft.sourceObject} disabled={controlsDisabled}
              onChange={(event) => setRelationDraft((current) => ({ ...current, sourceObject: event.target.value }))}>
              <option value="">选择源实体</option>
              {preview.schema.objects.map((object) => {
                const issue = objectImportIssue(preview, object);
                return <option key={object.key} value={object.key} disabled={!!issue}>
                  {object.title}（{object.key}）{issue ? ` - 不可导入：${issue}` : ''}
                </option>;
              })}
            </select>
            <input value={relationDraft.relationName} placeholder="关系名称" disabled={controlsDisabled}
              onChange={(event) => setRelationDraft((current) => ({ ...current, relationName: event.target.value }))} />
            <select value={relationDraft.targetObject} disabled={controlsDisabled}
              onChange={(event) => setRelationDraft((current) => ({ ...current, targetObject: event.target.value }))}>
              <option value="">选择目标实体</option>
              {preview.schema.objects.map((object) => {
                const issue = objectImportIssue(preview, object);
                return <option key={object.key} value={object.key} disabled={!!issue}>
                  {object.title}（{object.key}）{issue ? ` - 不可导入：${issue}` : ''}
                </option>;
              })}
            </select>
            <button className="btn" disabled={!relationDraft.sourceObject || !relationDraft.relationName.trim()
              || !relationDraft.targetObject || controlsDisabled} onClick={addRelation}>新增关系</button>
          </div>

          {mappings.map((mapping, index) => (
            <RelationCard key={`${mapping.sourceObject}|${mapping.relationName}|${mapping.targetObject}`} index={index} mapping={mapping} preview={preview}
              disabled={controlsDisabled} onChange={changeMapping} onRemove={removeMapping} />
          ))}

          <div className="gi-actions">
            <button className="btn" disabled={controlsDisabled} onClick={validate}>
              {busy === 'validate' ? '校验中…' : '校验映射与数据'}
            </button>
            {validation && (
              <div className="gi-counts">
                <b>{validation.counts.nodeCount}</b> 个实例
                <b>{validation.counts.relationshipCount}</b> 条关系
                <b>{validation.counts.skippedBlankCount}</b> 个空关联值跳过
                <b>{validation.counts.skippedUnmatchedCount}</b> 个未匹配关系跳过
              </div>
            )}
            {validation?.warnings?.length > 0 && (
              <div className="gi-warnings">{validation.warnings.join('；')}</div>
            )}
          </div>

          {validation && (
            <div className="gi-execute">
              <div className="field">
                <label>本次执行 Neo4j Password（不会保存）</label>
                <input ref={passwordInputRef} type="password" value={password} disabled={controlsDisabled} autoComplete="new-password"
                  onChange={(event) => setPassword(event.target.value)} placeholder="每次执行均需重新输入" />
              </div>
              <button className="btn btn-p" disabled={controlsDisabled || !password || !isolationReady} onClick={execute}>
                {busy === 'execute' ? '正在写入…' : '写入实例与关系'}
              </button>
            </div>
          )}
        </>
      )}

      <ImportHistory imports={history} />
      {replacementRequest && (
        <GraphIsolationConflictModal
          busy={busy === 'replace'}
          dialogRef={replacementDialogRef}
          onCancel={dismissReplacement}
          onConfirm={confirmReplacement}
        />
      )}
    </section>
  );
}

export function GraphIsolationConflictModal({ busy, dialogRef, onCancel, onConfirm }) {
  const cancel = () => { if (!busy) onCancel(); };
  const confirm = () => { if (!busy) onConfirm(); };
  const handleKeyDown = (event) => {
    if (event.key === 'Escape') {
      if (busy) return;
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(event.currentTarget.querySelectorAll('button:not([disabled])'));
    if (busy || !focusable.length) {
      event.preventDefault();
      event.currentTarget.querySelector('[role="dialog"]')?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = event.currentTarget.ownerDocument.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };
  return (
    <div className="modal-ov"
      onClick={(event) => { if (event.target === event.currentTarget) cancel(); }}
      onKeyDown={handleKeyDown}>
      <div ref={dialogRef} className="modal gi-conflict-modal" role="dialog" aria-modal="true" tabIndex={-1}
        aria-labelledby="gi-conflict-title" aria-describedby="gi-conflict-message" aria-busy={busy}>
        <h3 id="gi-conflict-title">确认覆盖旧数据</h3>
        <p id="gi-conflict-message" className="gi-conflict-message">{GRAPH_ISOLATION_EXISTS_PROMPT}</p>
        <div className="foot">
          <button className="btn" disabled={busy} onClick={cancel}>取消</button>
          <button className="btn btn-p" disabled={busy} autoFocus onClick={confirm}>
            {busy ? '正在清空并写入…' : '清空旧数据并写入'}
          </button>
        </div>
      </div>
    </div>
  );
}

export function RelationCard({ index, mapping, preview, disabled, onChange, onRemove }) {
  const sourceObject = preview.schema.objects.find((item) => item.key === mapping.sourceObject);
  const targetObject = preview.schema.objects.find((item) => item.key === mapping.targetObject);
  const canMatchSameRow = relationCanMatchSameRow(preview, mapping);
  const sameRow = mapping.matchMode === 'same_row';
  return (
    <div className="gi-relation">
      <div className="gi-relation-head">
        <b>{graphRelationLabel(mapping, preview)}</b>
        <button className="tc-del" disabled={disabled} onClick={() => onRemove(index)}>移除</button>
      </div>
      {canMatchSameRow && (
        <div className="gi-match-row">
          <span className="gi-match-label">关联方式</span>
          <div className="gi-match-mode" role="radiogroup" aria-label="关联方式">
            <button type="button" role="radio" aria-checked={sameRow}
              className={sameRow ? 'active' : ''} disabled={disabled}
              onClick={() => onChange(index, 'matchMode', 'same_row')}>同一数据行</button>
            <button type="button" role="radio" aria-checked={!sameRow}
              className={!sameRow ? 'active' : ''} disabled={disabled}
              onClick={() => onChange(index, 'matchMode', 'field_match')}>按字段值匹配</button>
          </div>
          <span className="gi-match-hint" role="note">
            来源于同表的两个实体关联方式可以选择同一数据行，或者自由选择某一关联字段关联。
          </span>
        </div>
      )}
      {sameRow ? (
        <div className="gi-same-row">
          <span>固定 Sheet</span>
          <b>{sourceObject?.defaultSheet || '-'}</b>
          <span>同一数据行</span>
        </div>
      ) : (
        <div className="gi-sides">
          <RelationSide title={`源实体：${sourceObject?.title || mapping.sourceObject}`} side="source"
            mapping={mapping} preview={preview} disabled={disabled}
            onChange={(field, value) => onChange(index, field, value)} />
          <div className="gi-arrow">→</div>
          <RelationSide title={`目标实体：${targetObject?.title || mapping.targetObject}`} side="target"
            mapping={mapping} preview={preview} disabled={disabled}
            onChange={(field, value) => onChange(index, field, value)} />
        </div>
      )}
    </div>
  );
}

function RelationSide({ title, side, mapping, preview, disabled, onChange }) {
  const object = preview.schema.objects.find((item) => item.key === mapping[`${side}Object`]);
  const joinField = `${side}JoinProperty`;
  const properties = joinPropertyOptions(preview, object?.key);
  return (
    <div className="gi-side">
      <div className="gi-side-title">{title}</div>
      <div className="field">
        <label>固定 Sheet</label>
        <div className="gi-fixed-sheet">{object?.defaultSheet || '-'}</div>
      </div>
      <SelectField label="关系关联属性" value={mapping[joinField]} disabled={disabled}
        options={properties} onChange={(value) => onChange(joinField, value)} />
    </div>
  );
}

function SelectField({ label, value, options, disabled, onChange }) {
  const normalized = options.map((option) => (
    typeof option === 'string' ? { value: option, label: option } : option
  ));
  return (
    <div className="field">
      <label>{label}</label>
      <select value={value} disabled={disabled || !normalized.length} onChange={(event) => onChange(event.target.value)}>
        {!normalized.length && <option value="">无可用属性</option>}
        {normalized.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    </div>
  );
}

function ImportHistory({ imports }) {
  if (!imports.length) return null;
  return (
    <div className="gi-history">
      <div className="gi-history-title">最近导入</div>
      {imports.slice(0, 5).map((item) => (
        <div className="gi-history-row" key={item.id}>
          <span className={`tag gi-${item.status}`}>{statusLabel[item.status] || item.status}</span>
          <b>{item.filename}</b>
          <span>{item.nodeCount} 个实例 / {item.relationshipCount} 条关系</span>
          <time>{formatTime(item.finishedAt || item.createdAt)}</time>
          {item.error && <div className="gi-history-error">{item.error}</div>}
        </div>
      ))}
    </div>
  );
}

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(value) {
  try { return new Date(value).toLocaleString(); } catch { return value; }
}
