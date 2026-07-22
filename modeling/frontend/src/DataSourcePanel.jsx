import { useEffect, useRef, useState } from 'react';
import { getDataSource, getPackageFile, getSettings, saveDataSource } from './api.js';
import {
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
import GraphImportPanel from './GraphImportPanel.jsx';
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
  const projectContextRef = useRef(projectId);
  const providerRevisionRef = useRef(0);
  projectContextRef.current = projectId;

  useEffect(() => {
    let active = true;
    const contextKey = projectId;
    providerRevisionRef.current += 1;
    setForm(null); setSaved(null); setBusy(false); setSyncingOntologyId(false); setErr(''); setMessage('');
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
    setErr('');
    setMessage('');
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
      setForm(dataSourceToForm(result.dataSource));
      setMessage(submittedForm.mode === 'none' ? '配置已保存' : '连接测试通过，评测连接已同步，配置已保存');
      if (result.dataSource.mode !== 'connection' || result.dataSource.provider !== 'neo4j') {
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
          <button className="btn" disabled={busy || syncingOntologyId} onClick={startConfigurationGuide}>
            配置引导
          </button>
        )}
        <button className="btn btn-p" disabled={busy || syncingOntologyId} onClick={submit}>
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
    </div>
  );
}
