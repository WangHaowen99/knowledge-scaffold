import React, { useRef, useState } from 'react';
import { api } from './api.js';
import { parseCases } from './domain.js';
import { Alert, Badge, Breadcrumb, Button, Confirm, Field, Heading, Modal, useAction } from './ui.jsx';

export function CaseEditor({
  record,
  onSaved,
  onDirty = () => {},
  onDiscard = () => {},
  onClose,
  embedded = false,
}) {
  const [form, setForm] = useState(record || { name: '', cases: [{ question: '', reference: '' }] });
  const [imported, setImported] = useState(null),
    [saveAs, setSaveAs] = useState(false),
    [copyName, setCopyName] = useState('');
  const [invalid, setInvalid] = useState(null),
    [dirty, setDirty] = useState(!record?.id),
    [reload, setReload] = useState(false);
  const { busy, error, setError, act } = useAction(),
    file = useRef(null),
    rows = useRef(new Map());
  function change(next) {
    setForm(next);
    setDirty(true);
    onDirty(true);
  }
  function update(index, key, value) {
    change({ ...form, cases: form.cases.map((c, i) => (i === index ? { ...c, [key]: value } : c)) });
  }
  async function save(asNew = false) {
    const bad = form.cases.findIndex(
      (c) => !c.question.trim() || c.question.length > 10000 || (c.reference || '').length > 20000,
    );
    if (bad >= 0) {
      setInvalid(bad);
      rows.current.get(bad)?.focus();
      throw new Error(`请修正用例 ${bad + 1} 的问题或内容长度`);
    }
    const name = asNew ? copyName.trim() : form.name.trim();
    if (!name || !form.cases.length || form.cases.length > 500)
      throw new Error('请填写名称并保留 1-500 条用例');
    const payload = {
      name,
      cases: form.cases.map(({ question, reference, execution_mode }) => ({
        question: question.trim(),
        reference: reference || '',
        ...(execution_mode && execution_mode !== 'auto' ? { execution_mode } : {}),
      })),
      ...(!asNew && form.id ? { revision: form.revision } : {}),
    };
    const saved = await api(
      asNew || !form.id ? '/eval-sets' : `/eval-sets/${form.id}`,
      payload,
      asNew || !form.id ? 'POST' : 'PUT',
    );
    const result = await api(`/eval-sets/${saved.id}`);
    setForm(result);
    setDirty(false);
    setSaveAs(false);
    onDirty(false);
    await onSaved(result);
  }
  function template() {
    const raw = JSON.stringify([{ question: '合成示例：1 加 1 等于几？', reference: '2' }], null, 2);
    const url = URL.createObjectURL(new Blob([raw], { type: 'application/json' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'eval-set-template.json';
    anchor.click();
    URL.revokeObjectURL(url);
  }
  const body = (
    <div className="case-editor">
      {error && (
        <Alert
          tone="red"
          title="保存或导入未完成"
          action={
            form.id && error.includes('变更') ? (
              <Button onClick={() => setReload(true)}>重新加载</Button>
            ) : undefined
          }
        >
          {error}
          {error.includes('变更') && '。当前输入已保留，可另存为新评测集。'}
        </Alert>
      )}
      <Field label="评测集名称 *">
        <input
          value={form.name}
          maxLength={128}
          onChange={(e) => change({ ...form, name: e.target.value })}
          placeholder="为这组用例命名"
        />
      </Field>
      <div className="section-caption">
        <p>问题必填，参考答案可选。无参考答案的用例在执行后等待人工判定。</p>
        <Badge>{form.cases.length} / 500 条</Badge>
      </div>
      <div className="case-edit-list">
        {form.cases.map((item, index) => (
          <article className={`case-edit-card ${invalid === index ? 'invalid' : ''}`} key={index}>
            <header>
              <strong>用例 {String(index + 1).padStart(2, '0')}</strong>
              <div className="actions">
                {!item.reference?.trim() && <Badge>无参考答案 · 待人工</Badge>}
                <Button
                  variant="text"
                  disabled={form.cases.length >= 500}
                  onClick={() =>
                    change({
                      ...form,
                      cases: [...form.cases.slice(0, index + 1), { ...item }, ...form.cases.slice(index + 1)],
                    })
                  }
                >
                  复制
                </Button>
                <Button
                  variant="text danger"
                  onClick={() => change({ ...form, cases: form.cases.filter((_, i) => i !== index) })}
                >
                  删除
                </Button>
              </div>
            </header>
            <div className="form-grid">
              <Field label="问题 *" error={invalid === index && !item.question.trim() ? '请填写问题' : ''}>
                <textarea
                  ref={(el) => {
                    if (el) rows.current.set(index, el);
                    else rows.current.delete(index);
                  }}
                  value={item.question}
                  maxLength={10000}
                  rows={2}
                  onChange={(e) => update(index, 'question', e.target.value)}
                />
              </Field>
              <Field label="参考答案（可选）">
                <textarea
                  value={item.reference || ''}
                  maxLength={20000}
                  rows={2}
                  onChange={(e) => update(index, 'reference', e.target.value)}
                  placeholder="留空时，执行后由人工判定"
                />
              </Field>
            </div>
            <Field label="执行要求" hint="自动识别覆盖明确查数表述；复杂或歧义题请明确选择。新任务会冻结该要求，不改原题或历史结果。">
              <select value={item.execution_mode || 'auto'} onChange={(e) => update(index, 'execution_mode', e.target.value)}>
                <option value="auto">自动识别</option>
                <option value="business_query">必须查询业务数据</option>
                <option value="reasoning_only">独立假设 / 纯规则推理</option>
              </select>
            </Field>
          </article>
        ))}
      </div>
      <div className="editor-tools">
        <Button
          icon="plus"
          disabled={form.cases.length >= 500}
          onClick={() => change({ ...form, cases: [...form.cases, { question: '', reference: '' }] })}
        >
          添加用例
        </Button>
        <Button icon="upload" onClick={() => file.current.click()}>
          导入 JSON
        </Button>
        <Button variant="text" onClick={template}>
          下载模板
        </Button>
        {form.id && (
          <Button
            variant="text"
            onClick={() => {
              setCopyName(`${form.name} 副本`);
              setSaveAs(true);
            }}
          >
            另存为新评测集
          </Button>
        )}
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
                setImported(parseCases(await value.text()));
              });
          }}
        />
      </div>
      <div className="editor-save">
        <span className="muted">
          {dirty ? '修改尚未保存；已有任务保留原用例快照。' : '评测集已保存，可继续下一步。'}
        </span>
        <Button busy={busy} variant="primary" onClick={() => act(() => save())}>
          {form.id ? '保存修改' : '保存评测集'}
        </Button>
      </div>
      {imported && (
        <Confirm
          title="确认导入评测用例"
          label="替换当前用例"
          onClose={() => setImported(null)}
          onConfirm={async () => {
            change({ ...form, cases: imported });
            setInvalid(null);
          }}
        >
          <p>
            已识别 {imported.length} 条用例，其中 {imported.filter((c) => !c.reference).length}{' '}
            条无参考答案。确认后替换编辑区的用例，保存后生效。
          </p>
          <div className="import-example">
            <strong>第一条预览</strong>
            <p>{imported[0].question}</p>
          </div>
        </Confirm>
      )}
      {saveAs && (
        <Modal
          title="另存为新评测集"
          onClose={() => setSaveAs(false)}
          footer={
            <>
              <Button onClick={() => setSaveAs(false)}>取消</Button>
              <Button busy={busy} variant="primary" onClick={() => act(() => save(true))}>
                保存为新评测集
              </Button>
            </>
          }
        >
          <Field label="新名称 *">
            <input value={copyName} maxLength={128} onChange={(e) => setCopyName(e.target.value)} />
          </Field>
          {error && <Alert tone="red">{error}</Alert>}
        </Modal>
      )}
      {reload && (
        <Confirm
          title="重新加载最新评测集？"
          label="加载并替换编辑内容"
          onClose={() => setReload(false)}
          onConfirm={async () => {
            const item = await api(`/eval-sets/${form.id}`);
            setForm(item);
            setDirty(false);
            onDirty(false);
            setError('');
            setInvalid(null);
          }}
        >
          <p>当前未保存的修改会被最新版本替换；如需保留，请先另存为新评测集。</p>
        </Confirm>
      )}
    </div>
  );
  const [discard, setDiscard] = useState(false);
  function close() {
    if (dirty) setDiscard(true);
    else onClose?.();
  }
  return (
    <>
      {embedded ? (
        body
      ) : (
        <>
          <Breadcrumb
            parent="评测集"
            current={form.id ? '编辑评测集' : '新建评测集'}
            onBack={close}
            disabled={busy}
          />
          <Heading title={form.id ? '编辑评测集' : '新建评测集'}>
            管理问题与参考答案，保存后可在新建评测中复用。
          </Heading>
          <section className="panel definition-editor">{body}</section>
        </>
      )}
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
