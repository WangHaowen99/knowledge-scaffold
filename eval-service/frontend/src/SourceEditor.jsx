import React, { useEffect, useRef, useState } from 'react';
import { api, randomId } from './api.js';
import { GRAPH_SCOPE_DEFAULTS, emptySource, packageReference, sourceFromRecord, sourcePayload } from './domain.js';
import { Alert, Badge, Breadcrumb, Button, Confirm, Field, Heading, Icon, useAction } from './ui.jsx';

export function GraphScopeStatus({ check }) {
  if (!check) return null;
  const reused = check.import_state === 'unchanged';
  const blocked = !reused && check.boundary_relationship_count > 0;
  return (
    <Alert
      tone={blocked ? 'red' : check.node_count && !reused ? 'amber' : 'green'}
      title={reused ? '图数据与数据源一致' : blocked ? '发现跨范围关系，暂不能替换' : check.node_count ? '检测到旧图数据' : '当前范围没有旧图数据'}
    >
      {check.scope.workspace_property} = {check.scope.workspace_value}，
      {check.scope.ontology_property} = {check.scope.ontology_value}（同时匹配）。
      共 {check.node_count} 个节点、{check.relationship_count} 条范围内关系。
      {reused
        ? '开始评测时直接使用这份数据，不删除也不重新导入。'
        : blocked
        ? `另有 ${check.boundary_relationship_count} 条跨范围关系，请先处理这些关系或调整范围。`
        : check.node_count
          ? '开始评测时将再次检查，并询问是否删除旧数据后导入新数据。'
          : '开始评测时将再次检查后导入。'}
    </Alert>
  );
}

