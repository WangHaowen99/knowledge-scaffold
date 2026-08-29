import React, { useEffect, useRef, useState } from 'react';
import {
  createProjectEvaluationDatabase,
  getDataSource,
  getEvaluationDatabaseTarget,
  getPackageFile,
  getSettings,
  saveDataSource,
} from './api.js';
import {
  applyCreatedProjectDatabaseScene,
  dataSourceFormError,
  dataSourceFormPayload,
  dataSourceToForm,
  providerLabel,
} from './data-source-form.js';
import { runGraphImportContextRequest } from './graph-import-form.js';
import { loadLatestKnowledgePackageManifestId } from './knowledge-package-manifest.js';
import {
  DATA_SOURCE_TOUR_SCENE,
  dataSourceTourSteps,
} from './data-source-tour.js';
import DataSourceConnectionFields from './DataSourceConnectionFields.jsx';
import EvaluationDatabaseUrlConfirmModal from './EvaluationDatabaseUrlConfirmModal.jsx';
import EvaluationDatabaseImportPanel from './EvaluationDatabaseImportPanel.jsx';
import GraphImportPanel from './GraphImportPanel.jsx';
import { PACKAGE_DATA_SOURCE_DETAIL, PACKAGE_DATA_SOURCE_HELP } from './package-data-source.js';
import Tour from './Tour.jsx';

const PACKAGE_DATA_SOURCE_HELP_ID = 'package-data-source-resource-help';

export function PackageDataSourceHelp() {
  return (
    <span className="pkg-source-help">
      <button type="button" className="pkg-source-help-icon" aria-label="查看数据源映射说明"
        aria-describedby={PACKAGE_DATA_SOURCE_HELP_ID}>?</button>
      <span id={PACKAGE_DATA_SOURCE_HELP_ID} className="pkg-source-tooltip" role="tooltip">
        {PACKAGE_DATA_SOURCE_HELP}
        {/* 实现细节放在后面且弱化：业务专家不需要它，实施同事需要时能看到 */}
        <span className="pkg-source-tooltip-detail">{PACKAGE_DATA_SOURCE_DETAIL}</span>
      </span>
    </span>
  );
}

