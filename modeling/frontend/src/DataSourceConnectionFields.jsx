import React, { useId } from 'react';
import { PROVIDER_OPTIONS } from './data-source-form.js';
import {
  DATA_SOURCE_TOUR_TARGETS,
  ONTOLOGY_ID_SYNC_BUTTON_LABEL,
} from './data-source-tour.js';

export default function DataSourceConnectionFields({
  form,
  busy,
  syncingOntologyId,
  onFieldChange,
  onProviderChange,
  creatingDatabase,
  onCreateProjectDatabase,
  onSyncOntologyId,
}) {
  const providerId = useId();
  return (
    <div className="ev-launch">
      <div className="field" data-tour={DATA_SOURCE_TOUR_TARGETS.provider}>
        <label htmlFor={providerId}>Provider</label>
        <select id={providerId} aria-label="Provider" value={form.provider} onChange={onProviderChange}>
          {PROVIDER_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </div>

      {form.provider === 'neo4j' ? (
        <Neo4jFields
          form={form}
          busy={busy}
          syncingOntologyId={syncingOntologyId}
          onFieldChange={onFieldChange}
          onSyncOntologyId={onSyncOntologyId}
        />
      ) : (
        <Nl2SqlFields
          form={form}
          busy={busy}
          onFieldChange={onFieldChange}
          creatingDatabase={creatingDatabase}
          onCreateProjectDatabase={onCreateProjectDatabase}
        />
      )}
    </div>
  );
}

function Neo4jFields({ form, busy, syncingOntologyId, onFieldChange, onSyncOntologyId }) {
  return (
    <>
      <div className="ds-field-grid">
        <Field label="Neo4j URL" value={form.uri} onChange={onFieldChange('uri')} placeholder="bolt://neo4j:7687"
          tourId={DATA_SOURCE_TOUR_TARGETS.neo4jUri} />
        <Field label="Database" value={form.database} onChange={onFieldChange('database')} placeholder="neo4j"
          tourId={DATA_SOURCE_TOUR_TARGETS.neo4jDatabase} />
        <Field label="User" value={form.username} onChange={onFieldChange('username')} placeholder="neo4j"
          tourId={DATA_SOURCE_TOUR_TARGETS.neo4jUser} />
        <Field label="Password" type="password" value={form.password} onChange={onFieldChange('password')}
          placeholder="用于本次连接测试并同步至评测服务" tourId={DATA_SOURCE_TOUR_TARGETS.neo4jPassword} />
      </div>
      <div style={{ fontWeight: 700, fontSize: 12.5, margin: '10px 0 8px' }}>Neo4j 数据隔离条件</div>
      <div className="ds-field-grid">
        <Field label="Workspace 隔离字段" value={form.workspace_property} onChange={onFieldChange('workspace_property')}
          tourId={DATA_SOURCE_TOUR_TARGETS.workspaceProperty} />
        <Field label="Workspace 对应值" value={form.workspace_value} onChange={onFieldChange('workspace_value')}
          tourId={DATA_SOURCE_TOUR_TARGETS.workspaceValue} />
        <Field label="Ontology 隔离字段" value={form.ontology_property} onChange={onFieldChange('ontology_property')}
          tourId={DATA_SOURCE_TOUR_TARGETS.ontologyProperty} />
        <Field label="Ontology 对应值" value={form.ontology_value} onChange={onFieldChange('ontology_value')}
          tourId={DATA_SOURCE_TOUR_TARGETS.ontologyValue}
          action={(
            <button type="button" className="btn" disabled={busy || syncingOntologyId} onClick={onSyncOntologyId}>
              {syncingOntologyId ? '同步中…' : ONTOLOGY_ID_SYNC_BUTTON_LABEL}
            </button>
          )} />
      </div>
      <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 8 }}>
        字段和值必须成对填写；两组都填写时按 AND 联合匹配指定 Database 中的实例、关系及路径元素。Workspace 对应值同时作为 consumer 评测工作空间 ID：须以英文小写字母开头，只能包含小写字母、数字、_、-，最长 64 个字符；已存在时直接复用。
      </div>
    </>
  );
}

function Nl2SqlFields({
  form,
  busy,
  onFieldChange,
  creatingDatabase,
  onCreateProjectDatabase,
}) {
  const tokenPlaceholder = '服务启用鉴权时填写（可选）';
  return (
    <>
      <div className="ds-field-grid">
        <Field label="数据服务 URL" value={form.url} onChange={onFieldChange('url')} placeholder="http://localhost:5600"
          tourId={DATA_SOURCE_TOUR_TARGETS.url} />
        <Field label="Scene" value={form.scene} onChange={onFieldChange('scene')} placeholder="例如 mv_shadow_project_id"
          tourId={DATA_SOURCE_TOUR_TARGETS.scene}
          action={(
            <button type="button" className="btn" disabled={busy || creatingDatabase}
              onClick={onCreateProjectDatabase}>
              {creatingDatabase ? '读取中…' : '使用项目专用数据库'}
            </button>
          )} />
        <Field label="Token" type="password" value={form.token} onChange={onFieldChange('token')}
          placeholder={tokenPlaceholder} tourId={DATA_SOURCE_TOUR_TARGETS.token} />
        <Field label="评测工作空间 ID" value={form.evaluation_workspace_id}
          onChange={onFieldChange('evaluation_workspace_id')} placeholder="例如 nl2sql-eval"
          tourId={DATA_SOURCE_TOUR_TARGETS.evaluationWorkspace} />
      </div>
      <div className="ds-field-hint">
        Scene 对应 MySQL 数据库；使用项目专用数据库和 Excel 导入时，都会先确认服务端连接或仅为本次操作指定其他 URL。
      </div>
    </>
  );
}

function Field({ label, type = 'text', value, onChange, placeholder = '', action = null, tourId }) {
  const inputId = useId();
  const input = <input id={inputId} aria-label={label} type={type} value={value ?? ''}
    onChange={onChange} placeholder={placeholder} autoComplete="off" />;
  return (
    <div className="field" data-tour={tourId}>
      <label htmlFor={inputId}>{label}</label>
      {action ? <div className="field-control-row">{input}{action}</div> : input}
    </div>
  );
}
