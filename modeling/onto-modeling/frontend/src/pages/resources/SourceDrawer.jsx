// 接入数据源 / 修改连接（ADR-0103，契约 DS-01～DS-04）：抽屉里按连接器的字段（SourceField）生成表单，
// 测试连接（DS-04）、保存（接入 DS-02 / 修改 DS-03）。密钥字段只写不读：修改时空着就是不改。
import { useEffect, useMemo, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Drawer, Field, Icon, Inp, Note, Select } from '../../ui/index.jsx';
import {
  FIELD_ERROR_TEXT, connectorOptions, fieldErrorsFrom, initialValues, secretPlaceholder, splitValues, testView, validateSource,
} from './sourceModel.js';

function SourceField({ f, value, error, savedSecrets, editing, onChange }) {
  const err = error ? FIELD_ERROR_TEXT[error] || FIELD_ERROR_TEXT.invalid : null;
  let input;
  if (f.type === 'select') {
    input = <Select value={value} options={(f.options || []).map((o) => ({ value: String(o.value), label: o.label }))} onChange={onChange} placeholder={f.placeholder || ''} />;
  } else {
    const secret = f.type === 'secret';
    input = (
      <Inp
        type={secret ? 'password' : f.type === 'port' || f.type === 'number' ? 'number' : 'text'}
        value={value}
        error={!!err}
        autoComplete={secret ? 'new-password' : 'off'}
        placeholder={secret && editing ? secretPlaceholder(f, savedSecrets) : f.placeholder || ''}
        mono={f.type === 'host' || f.type === 'port'}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <Field label={f.label} req={f.required && !(editing && f.type === 'secret')} hint={err ? <span style={{ color: 'var(--red)' }}>{err}</span> : null}>
      {input}
    </Field>
  );
}

function TestResult({ view }) {
  if (!view) return null;
  return (
    <Note tone={view.ok ? 'teal' : 'red'} icon={view.ok ? 'check' : 'alert'} className="mt12">
      <div className="b">{view.title}</div>
      {view.error ? <div className="sm">{view.error}</div> : null}
      {view.checks.length ? (
        <div className="col mt8" style={{ gap: 4 }}>
          {view.checks.map((c) => (
            <div key={c.name} className="row g4 sm" data-check={c.ok ? 'ok' : 'fail'}>
              <Icon name={c.ok ? 'check' : 'x'} size={12} />
              <span>{c.name}</span>
              {c.detail ? <span className="muted ell">{c.detail}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </Note>
  );
}

/**
 * open：{connectors}（接入）或 {edit: editTarget(m)}（修改连接）；null 关着。
 * onSaved(material)：接入或修改成功后由页面刷新列表、选中这份资料。
 */
export function SourceDrawer({ open, onClose, onSaved }) {
  const { api } = useApp();
  const { pid } = useProject();
  const edit = open?.edit || null;
  const connectors = open?.connectors || [];
  const [name, setName] = useState('');
  const [connectorId, setConnectorId] = useState('');
  const [values, setValues] = useState({});
  const [errors, setErrors] = useState({});
  const [failure, setFailure] = useState(null);
  const [tested, setTested] = useState(null);
  const [busy, setBusy] = useState('');

  const connector = edit ? edit.connector : connectors.find((c) => c.id === connectorId) || null;
  const fields = useMemo(() => (edit ? edit.fields : connector?.fields || []), [edit, connector]);

  // 打开时重置；接入默认选第一个连接器
  useEffect(() => {
    if (!open) return;
    setErrors({});
    setFailure(null);
    setTested(null);
    setBusy('');
    if (open.edit) {
      setName(open.edit.name);
      setConnectorId(open.edit.connector?.id || '');
      setValues(open.edit.values);
    } else {
      const first = (open.connectors || [])[0];
      setName('');
      setConnectorId(first?.id || '');
      setValues(initialValues(first?.fields || []));
    }
  }, [open]);

  const pickConnector = (id) => {
    const c = connectors.find((x) => x.id === id);
    setConnectorId(id);
    setValues(initialValues(c?.fields || []));
    setErrors({});
    setTested(null);
  };
  const setValue = (key, v) => {
    setValues((cur) => ({ ...cur, [key]: v }));
    setErrors((cur) => (cur[key] ? { ...cur, [key]: undefined } : cur));
    setTested(null);
  };

  const check = () => {
    const e = validateSource({ name, fields, values, editing: !!edit });
    setErrors(e);
    return !Object.keys(e).length;
  };
  const showFailure = (e) => {
    const marks = fieldErrorsFrom(e);
    if (marks) setErrors((cur) => ({ ...cur, ...marks }));
    else setFailure(errorText(e));
  };

  const test = async () => {
    setFailure(null);
    // 测试不看名称；修改时密钥空着就用已存的
    const e = validateSource({ name: 'x', fields, values, editing: !!edit });
    setErrors(e);
    if (Object.keys(e).length) return;
    const { config, secrets } = splitValues(fields, values);
    setBusy('test');
    try {
      const body = { connector: connector.id, config, ...(Object.keys(secrets).length ? { secrets } : {}), ...(edit ? { path: edit.path } : {}) };
      setTested(testView(await api.testSource(pid, body)));
    } catch (err) {
      showFailure(err);
    } finally {
      setBusy('');
    }
  };

  const save = async () => {
    setFailure(null);
    if (!check()) return;
    const { config, secrets } = splitValues(fields, values);
    setBusy('save');
    try {
      const r = edit
        ? await api.updateSource(pid, { path: edit.path, config, ...(Object.keys(secrets).length ? { secrets } : {}) })
        : await api.addSource(pid, { name: name.trim(), connector: connector.id, config, secrets });
      toast(edit ? `已修改连接，重新采集「${edit.name}」` : `已接入「${name.trim()}」，正在采集`, 'check');
      onSaved?.(r.material);
    } catch (err) {
      showFailure(err);
    } finally {
      setBusy('');
    }
  };

  const canTest = !!connector && (!edit || edit.connector?.available !== false);
  return (
    <Drawer
      open={!!open}
      onClose={onClose}
      icon="db"
      w={460}
      title={edit ? '修改连接' : '接入数据源'}
      sub={edit ? edit.name : null}
      foot={(
        <>
          <Button kind="ghost" icon="plug" disabled={!canTest || !!busy} onClick={test}>{busy === 'test' ? '测试中…' : '测试连接'}</Button>
          <span className="sp" />
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={!connector || !!busy} onClick={save}>{busy === 'save' ? '保存中…' : '保存'}</Button>
        </>
      )}
    >
      <div className="col" style={{ gap: 12 }} data-source-form>
        {edit ? null : (
          <Field label="名称" req hint={errors.name ? <span style={{ color: 'var(--red)' }}>{FIELD_ERROR_TEXT[errors.name]}</span> : null}>
            <Inp value={name} error={!!errors.name} onChange={(e) => { setName(e.target.value); setErrors((c) => ({ ...c, name: undefined })); }} />
          </Field>
        )}
        <Field label="连接器">
          {edit
            ? <Inp value={edit.connector?.label || edit.connector?.id || ''} disabled readOnly />
            : <Select value={connectorId} options={connectorOptions(connectors)} onChange={pickConnector} />}
        </Field>
        {fields.map((f) => (
          <SourceField
            key={`${connector?.id || ''}:${f.key}`} f={f} value={values[f.key] ?? ''} error={errors[f.key]}
            savedSecrets={edit?.savedSecrets || []} editing={!!edit} onChange={(v) => setValue(f.key, v)}
          />
        ))}
        {failure ? <Note tone="red" icon="alert">{failure}</Note> : null}
        <TestResult view={tested} />
      </div>
    </Drawer>
  );
}
