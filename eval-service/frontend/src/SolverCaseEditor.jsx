import React, { useEffect, useRef, useState } from 'react';
import { allPages, api } from './api.js';
import { SOLVER_MAX_CASES, emptySolverCase, parseSolverCases, solverCasePayload, solverCaseRows } from './domain.js';
import { Alert, Badge, Breadcrumb, Button, Confirm, Field, Heading, useAction } from './ui.jsx';

// 参考版本只提供操作下拉和覆盖提示，不随集合保存：用例可以在对应版本发布前写好
function ReferenceVersion({ onOperations }) {
  const [packages, setPackages] = useState([]),
    [versions, setVersions] = useState([]),
    [packageId, setPackageId] = useState(''),
    [releaseId, setReleaseId] = useState(''),
    [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    allPages('/packages')
      .then((items) => live && setPackages(items))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, []);
  useEffect(() => {
    setReleaseId('');
    setVersions([]);
    if (!packageId) return undefined;
    let live = true;
    allPages(`/packages/${encodeURIComponent(packageId)}/releases`)
      .then((items) => live && setVersions(items))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [packageId]);
  useEffect(() => {
    onOperations(null);
    setError('');
    if (!releaseId) return undefined;
    let live = true;
    api(`/packages/${encodeURIComponent(packageId)}/releases/${encodeURIComponent(releaseId)}/solver-operations`)
      .then((value) => live && onOperations(value.operations))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [releaseId]);
  return (
    <>
      {error && <Alert tone="amber" title="参考版本读取失败，不影响保存">{error}</Alert>}
      <div className="form-grid">
        <Field label="参考知识包（可选）" hint="选择后提供求解操作建议，不随验证集保存">
          <select value={packageId} onChange={(e) => setPackageId(e.target.value)}>
            <option value="">不选择</option>
            {packages.map((item) => (
              <option key={item.ontology_id} value={item.ontology_id}>
                {item.name || item.ontology_id}
              </option>
            ))}
          </select>
        </Field>
        <Field label="参考版本（可选）" hint="版本尚未发布时可以不选，发起求解验证时按所选版本核对">
          <select value={releaseId} disabled={!packageId} onChange={(e) => setReleaseId(e.target.value)}>
            <option value="">不选择</option>
            {versions.map((item) => (
              <option key={item.release_id} value={item.release_id}>
                {item.version}
              </option>
            ))}
          </select>
        </Field>
      </div>
    </>
  );
}

// 求解验证集：每条用例点名一个求解操作并给出入参；判定只看求解工具调用与权威结果，不需要参考答案
// embedded 用在新建评测向导里，操作来自向导选定的版本；独立页面不绑定版本，求解操作可手动填写
export function SolverCaseEditor({
  record,
  operations: wizardOperations,
  onSaved,
  onDirty = () => {},
  onDiscard = () => {},
  onClose,
  embedded = false,
}) {
  const [referenceOperations, setReferenceOperations] = useState(null),
    [discard, setDiscard] = useState(false);
  const operations = embedded ? wizardOperations : referenceOperations;
  const [form, setForm] = useState(() => ({
    id: record?.id,
    revision: record?.revision,
    name: record?.name || '',
    rows: record?.cases?.length ? solverCaseRows(record.cases) : [emptySolverCase(0, operations?.[0]?.solver_key)],
  }));
  const [dirty, setDirty] = useState(!record?.id);
  const { busy, error, act } = useAction(),
    file = useRef(null);
  const keys = new Set((operations || []).map((item) => item.solver_key));
  const uncovered = (operations || []).filter((item) => !form.rows.some((row) => row.solver_key === item.solver_key));
  function change(next) {
    setForm(next);
    setDirty(true);
    onDirty(true);
  }
  function update(index, key, value) {
    change({ ...form, rows: form.rows.map((row, i) => (i === index ? { ...row, [key]: value } : row)) });
  }
  async function save() {
    const name = form.name.trim();
    if (!name) throw new Error('请填写求解验证集名称');
    const payload = { name, cases: solverCasePayload(form.rows), ...(form.id ? { revision: form.revision } : {}) };
    const saved = await api(form.id ? `/solver-sets/${form.id}` : '/solver-sets', payload, form.id ? 'PUT' : 'POST');
    const result = await api(`/solver-sets/${saved.id}`);
    setForm({ id: result.id, revision: result.revision, name: result.name, rows: solverCaseRows(result.cases) });
    setDirty(false);
    onDirty(false);
    await onSaved(result);
  }
  const body = (
    <div className="case-editor">
      {error && <Alert tone="red" title="保存或导入未完成">{error}</Alert>}
      <Field label="求解验证集名称 *">
        <input value={form.name} maxLength={128} onChange={(e) => change({ ...form, name: e.target.value })} />
      </Field>
      {!embedded && <ReferenceVersion onOperations={setReferenceOperations} />}
      {!embedded && (
        <datalist id="solver-operation-options">
          {(operations || []).map((item) => (
            <option key={item.solver_key} value={item.solver_key}>
              {item.title}
            </option>
          ))}
        </datalist>
      )}
      <div className="section-caption">
        <p>每个启用的求解操作至少需要一条用例。提问留空时按求解操作和入参自动生成，要求 Agent 原样传入参。</p>
        <Badge>
          {form.rows.length} / {SOLVER_MAX_CASES} 条
        </Badge>
      </div>
      {operations && !operations.length && (
        <Alert tone="amber">所选知识包版本没有启用的求解操作，无需求解验证。</Alert>
      )}
      {uncovered.length > 0 && (
        <Alert tone="amber">以下求解操作还没有用例：{uncovered.map((item) => item.solver_key).join('、')}</Alert>
      )}
      <div className="case-edit-list">
        {form.rows.map((row, index) => (
          <article className="case-edit-card" key={index}>
            <header>
              <strong>用例 {String(index + 1).padStart(2, '0')}</strong>
              <div className="actions">
                {operations && row.solver_key && !keys.has(row.solver_key) && <Badge tone="red">版本中无此求解操作</Badge>}
                <Button
                  variant="text danger"
                  disabled={form.rows.length <= 1}
                  onClick={() => change({ ...form, rows: form.rows.filter((_, i) => i !== index) })}
                >
                  删除
                </Button>
              </div>
            </header>
            <div className="form-grid">
              <Field label="用例标识 *">
                <input value={row.id} maxLength={64} onChange={(e) => update(index, 'id', e.target.value)} />
              </Field>
              <Field label="名称 *">
                <input value={row.name} maxLength={80} onChange={(e) => update(index, 'name', e.target.value)} />
              </Field>
              <Field label="求解操作 *" hint={embedded ? undefined : '填写 solver_key，以建模页「求解模型」面板的契约为准'}>
                {embedded ? (
                  <select value={row.solver_key} onChange={(e) => update(index, 'solver_key', e.target.value)}>
                    <option value="">选择求解操作</option>
                    {[...(operations || []), ...(row.solver_key && !keys.has(row.solver_key) ? [{ solver_key: row.solver_key }] : [])].map(
                      (item) => (
                        <option key={item.solver_key} value={item.solver_key}>
                          {item.title ? `${item.title} · ${item.solver_key}` : item.solver_key}
                        </option>
                      ),
                    )}
                  </select>
                ) : (
                  <input
                    list="solver-operation-options"
                    value={row.solver_key}
                    maxLength={32}
                    onChange={(e) => update(index, 'solver_key', e.target.value.trim())}
                  />
                )}
              </Field>
              <Field label="期望候选数（可选）" hint="留空时有序二元路线按 2×项目数推算，其余不核对候选数">
                <input
                  value={row.expected}
                  inputMode="numeric"
                  onChange={(e) => update(index, 'expected', e.target.value.replace(/[^0-9]/g, ''))}
                />
              </Field>
              <Field label="入参（JSON 对象）*">
                <textarea rows={6} value={row.argumentsText} onChange={(e) => update(index, 'argumentsText', e.target.value)} />
              </Field>
              <Field label="提问（可选）">
                <textarea
                  rows={6}
                  maxLength={4000}
                  value={row.question}
                  placeholder="留空时自动生成提问"
                  onChange={(e) => update(index, 'question', e.target.value)}
                />
              </Field>
            </div>
          </article>
        ))}
      </div>
      <div className="editor-tools">
        <Button
          icon="plus"
          disabled={form.rows.length >= SOLVER_MAX_CASES}
          onClick={() =>
            change({ ...form, rows: [...form.rows, emptySolverCase(form.rows.length, uncovered[0]?.solver_key)] })
          }
        >
          添加用例
        </Button>
        <Button icon="upload" onClick={() => file.current.click()}>
          导入 JSON
        </Button>
        <input
          ref={file}
          hidden
          type="file"
          accept=".json,application/json"
          onChange={(e) => {
            const value = e.target.files[0];
            e.target.value = '';
            if (value)
              act(async () => {
                if (value.size > 1024 * 1024) throw new Error('JSON 文件不得超过 1 MiB');
                change({ ...form, rows: parseSolverCases(await value.text()) });
              });
          }}
        />
      </div>
      <div className="editor-save">
        <span className="muted">{dirty ? '修改尚未保存；已有任务保留原用例快照。' : '求解验证集已保存，可继续下一步。'}</span>
        <Button busy={busy} variant="primary" onClick={() => act(save)}>
          {form.id ? '保存修改' : '保存求解验证集'}
        </Button>
      </div>
    </div>
  );
  if (embedded) return body;
  function close() {
    if (dirty) setDiscard(true);
    else onClose?.();
  }
  const title = form.id ? '编辑求解验证集' : '新建求解验证集';
  return (
    <>
      <Breadcrumb parent="评测集" current={title} onBack={close} disabled={busy} />
      <Heading title={title}>用例可以在对应版本发布前写好并保存，发布后发起求解验证时按所选版本核对。</Heading>
      <section className="panel definition-editor">{body}</section>
      {discard && (
        <Confirm
          title="放弃未保存的修改？"
          label="放弃修改"
          onClose={() => setDiscard(false)}
          onConfirm={async () => {
            onDiscard();
            onClose?.();
          }}
        >
          <p>当前编辑尚未保存，关闭后将丢失这些修改。</p>
        </Confirm>
      )}
    </>
  );
}
