import React, { useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import { scopeErrors, scopeOf } from './domain.js';
import { Alert, Badge, Button, Field, Skeleton, useAction } from './ui.jsx';

const STATUS = {
  ready: ['green', '已保存'],
  dirty: ['amber', '修改未保存'],
  missing: ['amber', '待填写'],
};

// 新建评测中选中外部连接数据源后显示的隔离范围：Neo4j 四项（Ontology 对应值由知识包带入），NL2SQL 一项。
// 保存到该数据源（PUT /data-sources/{id}/scope），之后选择它的评测沿用；onStatus 告诉向导能否进入下一步。
export function SourceScope({ sourceId, revision, packageRef, onSaved, onStatus }) {
  const [record, setRecord] = useState(null),
    [values, setValues] = useState(null),
    [loadError, setLoadError] = useState(''),
    [showErrors, setShowErrors] = useState(false);
  const { busy, error, act } = useAction();
  const node = useRef(null);
  useEffect(() => {
    let live = true;
    setLoadError('');
    api(`/data-sources/${sourceId}`)
      .then((item) => {
        if (!live) return;
        setRecord(item);
        setValues(scopeOf(item));
        setShowErrors(false);
      })
      .catch((e) => live && setLoadError(e.message));
    return () => {
      live = false;
    };
  }, [sourceId, revision]);
  const graph = record?.provider === 'neo4j';
  const saved = record ? scopeOf(record) : null;
  const errors = values ? scopeErrors(record, values) : {};
  const dirty = !!values && Object.keys(values).some((key) => values[key].trim() !== saved[key]);
  const status = !record ? 'loading' : dirty ? 'dirty' : Object.keys(errors).length ? 'missing' : 'ready';
  useEffect(() => {
    onStatus(loadError ? 'error' : status);
  }, [status, loadError]);
  const shown = !!record && !!values;
  useEffect(() => {
    // 选中数据源后展开的隔离范围整块露出来：靠近页面底部时滚到操作栏上方，已经完整可见时不滚动
    if (shown) node.current?.scrollIntoView({ block: 'nearest' });
  }, [shown]);
  async function save() {
    setShowErrors(true);
    if (Object.keys(errors).length) throw new Error('请先修正隔离范围中的问题');
    const body = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, value.trim()]));
    const result = await api(`/data-sources/${sourceId}/scope`, { revision: record.revision, ...body }, 'PUT');
    setRecord(result);
    setValues(scopeOf(result));
    setShowErrors(false);
    onSaved(result);
  }
  if (loadError) return <div className="source-scope"><Alert tone="red">读取数据源配置失败：{loadError}</Alert></div>;
  if (!record || !values) return <div className="source-scope"><Skeleton /></div>;
  const [tone, label] = STATUS[status];
  const field = (key, title, hint, input = {}) => (
    <Field label={title} hint={hint} error={(showErrors || values[key]) && errors[key]}>
      <input
        value={values[key]}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
        onKeyDown={(e) => e.key === 'Enter' && dirty && act(save)}
        {...input}
      />
    </Field>
  );
  return (
    <div className="source-scope" aria-label="评测隔离范围" ref={node}>
      <div className="source-scope-head">
        <div>
          <strong>评测隔离范围</strong>
          <small>
            {graph
              ? 'Workspace 与 Ontology 两组条件同时匹配，限定本数据源查询与导入的图数据'
              : '评测在同名的 consumer 工作空间中执行，空间名称与 ID 一致'}
          </small>
        </div>
        <Badge tone={tone}>{label}</Badge>
      </div>
      {error && <Alert tone="red">{error}</Alert>}
      <div className="form-grid">
        {graph ? (
          <>
            {field('workspace_property', 'Workspace 隔离字段 *', '图数据中标记评测空间的属性名')}
            {field('workspace_value', 'Workspace 对应值 *', '也是评测所用 consumer 工作空间的 ID', {
              placeholder: '如 eval_workspace',
            })}
            {field(
              'ontology_property',
              `Ontology 隔离字段${record.import_data ? ' *' : '（可选）'}`,
              '图数据中标记知识包的属性名',
            )}
            <Field label="Ontology 对应值" hint="由本轮选择的知识包自动带入">
              <input readOnly value={packageRef?.ontology_id || ''} placeholder="选择知识包后自动带入" />
            </Field>
          </>
        ) : (
          field('evaluation_workspace_id', '评测工作空间 ID *', '小写字母开头，最多 64 个字符', {
            placeholder: '如 eval_workspace',
          })
        )}
      </div>
      <div className="source-scope-foot">
        <span>
          {status === 'ready'
            ? '已随数据源保存，之后选择该数据源的评测沿用。'
            : '填写后保存到该数据源，之后选择它的评测沿用。'}
        </span>
        {dirty && (
          <div className="actions">
            <Button variant="text" disabled={busy} onClick={() => setValues(saved)}>
              撤销修改
            </Button>
            <Button variant="primary" busy={busy} onClick={() => act(save)}>
              保存隔离范围
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
