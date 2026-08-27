import { Database, LoaderCircle, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { listEvaluationDatabaseImports, quickImportEvaluationDatabase } from './api.js';

export default function QuickEvaluationDatabaseImport({ projectId, onConfigured, onContinue }) {
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const inputRef = useRef(null);
  const contextRef = useRef(projectId);
  contextRef.current = projectId;

  const loadRun = async (preferredId = '') => {
    try {
      const response = await listEvaluationDatabaseImports(projectId);
      if (contextRef.current !== projectId) return;
      const imports = response.imports || [];
      setRun(imports.find((item) => item.id === preferredId)
        || imports.find((item) => item.status === 'running') || imports[0] || null);
    } catch (cause) {
      if (contextRef.current === projectId) setError(cause.message);
    } finally {
      if (contextRef.current === projectId) setLoading(false);
    }
  };

  useEffect(() => {
    setRun(null); setBusy(false); setLoading(true); setError('');
    void loadRun();
  }, [projectId]);

  useEffect(() => {
    if (run?.status !== 'running') return undefined;
    const timer = window.setInterval(() => void loadRun(run.id), 2_000);
    return () => window.clearInterval(timer);
  }, [projectId, run?.id, run?.status]);

  const select = async (file) => {
    if (!file) return;
    setBusy(true); setError('');
    try {
      const response = await quickImportEvaluationDatabase(projectId, file);
      if (contextRef.current !== projectId) return;
      setRun(response.run);
      onConfigured?.(response.dataSource);
    } catch (cause) {
      if (contextRef.current !== projectId) return;
      if (cause.status === 409 && cause.data?.run) setRun(cause.data.run);
      setError(cause.message);
    } finally {
      if (inputRef.current) inputRef.current.value = '';
      if (contextRef.current === projectId) setBusy(false);
    }
  };

  const running = run?.status === 'running';
  return <section className="edi-quick">
    <div className="edi-quick-copy"><Database size={20} /><div><b>一键导入 Excel 数据源</b><span>自动创建项目专属数据库并完成连接配置；每个 Sheet 写入一张表，同名表会被替换。</span></div></div>
    <label className={`btn btn-p ${busy || running || loading ? 'disabled' : ''}`}>
      {busy || running ? <LoaderCircle className="wb-spin" size={15} /> : <Upload size={15} />}
      {busy ? '正在提交…' : running ? '后台导入中…' : '选择 Excel 并导入'}
      <input ref={inputRef} hidden type="file" accept=".xlsx,.xls" disabled={busy || running || loading}
        onChange={(event) => void select(event.target.files?.[0])} />
    </label>
    {error && <div className="lerr edi-quick-status">{error}</div>}
    {run?.status === 'running' && <div className="gi-message edi-quick-status">{run.filename} 正在后台导入，刷新或切换页面不会中断。</div>}
    {run?.status === 'failed' && <div className="lerr edi-quick-status">导入失败{run.error ? `：${run.error}` : ''}，请修正 Excel 后重试。</div>}
    {run?.status === 'done' && <div className="gi-message edi-quick-status">导入完成：{run.tableCount} 张表、{run.rowCount} 行已写入项目专属数据库。{onContinue && <button className="btn btn-p btn-sm" onClick={onContinue}>进入评测用例录入</button>}</div>}
  </section>;
}