export function DataSourceSummary({ dataSource, error = '' }) {
  if (error) {
    return <div className="lerr" style={{ marginBottom: 0 }}>{error}</div>;
  }
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

function projectEvaluationDatabaseName(projectId) {
  const normalized = String(projectId || '').trim().replace(/[^a-zA-Z0-9_]/g, '_');
  return normalized ? `mv_shadow_${normalized}`.slice(0, 60) : '';
}

function isNl2SqlProvider(provider) {
  return ['nl2sql_template', 'nl2sql_http'].includes(provider);
}

function databaseUrlPreview(databaseUrl) {
  try {
    const parsed = new URL(String(databaseUrl || '').trim());
    if (parsed.password) parsed.password = '******';
    return parsed.toString();
  } catch {
    return '自定义 MySQL URL';
  }
}

export default function DataSourcePanel({ projectId, onAdvance, saveDataSourceRequest = saveDataSource }) {
  const [form, setForm] = useState(null);
  const [saved, setSaved] = useState(null);
  const [providerSelectionChanged, setProviderSelectionChanged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [syncingOntologyId, setSyncingOntologyId] = useState(false);
  const [err, setErr] = useState('');
  const [message, setMessage] = useState('');
  const [databaseCreationRequest, setDatabaseCreationRequest] = useState(null);
  const [creatingDatabase, setCreatingDatabase] = useState(false);
  const [databaseCreationError, setDatabaseCreationError] = useState('');
  const [projectDatabaseSelection, setProjectDatabaseSelection] = useState(null);
  const projectContextRef = useRef(projectId);
  const providerRevisionRef = useRef(0);
  projectContextRef.current = projectId;

  useEffect(() => {
    let active = true;
    const contextKey = projectId;
    providerRevisionRef.current += 1;
    setForm(null); setSaved(null); setProviderSelectionChanged(false); setBusy(false);
    setSyncingOntologyId(false); setErr(''); setMessage('');
    setDatabaseCreationRequest(null); setCreatingDatabase(false); setDatabaseCreationError('');
    setProjectDatabaseSelection(null);
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
    setProviderSelectionChanged(true);
    setForm((current) => ({ ...current, provider }));
    setSyncingOntologyId(false);
    setDatabaseCreationRequest(null);
    setDatabaseCreationError('');
    setProjectDatabaseSelection(null);
    setErr('');
    setMessage('');
  };
  const requestProjectDatabaseCreation = async () => {
    const contextKey = projectContextRef.current;
    const providerRevision = providerRevisionRef.current;
    const isCurrentRequest = () => projectContextRef.current === contextKey
      && providerRevisionRef.current === providerRevision;
    setCreatingDatabase(true);
    setErr(''); setMessage(''); setDatabaseCreationError('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: isCurrentRequest,
        request: () => getEvaluationDatabaseTarget(contextKey),
      });
      if (outcome.kind === 'current') setDatabaseCreationRequest(outcome.value);
    } catch (error) {
      if (isCurrentRequest()) setErr(`读取数据库连接失败：${error.message}`);
    } finally {
      if (isCurrentRequest()) setCreatingDatabase(false);
    }
  };
  const cancelProjectDatabaseCreation = () => {
    if (creatingDatabase) return;
    setDatabaseCreationRequest(null);
    setDatabaseCreationError('');
  };
  const confirmProjectDatabaseCreation = async (selection) => {
    const contextKey = projectContextRef.current;
    setCreatingDatabase(true);
    setDatabaseCreationError('');
    try {
      const outcome = await runGraphImportContextRequest({
        contextKey,
        isCurrent: (key) => projectContextRef.current === key,
        request: () => createProjectEvaluationDatabase(contextKey, selection),
      });
      if (outcome.kind === 'stale') return;
      const result = outcome.value;
      setForm((current) => applyCreatedProjectDatabaseScene(current, result.database));
      setProjectDatabaseSelection({
        mode: selection.mode,
        databaseUrl: selection.mode === 'custom' ? selection.databaseUrl : '',
        databaseUrlPreview: selection.mode === 'custom'
          ? databaseUrlPreview(selection.databaseUrl)
          : databaseCreationRequest?.databaseUrlPreview || '',
      });
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
        request: () => saveDataSourceRequest(contextKey, dataSourceFormPayload(submittedForm)),
      });
      if (outcome.kind === 'stale') return;
      const result = outcome.value;
      setSaved(result.dataSource);
      setProviderSelectionChanged(false);
      setForm(dataSourceToForm(result.dataSource));
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
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 14 }}>
        <label className="ev-bind-toggle">
          <input type="checkbox" checked={form.mode === 'connection'}
            onChange={(e) => setForm((current) => ({ ...current, mode: e.target.checked ? 'connection' : 'none' }))} />
          使用业务数据源
        </label>
        <PackageDataSourceHelp />
      </div>

      {form.mode === 'connection' ? (
        <DataSourceConnectionFields
          form={form}
          busy={busy}
          syncingOntologyId={syncingOntologyId}
          onFieldChange={set}
          onProviderChange={changeProvider}
          creatingDatabase={creatingDatabase}
          onCreateProjectDatabase={requestProjectDatabaseCreation}
          onSyncOntologyId={syncOntologyId}
        />
      ) : (
        <div className="ev-launch" style={{ color: 'var(--muted)', fontSize: 12.5 }}>
          评测将不使用业务数据源；已有本体数据源配置会在每轮评测导入后被清除。
        </div>
      )}

      <div className="ds-panel-actions">
        {message && <span style={{ color: 'var(--good)', fontSize: 12.5 }}>{message}</span>}
        {form.mode === 'connection' && (
          <button type="button" className="btn" disabled={busy || syncingOntologyId || creatingDatabase}
            onClick={startConfigurationGuide}>
            配置引导
          </button>
        )}
        <button type="button" className="btn btn-p" disabled={busy || syncingOntologyId || creatingDatabase}
          onClick={submit}>
          {busy ? '保存并测试中…' : form.mode === 'connection' ? '保存并测试连接' : '保存并继续'}
        </button>
      </div>

      {!providerSelectionChanged && saved?.mode === 'connection' && saved.provider === 'neo4j' && (
        <>
          <GraphImportPanel projectId={projectId} dataSource={saved} />
          <div className="gi-continue">
            <span>图数据可现在导入，也可以稍后回到本步骤继续导入。</span>
            <button className="btn btn-p" onClick={() => onAdvance?.(saved)}>进入评测用例录入 →</button>
          </div>
        </>
      )}
      {!providerSelectionChanged && saved?.mode === 'connection' && isNl2SqlProvider(saved.provider)
        && saved.config?.scene === projectEvaluationDatabaseName(projectId) && (
        <EvaluationDatabaseImportPanel
          projectId={projectId}
          dataSource={saved}
          databaseUrlSelection={projectDatabaseSelection}
          onContinue={() => onAdvance?.(saved)}
        />
      )}
      {!providerSelectionChanged && saved?.mode === 'connection' && isNl2SqlProvider(saved.provider)
        && saved.config?.scene !== projectEvaluationDatabaseName(projectId) && (
        <div className="gi-continue">
          <span>当前使用已有 NL2SQL Scene，将直接查询该数据源；如需导入 Excel，请先使用项目专用数据库。</span>
          <button className="btn btn-p" onClick={() => onAdvance?.(saved)}>进入评测用例录入 →</button>
        </div>
      )}
      {databaseCreationRequest && (
        <EvaluationDatabaseUrlConfirmModal
          title="使用项目专用数据库"
          message="请确认服务端 EVAL_DB_URL 是否为当前 NL2SQL 服务实际指向的评测库；也可以为本次建库指定其他 MySQL URL。"
          databaseName={databaseCreationRequest.database}
          configured={databaseCreationRequest.configured}
          databaseUrlPreview={databaseCreationRequest.databaseUrlPreview}
          confirmLabel="确认并使用"
          busy={creatingDatabase}
          error={databaseCreationError}
          onCancel={cancelProjectDatabaseCreation}
          onConfirm={confirmProjectDatabaseCreation}
        />
      )}
    </div>
  );
}
