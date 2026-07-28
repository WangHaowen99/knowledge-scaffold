import { useEffect, useRef, useState } from 'react';
import {
  createProjectEvaluationDatabase,
  getDataSource,
  getPackageFile,
  getSettings,
  saveDataSource,
} from './api.js';
import {
  applyCreatedEvaluationDatabaseName,
  changeEvaluationDatabaseName,
  dataSourceFormError,
  dataSourceFormPayload,
  dataSourceToForm,
  evaluationDatabaseServerFormError,
  projectEvaluationDatabaseName,
  providerLabel,
  switchEvaluationDatabaseEngine,
} from './data-source-form.js';
import { runGraphImportContextRequest } from './graph-import-form.js';
import { loadLatestKnowledgePackageManifestId } from './knowledge-package-manifest.js';
import {
  DATA_SOURCE_TOUR_SCENE,
  dataSourceTourSteps,
} from './data-source-tour.js';
import DataSourceConnectionFields from './DataSourceConnectionFields.jsx';
import EvaluationDatabaseImportPanel from './EvaluationDatabaseImportPanel.jsx';
import GraphImportPanel from './GraphImportPanel.jsx';
import ProjectDatabaseCreateModal from './ProjectDatabaseCreateModal.jsx';
import Tour from './Tour.jsx';

export function DataSourceSummary({ dataSource }) {
  if (!dataSource) {
    return <div className="empty" style={{ padding: '14px 0' }}>尚未保存数据源配置</div>;
  }
  if (dataSource.mode === 'none') {
    return (
      <div className="ev-launch">
        <div style={{ fontWeight: 700, marginBottom: 4 }}>不使用数据源</div>
        <div style={{ color: 'var(--muted)', fontSize: 12.5 }}>评测时不配置本体业务数据源，适用于无需查询实例数据的用例。</div>
      </div>
    );
  }

  const config = dataSource.config || {};
  const evaluationDatabase = config.evaluation_database || {};
  const rows = dataSource.provider === 'neo4j'
    ? [
        ['Provider', providerLabel(dataSource.provider)],
        ['URL', config.uri],
        ['User', config.username],
        ['Password', config.has_password ? '已同步至评测服务（modeling 未保存）' : '未提供'],
        ['Database', config.database],
        ['Workspace 隔离条件', pairText(config.workspace_property, config.workspace_value)],
        ['Ontology 隔离条件', pairText(config.ontology_property, config.ontology_value)],
      ]
    : [
        ['Provider', providerLabel(dataSource.provider)],
        ['数据服务 URL', config.url],
        ['Token', config.has_token ? '已同步至评测服务（modeling 未保存）' : '未提供'],
        ['Scene', config.scene || '—'],
        ['评测工作空间 ID', config.evaluation_workspace_id],
        ['评测库类型', evaluationDatabase.engine === 'postgresql' ? 'PostgreSQL' : 'MySQL'],
        ['评测库地址', `${evaluationDatabase.host || '—'}:${evaluationDatabase.port || '—'}`],
        ['评测库 Database', evaluationDatabase.database],
        ['评测库 User', evaluationDatabase.username],
        ['评测库 Password', evaluationDatabase.has_password ? '已验证（modeling 未保存）' : '未提供'],
      ];

  return (
    <div className="ev-launch">
      <div style={{ fontWeight: 700, marginBottom: 10 }}>已保存的数据源配置</div>
      <div style={{ display: 'grid', gridTemplateColumns: '150px 1fr', gap: '7px 12px', fontSize: 12.5 }}>
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: 'contents' }}>
            <span style={{ color: 'var(--muted)' }}>{label}</span>
            <span style={{ overflowWrap: 'anywhere' }}>{String(value || '—')}</span>
          </div>
        ))}
      </div>
      {dataSource.provider === 'neo4j' && config.workspace_property && config.ontology_property && (
        <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 10 }}>
          两组隔离条件按 AND 联合匹配 Neo4j 中的实例、关系及路径元素。
        </div>
      )}
    </div>
  );
}

function pairText(property, value) {
  return property && value ? `${property} = ${value}` : '未配置';
}