// Objects of the package whose table binding does not match the data tables, returned with a mapping conflict.
const CONFLICT_KINDS = {
  declared_missing: '声明的来源表不在数据中',
  no_columns: '绑定的表没有一列能对上属性',
};
export function MappingConflicts({ details }) {
  const issues = details?.issues;
  if (details?.problems?.length) return (
    <Alert tone="red" title="知识包关系结构需要修正">
      <ul>{details.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>
      {details.total > details.problems.length && <p>共 {details.total} 项，当前显示前 {details.problems.length} 项。</p>}
    </Alert>
  );
  if (!issues?.length) return null;
  return (
    <div className="mapping-preview mapping-conflicts">
      <div className="section-title">
        <h3>对象与数据表不一致</h3>
        <Badge tone="red">{issues.length} 个对象</Badge>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>对象</th>
              <th>声明或匹配的表</th>
              <th>问题</th>
            </tr>
          </thead>
          <tbody>
            {issues.map((issue) => (
              <tr key={issue.object + issue.kind}>
                <td>{issue.object}</td>
                <td>
                  {issue.table || '—'}
                  {issue.candidates?.length > 0 && <small>可选：{issue.candidates.join('、')}</small>}
                </td>
                <td>{CONFLICT_KINDS[issue.kind] || issue.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="notice">在建模中修改这些对象的“数据来源”后重新发布，或改用“不使用数据源”评测。</p>
    </div>
  );
}

// 新建评测中的数据源预览：还没拿到结果时说明正在预览，或可以重新预览
export function SourcePreview({ preview, busy, graph, onRetry }) {
  return (
    <div className="preview-action">
      {!preview && (
        <p className="muted">
          {busy ? `正在预览${graph ? '图导入' : '数据映射'}…` : '没有拿到预览结果。'}
          {!busy && (
            <Button variant="text" onClick={onRetry}>
              重新预览
            </Button>
          )}
        </p>
      )}
      <MappingPreview preview={preview} />
    </div>
  );
}

export function MappingPreview({ preview }) {
  if (!preview) return null;
  const objects = Array.isArray(preview.objects)
    ? preview.objects
    : Object.entries(preview.objects || {}).map(([name, value]) => ({ name, ...value }));
  // Objects the run cannot query fully, named above the table so they are not missed in a long list.
  const missing = objects.filter((o) => o.table && o.missing_columns?.length),
    unbound = objects.filter((o) => !o.table),
    issues = missing.length + unbound.length;
  return (
    <div className="mapping-preview">
      <div className="section-title">
        <h3>{preview.graph ? '图导入预览' : '映射预览'}</h3>
        {issues ? <Badge tone="amber">{issues} 项需要留意</Badge> : <Badge tone="green">准备就绪</Badge>}
      </div>
      {issues > 0 && (
        <Alert tone="amber">
          {[
            missing.length > 0 && missing.map((o) => `「${o.name}」缺字段 ${o.missing_columns.join('、')}`).join('；'),
            unbound.length > 0 && `${unbound.map((o) => `「${o.name}」`).join('')}没有绑定数据表`,
          ]
            .filter(Boolean)
            .join('；')}
          。可以继续评测，相关问题可能因为查不到数据而失败。
        </Alert>
      )}
      {preview.graph && (
        <div className="graph-counts">
          <div>
            <b>{preview.entity_count}</b>实体类型
          </div>
          <div>
            <b>{preview.instances}</b>实例
          </div>
          <div>
            <b>{preview.relationship_count}</b>关系
          </div>
        </div>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>对象</th>
              <th>来源表</th>
              <th>字段匹配</th>
              {preview.graph && <th>实例</th>}
            </tr>
          </thead>
          <tbody>
            {objects.map((o) => (
              <tr key={o.name}>
                <td>{o.name}</td>
                <td>{o.table ? <code>{o.table}</code> : <Badge tone="amber">未绑定</Badge>}</td>
                <td>
                  {Array.isArray(o.matched_columns) ? o.matched_columns.length : o.matched_columns || 0} 列
                  {o.missing_columns?.length > 0 && <Badge tone="red">缺 {o.missing_columns.join('、')}</Badge>}
                </td>
                {preview.graph && <td>{o.instances}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {preview.relations?.map((r, i) => (
        <div className="relation-row" key={i}>
          {r.source}
          <span>— {r.relation} →</span>
          {r.target}
          <b>{r.instances == null ? '无法计算' : `${r.instances} 条关系`}</b>
        </div>
      ))}
      {preview.graph && !preview.relations?.length && (
        <p className="muted">未声明可执行关系，本轮仅导入节点。</p>
      )}
      {(preview.warnings || []).map((w, i) => (
        <Alert tone="amber" key={i}>
          {typeof w === 'string' ? w : '部分对象绑定需要确认'}
        </Alert>
      ))}
      {preview.truncated && (
        <Alert tone="amber">预览已截断，仅展示部分对象或关系。完整执行以本轮映射为准。</Alert>
      )}
    </div>
  );
}

// 隔离范围不在这里编辑：在新建评测中选中数据源后填写，随数据源保存。这里只说明并显示已保存的值
function ScopeNote({ connection, graph = false }) {
  const saved = graph
    ? connection.workspace_value &&
      `${connection.workspace_property || GRAPH_SCOPE_DEFAULTS.workspace_property} = ${connection.workspace_value}`
    : connection.evaluation_workspace_id && `评测工作空间 ${connection.evaluation_workspace_id}`;
  return (
    <div className="scope-note">
      <Icon name="info" />
      <div>
        <strong>评测隔离范围在新建评测中填写</strong>
        <span>
          {graph
            ? '选中本数据源后填写 Workspace 与 Ontology 隔离字段及对应值，随数据源保存。'
            : '选中本数据源后填写评测工作空间 ID，随数据源保存。'}
          {saved ? ` 当前：${saved}` : ' 当前尚未填写。'}
        </span>
      </div>
    </div>
  );
}

export function SourceEditor({ record, packageRef, onSaved, onClose, onDirty, parentLabel = '数据源' }) {
  const [form, setForm] = useState(record ? sourceFromRecord(record) : emptySource());
  const [preview, setPreview] = useState(null),
    [graphCheck, setGraphCheck] = useState(null),
    [dirty, setDirty] = useState(false),
    [discard, setDiscard] = useState(false);
  const [uploads, setUploads] = useState([]),
    [testing, setTesting] = useState(false),
    [testResult, setTestResult] = useState(null),
    [testError, setTestError] = useState('');
  const { busy, error, details, act } = useAction(),
    file = useRef(null),
    revision = useRef(0),
    mounted = useRef(true),
    uploadLock = useRef(false),
    testController = useRef(null),
    onDirtyRef = useRef(onDirty);
  onDirtyRef.current = onDirty;
  useEffect(() => {
    onDirtyRef.current?.(dirty);
    return () => onDirtyRef.current?.(false);
  }, [dirty]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      testController.current?.abort();
    };
  }, []);
  const graph = form.provider === 'neo4j';
  function change(patch) {
    testController.current?.abort();
    revision.current += 1;
    setDirty(true);
    setPreview(null);
    setGraphCheck(null);
    setTestResult(null);
    setTestError('');
    setForm((f) => ({ ...f, ...patch }));
  }
  function connection(key, value) {
    change({ connection: { ...form.connection, [key]: value } });
  }
  function provider(value) {
    change(
      {
        provider: value,
        import_data: false,
        secret_configured: false,
        connection: { ...form.connection, token: '', password: '' },
      },
    );
  }
  function close() {
    if (busy || uploadLock.current) return;
    if (dirty) setDiscard(true);
    else onClose();
  }
  async function testConnection() {
    if (testController.current) return;
    const controller = new AbortController();
    testController.current = controller;
    const version = revision.current;
    setTesting(true);
    setTestResult(null);
    setTestError('');
    try {
      const result = await api('/data-source-tests', {
        provider: form.provider,
        connection: sourcePayload(form).connection,
        data_source_id: form.id || '',
      }, 'POST', true, 'json', undefined, { signal: controller.signal });
      if (mounted.current && revision.current === version) setTestResult(result);
    } catch (error) {
      if (mounted.current && revision.current === version && !controller.signal.aborted)
        setTestError(error.message);
    } finally {
      if (testController.current === controller) testController.current = null;
      if (mounted.current) setTesting(false);
    }
  }
  async function upload(files) {
    if (uploadLock.current) return;
    if (
      form.input_ids.length + files.length > 20 ||
      files.some((f) => f.size > 50 * 1024 * 1024) ||
      files.reduce(
        (n, f) => n + f.size,
        form.inputs.reduce((n, f) => n + (f.size || 0), 0),
      ) >
        50 * 1024 * 1024
    ) {
      setUploads([
        { name: '文件检查', state: 'error', error: '每源最多 20 个文件，单文件与合计不得超过 50 MiB' },
      ]);
      return;
    }
    uploadLock.current = true;
    try {
      for (const value of files) {
        if (!/\.(csv|xlsx)$/i.test(value.name)) {
          setUploads((u) => [...u, { name: value.name, state: 'error', error: '仅支持 CSV / XLSX' }]);
          continue;
        }
        const key = randomId();
        setUploads((u) => [...u, { key, name: value.name, state: 'uploading' }]);
        try {
          const data = new FormData();
          data.append('file', value);
          const item = await api('/inputs', data);
          if (!mounted.current) break;
          setForm((f) => ({ ...f, input_ids: [...f.input_ids, item.id], inputs: [...f.inputs, item] }));
          setDirty(true);
          setPreview(null);
          setGraphCheck(null);
          revision.current += 1;
          setUploads((u) => u.filter((f) => f.key !== key));
        } catch (e) {
          if (mounted.current)
            setUploads((u) => u.map((f) => (f.key === key ? { ...f, state: 'error', error: e.message } : f)));
        }
      }
    } finally {
      // The editor's buttons read this lock; an unexpected error must not leave them disabled.
      uploadLock.current = false;
    }
  }
  async function save(makePreview = false) {
    if (!form.name.trim()) throw new Error('请填写数据源名称');
    if (
      (form.mode === 'dataset' || (form.mode === 'connection' && graph && form.import_data)) &&
      !form.input_ids.length
    )
      throw new Error('请至少上传一份数据表');
    revision.current += 1;
    testController.current?.abort();
    const result = await api(
      form.id ? `/data-sources/${form.id}` : '/data-sources',
      sourcePayload(form),
      form.id ? 'PUT' : 'POST',
    );
    const saved = await api(`/data-sources/${result.id}`);
    setForm(sourceFromRecord(saved));
    setGraphCheck(null);
    setDirty(false);
    onDirtyRef.current?.(false);
    setUploads((items) => items.filter((item) => item.state !== 'error'));
    await onSaved(saved, !makePreview);
    if (makePreview) {
      const data = await api('/data-source-previews', {
        data_source_id: saved.id,
        package_ref: packageReference(packageRef),
      });
      setPreview(data);
      if (graph && form.import_data) await inspectGraph(saved.id);
    }
  }
  async function inspectGraph(sourceId) {
    if (sourceId === undefined && (dirty || !form.id)) throw new Error('请先保存当前数据源配置，再检查旧图数据');
    const version = revision.current;
    const check = await api('/graph-import-checks', {
      data_source_id: sourceId || form.id,
      package_ref: packageReference(packageRef),
    });
    if (mounted.current && version === revision.current) setGraphCheck(check);
  }
  const configField = (key, label, options = {}) => (
    <Field label={label} hint={options.hint}>
      <input
        value={form.connection[key] || ''}
        onChange={(e) => connection(key, e.target.value)}
        autoComplete="off"
        {...options.input}
      />
    </Field>
  );
  return (
    <>
      <Breadcrumb
        parent={parentLabel}
        current={form.id ? '编辑数据源配置' : '新建数据源配置'}
        onBack={close}
        disabled={busy || uploadLock.current}
      />
      <Heading title={form.id ? '编辑数据源配置' : '新建数据源配置'}>
        配置连接与数据文件，保存后可在新建评测中复用。
      </Heading>
      <section className="panel definition-editor">
        {error && <Alert tone="red">{error}</Alert>}
        <MappingConflicts details={details} />
        <div className="segmented modes">
          {[
            ['none', 'box', '不使用数据源'],
            ['dataset', 'upload', '上传数据'],
            ['connection', 'link', '外部连接'],
          ].map(([mode, name, title]) => (
            <button
              key={mode}
              type="button"
              aria-pressed={form.mode === mode}
              className={form.mode === mode ? 'selected' : ''}
              disabled={uploadLock.current}
              onClick={() => change({ mode })}
            >
              <Icon name={name} />
              {title}
            </button>
          ))}
        </div>
        <div className="form-grid">
          <Field label="配置名称 *">
            <input maxLength={128} value={form.name} onChange={(e) => change({ name: e.target.value })} />
          </Field>
          {form.mode === 'connection' && (
            <Field label="Provider *">
              <select value={form.provider} onChange={(e) => provider(e.target.value)}>
                <option value="nl2sql_http">NL2SQL HTTP</option>
                <option value="nl2sql_template">NL2SQL Template</option>
                <option value="neo4j">Neo4j</option>
              </select>
            </Field>
          )}
        </div>
        {form.mode === 'none' && (
          <Alert title="仅使用知识包作答">验证结构与语义，不查询数据，也不生成数据映射。</Alert>
        )}
        {form.mode === 'connection' && (
          <>
            <div className="form-grid">
              {graph ? (
                <>
                  {configField('uri', 'Neo4j URL *', {
                    input: { placeholder: 'bolt://service.example.net:7687' },
                  })}
                  {configField('database', 'Database')}
                  {configField('username', 'User *')}
                  {configField('password', 'Password *', {
                    input: {
                      type: 'password',
                      placeholder: form.secret_configured ? '已保存；留空沿用' : '请输入密码',
                    },
                  })}
                </>
              ) : (
                <>
                  {configField('url', '数据服务 URL *', {
                    input: { placeholder: 'https://service.example.net' },
                  })}
                  {configField('scene', 'Scene / 数据库 *')}
                  {configField('token', 'Token', {
                    input: {
                      type: 'password',
                      placeholder: form.secret_configured ? '已配置；留空沿用' : '请输入访问凭据',
                    },
                  })}
                </>
              )}
            </div>
            {!graph && <ScopeNote connection={form.connection} />}
            {graph ? (
              <>
                <ScopeNote connection={form.connection} graph />
                <div className="section-title">
                  <h3>评测前导入图数据</h3>
                </div>
                <div className="graph-modes">
                  <button
                    type="button"
                    className={!form.import_data ? 'selected' : ''}
                    onClick={() => change({ import_data: false })}
                  >
                    不导入<small>查询图库现有数据</small>
                  </button>
                  <button type="button" disabled title="独立服务尚无可用示例数据种子">
                    示例数据<small>未接入示例数据</small>
                  </button>
                  <button
                    type="button"
                    className={form.import_data ? 'selected' : ''}
                    onClick={() => change({ import_data: true })}
                  >
                    业务数据表<small>上传文件后预览</small>
                  </button>
                </div>
                {form.import_data && (
                  <Alert title="评测前导入图数据">
                    导入写入隔离范围内 Workspace 与 Ontology 两个条件同时匹配的数据。发现旧数据时，开始评测前需要确认删除后再导入。
                  </Alert>
                )}
                {form.import_data && packageRef && (
                  <>
                    <Button
                      busy={busy}
                      disabled={dirty || !form.id || !form.connection.workspace_value || uploadLock.current}
                      onClick={() => act(() => inspectGraph())}
                    >
                      检查旧图数据
                    </Button>
                    <GraphScopeStatus check={graphCheck} />
                  </>
                )}
                {form.import_data && !packageRef && (
                  <p className="help-text">在新建评测中选定知识包后，检查此范围的旧数据并确认替换。</p>
                )}
                {!form.import_data && (
                  <p className="help-text">当前仅查询现有图库，查询限定在隔离范围内。</p>
                )}
              </>
            ) : (
              <>
                <div className="section-title">
                  <h3>连接测试</h3>
                  <Button icon="link" busy={testing} disabled={busy} onClick={testConnection}>
                    测试连接
                  </Button>
                </div>
                {testError && <Alert tone="red" title="连接测试失败">{testError}</Alert>}
                {testResult && <Alert tone="green">{testResult.message}</Alert>}
                <p className="help-text">测试当前连接的连通性。评测沿用所选知识包中的对象、表与字段映射。</p>
              </>
            )}
          </>
        )}
        {(form.mode === 'dataset' || (form.mode === 'connection' && graph && form.import_data)) && (
          <>
            <button
              type="button"
              className="upload-zone"
              onClick={() => file.current.click()}
              disabled={uploadLock.current}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                upload([...e.dataTransfer.files]);
              }}
            >
              <Icon name="upload" />
              <strong>拖拽文件到此处，或点击选择文件</strong>
              <small>CSV / XLSX · 最多 20 个文件 · 单文件及合计不超过 50 MiB</small>
            </button>
            <input
              type="file"
              hidden
              multiple
              ref={file}
              accept=".csv,.xlsx"
              onChange={(e) => {
                const files = [...e.target.files];
                e.target.value = '';
                upload(files);
              }}
            />
            <div className="upload-list">
              {form.inputs.map((f) => (
                <div key={f.id}>
                  <Icon name="set" />
                  <span>{f.name}</span>
                  <small>{(f.size / 1024).toFixed(1)} KB · 已上传</small>
                  <Button
                    icon="close"
                    variant="icon-only ghost"
                    aria-label={`移除 ${f.name}`}
                    disabled={uploadLock.current}
                    onClick={() =>
                      change({
                        inputs: form.inputs.filter((item) => item.id !== f.id),
                        input_ids: form.input_ids.filter((id) => id !== f.id),
                      })
                    }
                  />
                </div>
              ))}
              {uploads.map((u, i) => (
                <div key={u.key || i} className={u.state === 'error' ? 'danger' : ''}>
                  <Icon name={u.state === 'error' ? 'info' : 'refresh'} />
                  <span>{u.name}</span>
                  <small>{u.error || '上传中…'}</small>
                  <Button variant="text" onClick={() => file.current.click()} disabled={u.state !== 'error'}>
                    {u.state === 'error' ? '重新选择' : ''}
                  </Button>
                </div>
              ))}
            </div>
            {form.inputs.length > 0 && (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>识别到的表</th>
                      <th>字段</th>
                    </tr>
                  </thead>
                  <tbody>
                    {form.inputs.flatMap((f) =>
                      Object.entries(f.tables || {}).map(([name, columns]) => (
                        <tr key={`${f.id}-${name}`}>
                          <td>{name}</td>
                          <td>{columns.join('、')}</td>
                        </tr>
                      )),
                    )}
                  </tbody>
                </table>
              </div>
            )}
            {graph && (
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={form.import_data}
                  onChange={(e) => change({ import_data: e.target.checked })}
                />
                允许本轮将所选数据写入以上 Workspace 与 Ontology 范围
              </label>
            )}
            <p className="help-text">
              {graph
                ? '保存配置与预览不写入数据库。发起评测时先检查旧图数据，确认替换后才删除并导入新数据。'
                : '保存配置与预览不写入数据库；发起评测后才执行导入。'}
            </p>
            {packageRef && (
              <Button
                busy={busy}
                disabled={!form.input_ids.length || uploadLock.current}
                onClick={() => act(() => save(true))}
              >
                保存并预览{graph ? '图导入' : '映射'}
              </Button>
            )}
            {!packageRef && <p className="help-text">在新建评测中选择知识包版本后可预览映射。</p>}
            <MappingPreview preview={preview} />
          </>
        )}
        <div className="editor-save">
          <span className="muted">{dirty ? '配置修改尚未保存' : form.id ? '已保存配置' : '配置尚未保存'}</span>
          <div className="actions">
            <Button onClick={close} disabled={busy || uploadLock.current}>
              取消
            </Button>
            <Button
              variant="primary"
              busy={busy}
              disabled={uploads.some((u) => u.state === 'uploading')}
              onClick={() => act(() => save())}
            >
              保存并继续
            </Button>
          </div>
        </div>
      </section>
      {discard && (
        <Confirm
          title="放弃未保存的数据源配置？"
          label="放弃修改"
          onClose={() => setDiscard(false)}
          onConfirm={async () => {
            onDirtyRef.current?.(false);
            onClose();
          }}
        >
          <p>当前配置修改尚未保存，关闭后将丢失这些修改。</p>
        </Confirm>
      )}
    </>
  );
}
