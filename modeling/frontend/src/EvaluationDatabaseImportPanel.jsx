import { useEffect, useRef, useState } from 'react';
import {
  getEvaluationDatabaseTarget,
  importEvaluationDatabase,
  listEvaluationDatabaseImports,
} from './api.js';
import EvaluationDatabaseUrlConfirmModal from './EvaluationDatabaseUrlConfirmModal.jsx';

export default function EvaluationDatabaseImportPanel({
  projectId,
  dataSource,
  databaseUrlSelection,
  onContinue,
}) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [run, setRun] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [targetLoading, setTargetLoading] = useState(false);
  const [targetRequest, setTargetRequest] = useState(null);
  const [targetError, setTargetError] = useState('');
  const fileInputRef = useRef(null);
  const database = dataSource.config?.scene || '';
  const contextKey = `${projectId}:${dataSource.updatedAt || ''}`;
  const contextRef = useRef(contextKey);
  contextRef.current = contextKey;
  const running = run?.status === 'running';
  const controlsDisabled = busy || targetLoading || running || historyLoading;

  const loadRuns = async (requestContext, preferredRunId = '') => {
    try {
      const response = await listEvaluationDatabaseImports(projectId);
      if (contextRef.current !== requestContext) return;
      const imports = response.imports || [];
      const next = imports.find((item) => item.id === preferredRunId)
        || imports.find((item) => item.status === 'running')
        || imports[0]
        || null;
      setRun(next);
      setHistoryLoading(false);
    } catch (loadError) {
      if (contextRef.current === requestContext) {
        setError(loadError.message);
        setHistoryLoading(false);
      }
    }
  };

  useEffect(() => {
    setFile(null);
    setBusy(false);
    setError('');
    setRun(null);
    setHistoryLoading(true);
    setTargetLoading(false);
    setTargetRequest(null);
    setTargetError('');
    if (fileInputRef.current) fileInputRef.current.value = '';
    void loadRuns(contextKey);
  }, [contextKey]);

  useEffect(() => {
    if (!running) return undefined;
    const requestContext = contextKey;
    const runId = run.id;
    const timer = window.setInterval(() => {
      void loadRuns(requestContext, runId);
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [contextKey, run?.id, running]);

  const requestSubmit = async () => {
    if (!file) { setError('请选择 Excel 文件'); return; }
    const requestContext = contextKey;
    setTargetLoading(true); setError(''); setTargetError('');
    try {
      const response = await getEvaluationDatabaseTarget(projectId);
      if (contextRef.current === requestContext) {
        setTargetRequest({ ...response, lockedSelection: databaseUrlSelection || null });
      }
    } catch (targetLoadError) {
      if (contextRef.current === requestContext) {
        setError(`读取数据库连接失败：${targetLoadError.message}`);
      }
    } finally {
      if (contextRef.current === requestContext) setTargetLoading(false);
    }
  };

  const submit = async (selection) => {
    const requestContext = contextKey;
    setBusy(true); setError(''); setRun(null);
    setTargetError('');
    try {
      const response = await importEvaluationDatabase(projectId, file, true, selection);
      if (contextRef.current !== requestContext) return;
      setRun(response.run);
      setFile(null);
      setTargetRequest(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
    } catch (importError) {
      if (contextRef.current !== requestContext) return;
      if (importError.status === 409 && importError.data?.run) {
        setRun(importError.data.run);
        setTargetRequest(null);
        setError(importError.message);
      } else {
        setTargetError(importError.message);
      }
    } finally {
      if (contextRef.current === requestContext) setBusy(false);
    }
  };

  return (
    <section className="gi-panel edi-panel">
      <div className="gi-heading">
        <div>
          <div className="gi-title">Excel 导入评测库</div>
          <div className="gi-subtitle">
            MySQL · {database}
          </div>
        </div>
        <div className="edi-heading-actions">
          <label className={`btn ${controlsDisabled ? 'disabled' : ''}`}>
            {running ? '后台导入中…' : '选择 Excel'}
            <input ref={fileInputRef} type="file" accept=".xlsx,.xls" hidden disabled={controlsDisabled}
              onChange={(event) => {
                setFile(event.target.files?.[0] || null);
                setError('');
                setRun(null);
                setTargetRequest(null);
              }} />
          </label>
          <button className="btn btn-p" disabled={controlsDisabled || !file} onClick={requestSubmit}>
            {targetLoading ? '读取配置中…' : busy ? '提交中…' : running ? '后台导入中…' : '开始导入'}
          </button>
        </div>
      </div>

      {file && (
        <div className="gi-filebar">
          <div><b>文件</b>{file.name}</div>
          <div>{(file.size / 1024).toFixed(1)} KB</div>
          <button className="btn btn-sm" disabled={controlsDisabled} onClick={() => {
            setFile(null); setRun(null);
            setTargetRequest(null); setTargetError('');
            if (fileInputRef.current) fileInputRef.current.value = '';
          }}>移除</button>
        </div>
      )}
      {error && <div className="lerr">{error}</div>}
      {run?.status === 'running' && (
        <div className="gi-message">
          {run.filename} 正在后台写入 MySQL 数据库 {run.database}，切换步骤或刷新页面不会中断任务。
        </div>
      )}
      {run?.status === 'done' && (
        <div className="gi-message">
          后台导入完成：已写入 {run.tableCount} 张表、{run.rowCount} 行到 MySQL 数据库 {run.database}
        </div>
      )}
      {run?.status === 'failed' && (
        <div className="lerr">后台导入失败{run.error ? `：${run.error}` : ''}，请重新选择 Excel 后重试。</div>
      )}
      {onContinue && (
        <div className="gi-continue">
          <span>{running ? '评测库仍在写入，完成后才能运行评测。' : '评测数据可现在导入，也可以稍后回到本步骤继续导入。'}</span>
          <button className="btn btn-p" disabled={busy || targetLoading || running || historyLoading}
            onClick={() => onContinue(dataSource)}>进入评测用例录入 →</button>
        </div>
      )}
      {targetRequest && (
        <EvaluationDatabaseUrlConfirmModal
          title="确认 Excel 导入数据库"
          message="请确认所选连接是当前 NL2SQL 服务实际指向的评测库。本次导入会在后台全量写入 Excel 数据，每个 Sheet 写入一张同名表，目标库中的同名表会被替换。"
          databaseName={database}
          configured={targetRequest.configured}
          databaseUrlPreview={targetRequest.lockedSelection?.databaseUrlPreview || targetRequest.databaseUrlPreview}
          confirmLabel="确认并导入"
          selectionLocked={Boolean(targetRequest.lockedSelection) || targetRequest.configured}
          lockedSelection={targetRequest.lockedSelection}
          busy={busy}
          error={targetError}
          onCancel={() => { if (!busy) { setTargetRequest(null); setTargetError(''); } }}
          onConfirm={submit}
        />
      )}
    </section>
  );
}