export default function DataSourcePanel({ projectId, onAdvance }) {
  const [form, setForm] = useState(null);
  const [saved, setSaved] = useState(null);
  const [busy, setBusy] = useState(false);
  const [syncingOntologyId, setSyncingOntologyId] = useState(false);
  const [err, setErr] = useState('');
  const [message, setMessage] = useState('');
  const [databaseCreationRequest, setDatabaseCreationRequest] = useState(null);
  const [creatingDatabase, setCreatingDatabase] = useState(false);
  const [databaseCreationError, setDatabaseCreationError] = useState('');
  const projectContextRef = useRef(projectId);
  const providerRevisionRef = useRef(0);
  projectContextRef.current = projectId;

  useEffect(() => {
    let active = true;
    const contextKey = projectId;
    providerRevisionRef.current += 1;
    setForm(null); setSaved(null); setBusy(false); setSyncingOntologyId(false); setErr(''); setMessage('');
    setDatabaseCreationRequest(null); setCreatingDatabase(false); setDatabaseCreationError('');
    void runGraphImportContextRequest({
      contextKey,
      isCurrent: (key) => active && projectContextRef.current === key,
      request: () => Promise.all([
        getDataSource(contextKey),
        getSettings().catch(() => ({ evalBinding: {} })),
      ]),
    }).then((outcome) => {
      if (outcome.kind !== 'current') return;
      const [source, settings] = outcome.value;
      const dataSource = source.dataSource || null;
      setSaved(dataSource);
      setForm(dataSourceToForm(dataSource, settings.evalBinding || {}));
    }).catch((e) => {
      if (active && projectContextRef.current === contextKey) setErr(e.message);
    });
    return () => { active = false; };
  }, [projectId]);

  const set = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const changeProvider = (event) => {
    const provider = event.target.value;
    providerRevisionRef.current += 1;
    setForm((current) => ({ ...current, provider }));
    setSyncingOntologyId(false);
    setDatabaseCreationRequest(null);
    setDatabaseCreationError('');
    setErr('');
    setMessage('');
  };
  const changeDatabaseEngine = (event) => {
    const engine = event.target.value;
    setForm((current) => switchEvaluationDatabaseEngine(current, engine));
    setErr('');
    setMessage('');
  };
  const changeDatabaseName = (event) => {
    const database = event.target.value;
    setForm((current) => changeEvaluationDatabaseName(current, database));
    setErr('');
    setMessage('');
  };
  const requestProjectDatabaseCreation = () => {
    const validationError = evaluationDatabaseServerFormError(form);
    if (validationError) {
      setErr(validationError);
      return;
    }
    setErr('');
    setMessage('');
    setDatabaseCreationError('');
    setDatabaseCreationRequest({ database: projectEvaluationDatabaseName(projectId) });
  };
  const cancelProjectDatabaseCreation = () => {
    if (creatingDatabase) return;
    setDatabaseCreationRequest(null);
    setDatabaseCreationError('');
  };
  const confirmProjectDatabaseCreation = async () => {
    const contextKey = projectContextRef.current;
    const input = {
      engine: form.eval_db_engine,
      host: form.eval_db_host,
      port: form.eval_db_port,
      username: form.eval_db_username,
      password: form.eval_db_password,
    };
    setCreatingDatabase(true);
    setDatabaseCreationError('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: (key) => projectContextRef.current === key,
        request: () => createProjectEvaluationDatabase(contextKey, input),
      });
      if (outcome.kind === 'stale') return;
      const result = outcome.value;
      setForm((current) => applyCreatedEvaluationDatabaseName(current, result.database));
      setMessage(result.created
        ? `项目数据库已创建：${result.database}`
        : `项目数据库已存在，已直接使用：${result.database}`);
      setDatabaseCreationRequest(null);
    } catch (error) {
      if (projectContextRef.current === contextKey) setDatabaseCreationError(error.message);
    } finally {
      if (projectContextRef.current === contextKey) setCreatingDatabase(false);
    }
  };
  const startConfigurationGuide = () => window.dispatchEvent(new CustomEvent(
    'mw-tour-start',
    { detail: { scene: DATA_SOURCE_TOUR_SCENE } },
  ));
  const syncOntologyId = async () => {
    const contextKey = projectContextRef.current;
    const providerRevision = providerRevisionRef.current;
    const isCurrentSync = () => projectContextRef.current === contextKey
      && providerRevisionRef.current === providerRevision;
    setSyncingOntologyId(true); setErr(''); setMessage('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: isCurrentSync,
        request: () => loadLatestKnowledgePackageManifestId(contextKey, getPackageFile),
      });
      if (outcome.kind === 'stale') return;
      setForm((current) => ({ ...current, ontology_value: outcome.value }));
      setMessage(`已从最新知识包 manifest.yaml 同步 Ontology ID：${outcome.value}`);
    } catch (e) {
      if (isCurrentSync()) setErr(`同步知识包 ID 失败：${e.message}`);
    } finally {
      if (isCurrentSync()) setSyncingOntologyId(false);
    }
  };
  const submit = async () => {
    const validationError = dataSourceFormError(form);
    if (validationError) {
      setErr(validationError);
      return;
    }
    const contextKey = projectContextRef.current;
    const submittedForm = form;
    setBusy(true); setErr(''); setMessage('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: (key) => projectContextRef.current === key,
        request: () => saveDataSource(contextKey, dataSourceFormPayload(submittedForm)),
      });
      if (outcome.kind === 'stale') return;
      const result = outcome.value;
      setSaved(result.dataSource);
      const nextForm = dataSourceToForm(result.dataSource);
      if (submittedForm.provider !== 'neo4j') {
        nextForm.eval_db_password = submittedForm.eval_db_password;
      }
      setForm(nextForm);
      setMessage(submittedForm.mode === 'none' ? '配置已保存' : '连接测试通过，评测连接已同步，配置已保存');
      if (result.dataSource.mode !== 'connection') {
        onAdvance?.(result.dataSource);
      }
    } catch (e) {
      if (projectContextRef.current === contextKey) setErr(e.message);
    } finally {
      if (projectContextRef.current === contextKey) setBusy(false);
    }
  };

  if (!form) return <div className="empty">加载中…</div>;
  return (
    <div>
      <Tour scene={DATA_SOURCE_TOUR_SCENE} steps={dataSourceTourSteps(form.provider)} autoStart={false} />
      {err && <div className="lerr">{err}</div>}
      <label className="ev-bind-toggle" style={{ marginBottom: 14 }}>
        <input type="checkbox" checked={form.mode === 'connection'}
          onChange={(e) => setForm((current) => ({ ...current, mode: e.target.checked ? 'connection' : 'none' }))} />
        使用业务数据源
      </label>

      {form.mode === 'connection' ? (
        <DataSourceConnectionFields
          form={form}
          busy={busy}
          syncingOntologyId={syncingOntologyId}
          onFieldChange={set}
          onProviderChange={changeProvider}
          onDatabaseEngineChange={changeDatabaseEngine}
          onDatabaseNameChange={changeDatabaseName}
          creatingDatabase={creatingDatabase}
          onCreateProjectDatabase={requestProjectDatabaseCreation}
          onSyncOntologyId={syncOntologyId}
        />
      ) : (
        <div className="ev-launch" style={{ color: 'var(--muted)', fontSize: 12.5 }}>
          评测将不使用业务数据源；已有本体数据源配置会在每轮评测导入后被清除。
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 12, marginTop: 14 }}>
        {message && <span style={{ color: 'var(--good)', fontSize: 12.5 }}>{message}</span>}
        {form.mode === 'connection' && (
          <button className="btn" disabled={busy || syncingOntologyId || creatingDatabase} onClick={startConfigurationGuide}>
            配置引导
          </button>
        )}
        <button className="btn btn-p" disabled={busy || syncingOntologyId || creatingDatabase} onClick={submit}>
          {busy ? '保存并测试中…' : form.mode === 'connection' ? '保存并测试连接' : '保存并继续'}
        </button>
      </div>

      {saved?.mode === 'connection' && saved.provider === 'neo4j' && (
        <>
          <GraphImportPanel projectId={projectId} dataSource={saved} />
          <div className="gi-continue">
            <span>图数据可现在导入，也可以稍后回到本步骤继续导入。</span>
            <button className="btn btn-p" onClick={() => onAdvance?.(saved)}>进入评测用例录入 →</button>
          </div>
        </>
      )}
      {saved?.mode === 'connection' && ['nl2sql_template', 'nl2sql_http'].includes(saved.provider) && (
        <>
          <EvaluationDatabaseImportPanel
            projectId={projectId}
            dataSource={saved}
            initialPassword={form.eval_db_password}
            onContinue={() => onAdvance?.(saved)}
          />
        </>
      )}
      {databaseCreationRequest && (
        <ProjectDatabaseCreateModal
          databaseName={databaseCreationRequest.database}
          engine={form.eval_db_engine}
          busy={creatingDatabase}
          error={databaseCreationError}
          onCancel={cancelProjectDatabaseCreation}
          onConfirm={confirmProjectDatabaseCreation}
        />
      )}
    </div>
  );
}
