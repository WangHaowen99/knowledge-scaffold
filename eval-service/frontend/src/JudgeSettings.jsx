import React, { useEffect, useState } from 'react';
import { api } from './api.js';
import { Alert } from './ui.jsx';

const SOURCES = { settings: '页面配置', deployment: '部署配置' };

// The Judge model is shared by every run on this service, so only administrators can change it.
export function JudgeSettings({ onClose, onChanged }) {
  const [view, setView] = useState(null),
    [form, setForm] = useState({ base_url: '', model: '', api_key: '' });
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [errorTitle, setErrorTitle] = useState('操作未完成');
  function show(value) {
    setView(value);
    setForm({ base_url: value.base_url || '', model: value.model || '', api_key: '' });
  }
  async function act(work, done, failureTitle = '操作未完成') {
    setBusy(true);
    setError('');
    setNotice('');
    setErrorTitle(failureTitle);
    try {
      await work();
      if (done) setNotice(done);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    act(async () => show(await api('/judge-settings')));
  }, []);
  const field = (key) => ({
    value: form[key],
    disabled: busy,
    onChange: (e) => {
      setForm({ ...form, [key]: e.target.value });
      setError('');
      setNotice('');
    },
  });
  const save = () =>
    act(async () => {
      show(await api('/judge-settings', form, 'PUT'));
      await onChanged();
    }, '已保存，新创建的评测轮次将使用该配置');
  const reset = () =>
    act(async () => {
      show(await api('/judge-settings', undefined, 'DELETE'));
      await onChanged();
    }, '已恢复为部署配置');
  return (
    <section className="settings">
      <div className="row">
        <h2>评测判分模型配置</h2>
        {onClose && <button onClick={onClose}>关闭</button>}
      </div>
      {error && (
        <Alert tone="red" title={errorTitle}>
          {error}
        </Alert>
      )}
      {notice && <Alert tone="green">{notice}</Alert>}
      {view && (
        <>
          <p>
            当前生效：{SOURCES[view.source]} · 模型 {view.model || '未配置'} · 配置版本 {view.revision || '—'}
            {view.configured ? '' : '（配置不完整，评测无法判分）'}
          </p>
          {!view.editable ? (
            <p>仅管理员可修改判分模型配置。</p>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                save();
              }}
            >
              <label>
                网关地址（OpenAI 兼容 /chat/completions，需支持工具调用）
                <input {...field('base_url')} placeholder="https://llm.example.com/v1" required />
              </label>
              <label>
                模型名称
                <input {...field('model')} required />
              </label>
              <label>
                API Key
                <input
                  {...field('api_key')}
                  type="password"
                  autoComplete="new-password"
                  placeholder={view.api_key_set ? '已配置；网关不变时留空沿用' : '请输入 API Key'}
                />
              </label>
              <p className="notice">
                该配置对所有用户的评测生效，判分时会把题目、参考答案和回答发送到该网关。修改后，排队及进行中的评测会停止判分，需重新创建轮次。
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  act(
                    () => api('/judge-settings/test', form),
                    '测试通过：判分模型连接正常，已返回有效的结构化判分。',
                    '测试连接失败',
                  )
                }
              >
                测试连接
              </button>
              <button disabled={busy}>保存</button>
              {view.source === 'settings' && (
                <button type="button" disabled={busy} onClick={reset}>
                  恢复部署配置
                </button>
              )}
            </form>
          )}
        </>
      )}
    </section>
  );
}
