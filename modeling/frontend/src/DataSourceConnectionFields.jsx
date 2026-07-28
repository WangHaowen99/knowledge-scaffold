import {
  EVALUATION_DATABASE_OPTIONS,
  PROVIDER_OPTIONS,
} from './data-source-form.js';
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
  onDatabaseEngineChange,
  onDatabaseNameChange,
  creatingDatabase,
  onCreateProjectDatabase,
  onSyncOntologyId,
}) {
  return (
    <div className="ev-launch">
      <div className="field" data-tour={DATA_SOURCE_TOUR_TARGETS.provider}>
        <label>Provider</label>
        <select value={form.provider} onChange={onProviderChange}>
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
          onDatabaseEngineChange={onDatabaseEngineChange}
          onDatabaseNameChange={onDatabaseNameChange}
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
  onDatabaseEngineChange,
  onDatabaseNameChange,
  creatingDatabase,
  onCreateProjectDatabase,
}) {
  const tokenPlaceholder = form.provider === 'nl2sql_template'
    ? 'NL2SQL Template 必填，用于连接测试并同步至评测服务'
    : '服务启用鉴权时填写（可选）';
  return (
    <>
      <div className="ds-field-grid">
        <Field label="数据服务 URL" value={form.url} onChange={onFieldChange('url')} placeholder="http://localhost:5600"
          tourId={DATA_SOURCE_TOUR_TARGETS.url} />
        <Field label="Scene" value={form.scene} onChange={onFieldChange('scene')} placeholder="留空使用评测库 Database"
          tourId={DATA_SOURCE_TOUR_TARGETS.scene} />
        <Field label="Token" type="password" value={form.token} onChange={onFieldChange('token')}
          placeholder={tokenPlaceholder} tourId={DATA_SOURCE_TOUR_TARGETS.token} />
        <Field label="评测工作空间 ID" value={form.evaluation_workspace_id}
          onChange={onFieldChange('evaluation_workspace_id')} placeholder="例如 nl2sql-eval"
          tourId={DATA_SOURCE_TOUR_TARGETS.evaluationWorkspace} />
      </div>

      <div className="ds-section-title">评测库配置</div>
      <div className="ds-field-grid">
        <SelectField label="数据库类型" value={form.eval_db_engine}
          onChange={onDatabaseEngineChange} options={EVALUATION_DATABASE_OPTIONS}
          tourId={DATA_SOURCE_TOUR_TARGETS.evalDbEngine} />
        <Field label="Host" value={form.eval_db_host} onChange={onFieldChange('eval_db_host')}
          placeholder="127.0.0.1" tourId={DATA_SOURCE_TOUR_TARGETS.evalDbHost} />
        <Field label="Port" type="number" value={form.eval_db_port} onChange={onFieldChange('eval_db_port')}
          placeholder={form.eval_db_engine === 'postgresql' ? '5432' : '3306'}
          tourId={DATA_SOURCE_TOUR_TARGETS.evalDbPort} />
        <Field label="Database" value={form.eval_db_database} onChange={onDatabaseNameChange}
          placeholder="evaluation" tourId={DATA_SOURCE_TOUR_TARGETS.evalDbDatabase}
          action={(
            <button type="button" className="btn" disabled={busy || creatingDatabase}
              onClick={onCreateProjectDatabase}>
              {creatingDatabase ? '创建中…' : '新建项目数据库'}
            </button>
          )} />
        <Field label="User" value={form.eval_db_username} onChange={onFieldChange('eval_db_username')}
          placeholder={form.eval_db_engine === 'postgresql' ? 'postgres' : 'root'}
          tourId={DATA_SOURCE_TOUR_TARGETS.evalDbUser} />
        <Field label="Password" type="password" value={form.eval_db_password}
          onChange={onFieldChange('eval_db_password')} placeholder="每次保存并测试时填写"
          tourId={DATA_SOURCE_TOUR_TARGETS.evalDbPassword} />
      </div>
      <div className="ds-field-hint">
        保存时会同时测试 NL2SQL 服务与评测库；密码不落库。Excel 导入会替换目标 Database 中与 Sheet 同名的表。
      </div>
    </>
  );
}

function SelectField({ label, value, onChange, options, tourId }) {
  return (
    <div className="field" data-tour={tourId}>
      <label>{label}</label>
      <select value={value} onChange={onChange}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </div>
  );
}

function Field({ label, type = 'text', value, onChange, placeholder = '', action = null, tourId }) {
  const input = <input type={type} value={value ?? ''} onChange={onChange} placeholder={placeholder} autoComplete="off" />;
  return (
    <div className="field" data-tour={tourId}>
      <label>{label}</label>
      {action ? <div className="field-control-row">{input}{action}</div> : input}
    </div>
  );
}
