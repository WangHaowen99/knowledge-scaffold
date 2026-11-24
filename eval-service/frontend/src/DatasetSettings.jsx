import React, { useEffect, useState } from 'react';
import { api } from './api.js';
import { datasetFieldErrors } from './domain.js';
import { Alert, Badge, Button, Field } from './ui.jsx';

// Shared storage belongs to the administrator; regular users see readiness only.
export function DatasetSettings({ compact = false }) {
  const [value, setValue] = useState(null), [error, setError] = useState(''),
    [message, setMessage] = useState(''), [busy, setBusy] = useState(false),
    [expanded, setExpanded] = useState(false),
    [fieldErrors, setFieldErrors] = useState({}),
    [form, setForm] = useState({ host: '', port: 3306, user: '', password: '', query_url: '' });
  function accept(next) {
    setValue(next);
    setForm({ host: next.host || '', port: next.port || 3306, user: next.user || '', password: '', query_url: next.query_auto ? '' : next.query_url || '' });
  }
  async function load() {
    setError('');
    setFieldErrors({}); setMessage('');
    try {
      const next = await api('/dataset-settings');
      accept(next);
      setExpanded(!next.configured);
    } catch (e) { setError(e.message); }
  }
  useEffect(() => { load(); }, []);
  async function perform(test) {
    const invalid = datasetFieldErrors(form, value);
    setFieldErrors(invalid);
    if (Object.keys(invalid).length) {
      setError(Object.values(invalid).join('；'));
      setMessage('');
      return;
    }
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await api(test ? '/dataset-settings/test' : '/dataset-settings', { ...form, port: Number(form.port) }, test ? 'POST' : 'PUT');
      if (test) setMessage(result.message);
      else { accept(result); setMessage('已保存，立即用于新评测。此前因未配置而中断的任务可以恢复；已绑定数据库的任务继续使用原连接。'); }
    } catch (e) {
      setError(e.message);
      setFieldErrors(Object.fromEntries((e.details?.fields || []).map(v => [v.field, v.message])));
    } finally { setBusy(false); }
  }
  const field = (key) => ({ value: form[key], disabled: busy, onChange: (e) => {
    setForm({ ...form, [key]: e.target.value }); setMessage(''); setError('');
    setFieldErrors(previous => ({ ...previous, [key]: undefined }));
  } });
  if (!value) return <Alert tone={error ? 'amber' : 'blue'} action={error && <Button onClick={load}>重试</Button>}>{error || '正在检查上传数据存储…'}</Alert>;
  if (compact) return <Alert tone={value.configured ? 'blue' : 'amber'} title={value.configured ? '上传数据存储已配置' : '上传数据暂不能执行评测'}>
    {value.configured ? '保存文件不会写入 MySQL；开始评测后才导入独立评测库。' : value.message}
    {!value.configured && <a className="text-link" href="#/data-sources">查看上传数据存储设置</a>}
  </Alert>;
  return <section className="panel dataset-settings">
    <div className="dataset-settings-heading">
      <div><h2>上传数据存储</h2><p>为上传的 Excel / CSV 配置评测数据库。</p></div>
      <div className="dataset-settings-status">
        <Badge tone={value.configured ? 'green' : 'amber'}>{value.configured ? '已配置' : '待配置'}</Badge>
        {value.editable && <Button aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起设置' : '配置存储'}</Button>}
      </div>
    </div>
    {!value.configured && <p className="dataset-settings-hint">文件可以先上传保存，完成配置后即可开始评测。</p>}
    {value.editable && expanded ? <form noValidate onSubmit={(e) => { e.preventDefault(); perform(false); }}>
      {error && <Alert tone="red" title={Object.values(fieldErrors).some(Boolean) ? '请补齐或修正配置' : '操作未完成'}>{error}</Alert>}
      {message && <Alert tone="green">{message}</Alert>}
      <div className="dataset-settings-section">
        <div className="dataset-settings-caption"><h3>数据库连接</h3><p>使用已部署的 MySQL，评测时自动创建独立数据库。</p></div>
        <div className="dataset-settings-fields">
          <Field label="MySQL 主机 *" error={fieldErrors.host}><input {...field('host')} required placeholder="主机名或 IP，不含协议" /></Field>
          <Field label="端口 *" error={fieldErrors.port}><input {...field('port')} type="number" min="1" max="65535" required /></Field>
          <Field label="用户名 *" error={fieldErrors.user}><input {...field('user')} required autoComplete="off" placeholder="评测数据库账号" /></Field>
          <Field label="密码" error={fieldErrors.password} hint={value.password_set ? '密码已安全保存，无需重新填写；留空保留。' : undefined}><input {...field('password')} type="password" autoComplete="new-password" placeholder={value.password_set ? '已配置，连接不变时留空保留' : '请输入密码'} /></Field>
        </div>
      </div>
      <details className="dataset-settings-help dataset-settings-advanced" open={fieldErrors.query_url ? true : undefined}>
        <summary>高级设置{form.query_url ? ' · 已自定义查询入口' : ' · 查询入口自动配置'}</summary>
        <p>标准部署无需填写。仅当 Consumer 与评测服务跨机器部署或使用自定义网络时覆盖此地址。</p>
        <Field label="自定义查询入口" error={fieldErrors.query_url} hint="留空使用部署默认地址；自定义地址须以 /evaluation/api/nl2sql 结尾。">
          <input {...field('query_url')} type="url" placeholder="留空，使用内置查询入口" />
        </Field>
        {!form.query_url && <p>当前默认：<code>{value.query_url}</code></p>}
      </details>
      <div className="dataset-settings-footer">
        <details className="dataset-settings-help"><summary>权限与生效范围</summary>
          <p>仅管理员可配置。账号须具有创建评测库、建表、写入、查询和清理权限；测试会创建临时库并在结束后删除。查询使用默认判分模型，无需另部署 NL2SQL。</p>
          <p>保存立即用于新评测，已绑定数据库的任务继续使用原连接。密码留空仅在主机、端口、账号未变时沿用。MySQL 测试不验证 Consumer 到查询入口的网络。</p>
          <p>{value.configured ? `当前来源：${value.source === 'settings' ? '页面配置' : '部署配置'}` : '尚无完整配置'}</p>
        </details>
        <div className="row-actions">
          <Button disabled={busy} onClick={() => perform(true)}>测试连接</Button>
          <Button type="submit" variant="primary" busy={busy}>保存配置</Button>
        </div>
      </div>
    </form> : !value.editable && <p className="muted">请联系管理员维护上传数据存储。</p>}
  </section>;
}
