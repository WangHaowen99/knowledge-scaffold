import { useEffect, useRef, useState } from 'react';
import {
  importEvaluationDatabase,
  listEvaluationDatabaseImports,
} from './api.js';

const engineLabel = (engine) => engine === 'postgresql' ? 'PostgreSQL' : 'MySQL';

export default function EvaluationDatabaseImportPanel({
  projectId,
  dataSource,
  initialPassword = '',
  onContinue,
}) {
  const [file, setFile] = useState(null);
  const [password, setPassword] = useState(initialPassword);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [run, setRun] = useState(null);
  const [replaceConfirmed, setReplaceConfirmed] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const fileInputRef = useRef(null);
  const database = dataSource.config?.evaluation_database || {};
  const contextKey = `${projectId}:${dataSource.updatedAt || ''}`;
  const contextRef = useRef(contextKey);
  contextRef.current = contextKey;
  const running = run?.status === 'running';
  const controlsDisabled = busy || running || historyLoading;

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
    setPassword(initialPassword);
    setBusy(false);
    setError('');
    setRun(null);
    setReplaceConfirmed(false);
    setHistoryLoading(true);
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

  const submit = async () => {
    if (!file) { setError('请选择 Excel 文件'); return; }
    if (!password.trim()) { setError('本次导入的评测库 Password 不能为空'); return; }
    if (!replaceConfirmed) { setError('请先确认同名表替换规则'); return; }
    const requestContext = contextKey;
    setBusy(true); setError(''); setRun(null);
    try {
      const response = await importEvaluationDatabase(projectId, file, password, true);
      if (contextRef.current !== requestContext) return;
      setRun(response.run);
      setFile(null);
      setPassword('');
      setReplaceConfirmed(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    } catch (importError) {
      if (contextRef.current !== requestContext) return;
      if (importError.status === 409 && importError.data?.run) {
        setRun(importError.data.run);
        setPassword('');
        setReplaceConfirmed(false);
      }
      setError(importError.message);
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
            {engineLabel(database.engine)} · {database.host}:{database.port}/{database.database}
          </div>
        </div>
        <label className={`btn ${controlsDisabled ? 'disabled' : ''}`}>
          {running ? '后台导入中…' : '选择 Excel'}
          <input ref={fileInputRef} type="file" accept=".xlsx,.xls" hidden disabled={controlsDisabled}
            onChange={(event) => {
              setFile(event.target.files?.[0] || null);
              setReplaceConfirmed(false);
              setError('');
              setRun(null);
            }} />
        </label>
      </div>

      {file && (
        <div className="gi-filebar">
          <div><b>文件</b>{file.name}</div>
          <div>{(file.size / 1024).toFixed(1)} KB</div>
          <button className="btn btn-sm" disabled={controlsDisabled} onClick={() => {
            setFile(null); setRun(null); setReplaceConfirmed(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
          }}>移除</button>
        </div>
      )}
      {error && <div className="lerr">{error}</div>}
      {run?.status === 'running' && (
        <div className="gi-message">
          {run.filename} 正在后台写入 {engineLabel(run.engine)} 数据库 {run.database}，切换步骤或刷新页面不会中断任务。
        </div>
      )}
      {run?.status === 'done' && (
        <div className="gi-message">
          后台导入完成：已写入 {run.tableCount} 张表、{run.rowCount} 行到 {engineLabel(run.engine)} 数据库 {run.database}
        </div>
      )}
      {run?.status === 'failed' && (
        <div className="lerr">后台导入失败{run.error ? `：${run.error}` : ''}，请重新选择 Excel 后重试。</div>
      )}

      <div className="gi-execute edi-execute">
        <div className="field">
          <label>评测库 Password</label>
          <input type="password" value={password} autoComplete="off" disabled={controlsDisabled}
            onChange={(event) => setPassword(event.target.value)} placeholder="密码仅用于本次导入" />
        </div>
        <button className="btn btn-p" disabled={controlsDisabled || !file || !replaceConfirmed} onClick={submit}>
          {busy ? '提交中…' : running ? '后台导入中…' : '开始后台导入'}
        </button>
      </div>
      <label className="ev-bind-toggle edi-replace-confirmation">
        <input type="checkbox" checked={replaceConfirmed} disabled={controlsDisabled || !file}
          onChange={(event) => { setReplaceConfirmed(event.target.checked); setError(''); }} />
        我确认：Excel 中的每个 Sheet 会替换目标数据库中的同名表
      </label>
      <div className="ds-field-hint">上传完成后在后台全量写入；每个 Sheet 写入一张同名表，目标库中的同名表会被替换。</div>
      {onContinue && (
        <div className="gi-continue">
          <span>{running ? '评测库仍在写入，完成后才能运行评测。' : '评测数据可现在导入，也可以稍后回到本步骤继续导入。'}</span>
          <button className="btn btn-p" disabled={busy || running || historyLoading}
            onClick={() => onContinue(dataSource)}>进入评测用例录入 →</button>
        </div>
      )}
    </section>
  );
}
