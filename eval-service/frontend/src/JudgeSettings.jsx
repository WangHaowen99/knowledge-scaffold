import React, { useEffect, useState } from 'react';
import { api } from './api.js';
import { Alert, Confirm } from './ui.jsx';

const SOURCES = { personal: '我的配置', settings: '默认配置', deployment: '默认配置' };

// One editable configuration: the caller's own (/judge-settings/me) or the shared default (administrators only).
function ConfigForm({ path, initial, keySet, notice, saveDone, extra, onSaved }) {
  const blank = () => ({ base_url: initial?.base_url || '', model: initial?.model || '', api_key: '' });
  const [form, setForm] = useState(blank),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [errorTitle, setErrorTitle] = useState('操作未完成'),
    [done, setDone] = useState('');
  useEffect(() => setForm(blank()), [path, initial?.base_url, initial?.model, initial?.revision]);
  async function act(work, message, failureTitle = '操作未完成') {
    setBusy(true);
    setError('');
    setDone('');
    setErrorTitle(failureTitle);
    try {
      await work();
      if (message) setDone(message);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const field = (key) => ({
    value: form[key],
    disabled: busy,
    onChange: (e) => {
      setForm({ ...form, [key]: e.target.value });
      setError('');
      setDone('');
    },
  });
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        act(async () => {
          await api(path, form, 'PUT');
          await onSaved();
        }, saveDone);
      }}
    >
      {error && (
        <Alert tone="red" title={errorTitle}>
          {error}
        </Alert>
      )}
      {done && <Alert tone="green">{done}</Alert>}
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
          placeholder={keySet ? '已配置；网关不变时留空沿用' : '请输入 API Key'}
        />
      </label>
      <p className="notice">{notice}</p>
      <button
        type="button"
        disabled={busy}
        onClick={() =>
          act(
            () => api(`${path}/test`, form),
            '测试通过：判分模型连接正常，已返回有效的结构化判分。',
            '测试连接失败',
          )
        }
      >
        测试连接
      </button>
      <button disabled={busy}>保存</button>
      {extra && (
        <button type="button" disabled={busy} onClick={() => act(extra.run, extra.done)}>
          {extra.label}
        </button>
      )}
    </form>
  );
}

function Feedback({ value, part }) {
  if (value?.part !== part) return null;
  return <Alert tone={value.tone}>{value.text}</Alert>;
}

function activeText(active) {
  if (active.source === 'disabled') return '当前生效：无。管理员已停用默认配置，请填写你自己的判分模型后再发起评测。';
  return `当前生效：${SOURCES[active.source]} · 模型 ${active.model || '未配置'} · 配置版本 ${active.revision || '—'}${
    active.configured ? '' : '（配置不完整，评测无法判分）'
  }`;
}

// Everyone edits their own configuration; administrators also maintain the shared default and can switch it off.
// "使用默认配置" follows the shared default; saving your own values switches to a personal configuration.
export function JudgeSettings({ onClose, onChanged }) {
  const [shared, setShared] = useState(null),
    [mine, setMine] = useState(null),
    [useDefault, setUseDefault] = useState(true),
    [busy, setBusy] = useState(false),
    [loadError, setLoadError] = useState(''),
    [feedback, setFeedback] = useState(null),
    [confirmOff, setConfirmOff] = useState(false);
  async function load() {
    const [defaults, personal] = await Promise.all([api('/judge-settings'), api('/judge-settings/me')]);
    setShared(defaults);
    setMine(personal);
    setUseDefault(!personal.personal && defaults.enabled);
  }
  useEffect(() => {
    load().catch((e) => setLoadError(e.message));
  }, []);
  async function changed() {
    await load();
    await onChanged?.();
  }
  async function perform(part, work, message) {
    setBusy(true);
    setFeedback(null);
    try {
      await work();
      setFeedback({ part, tone: 'green', text: message });
    } catch (e) {
      setFeedback({ part, tone: 'red', text: e.message });
    } finally {
      setBusy(false);
    }
  }
  async function switchDefault(enabled) {
    await api('/judge-settings/enabled', { enabled }, 'PUT');
    await changed();
  }
  function toggle(checked) {
    setFeedback(null);
    // Unchecking only reveals the personal form; nothing changes until it is saved.
    if (!checked || !mine?.personal) {
      setUseDefault(checked);
      return;
    }
    perform(
      'mine',
      async () => {
        await api('/judge-settings/me', undefined, 'DELETE');
        await changed();
      },
      '已切换为默认配置，你新创建的评测轮次将使用默认配置',
    );
  }
  if (!shared || !mine)
    return (
      <section className="settings">
        {loadError ? (
          <Alert tone="red" title="配置读取失败">
            {loadError}
          </Alert>
        ) : (
          <p>正在读取判分模型配置…</p>
        )}
      </section>
    );
  const admin = shared.editable;
  return (
    <>
      <section className="settings">
        <div className="row">
          <h2>{admin ? '我的判分模型' : '评测判分模型配置'}</h2>
          {onClose && <button onClick={onClose}>关闭</button>}
        </div>
        <Feedback value={feedback} part="mine" />
        <p>{activeText(mine.active)}</p>
        <label className="use-default">
          <input
            type="checkbox"
            checked={useDefault}
            disabled={busy || !shared.enabled}
            onChange={(e) => toggle(e.target.checked)}
          />
          使用默认配置{shared.enabled ? '' : '（已停用）'}
        </label>
        {!useDefault ? (
          <ConfigForm
            path="/judge-settings/me"
            initial={mine.personal || { model: shared.model }}
            keySet={!!mine.personal?.api_key_set}
            notice={`保存后成为你单独的配置，只对你新发起的评测生效；判分时会把你的评测题目、参考答案和回答发送到该网关。API Key 不会沿用默认配置的 Key。修改后，你排队及进行中的评测会停止判分，需重新创建轮次。${
              shared.enabled ? '勾选“使用默认配置”可随时切回。' : ''
            }`}
            saveDone="已保存为你的配置，你新创建的评测轮次将使用该配置"
            onSaved={changed}
          />
        ) : (
          <p>
            正在使用默认配置（模型 {shared.model || '未配置'}），{admin ? '在下方“默认判分模型”中维护' : '由管理员维护'}
            。取消勾选“使用默认配置”即可填写你自己的判分模型。
          </p>
        )}
      </section>
      {admin && (
        <section className="settings">
          <div className="row">
            <h2>默认判分模型</h2>
            <button
              type="button"
              role="switch"
              aria-checked={shared.enabled}
              aria-label="启用默认判分模型"
              className="switch"
              disabled={busy}
              onClick={() =>
                shared.enabled
                  ? setConfirmOff(true)
                  : perform('default', () => switchDefault(true), '默认配置已启用，未单独配置的用户恢复使用默认配置')
              }
            >
              <span aria-hidden="true" />
              {shared.enabled ? '已启用' : '已停用'}
            </button>
          </div>
          <Feedback value={feedback} part="default" />
          <p>
            供所有用户使用：未单独配置判分模型的用户，以及勾选“使用默认配置”的你，都使用此配置。来源：
            {shared.source === 'settings' ? '页面配置' : '部署配置'} · 配置版本 {shared.revision || '—'}
          </p>
          {!shared.enabled && (
            <Alert tone="amber" title="默认配置已停用">
              未单独配置的用户须先配置自己的判分模型才能发起评测；重新启用后，因停用而中断的评测可以恢复。
            </Alert>
          )}
          <ConfigForm
            path="/judge-settings"
            initial={shared}
            keySet={!!shared.api_key_set}
            notice="对所有使用默认配置的用户生效，判分时会把题目、参考答案和回答发送到该网关。修改后，使用默认配置的排队及进行中的评测会停止判分，需重新创建轮次。"
            saveDone="已保存默认配置"
            extra={
              shared.source === 'settings' && {
                label: '恢复部署配置',
                done: '默认配置已恢复为部署配置',
                run: async () => {
                  await api('/judge-settings', undefined, 'DELETE');
                  await changed();
                },
              }
            }
            onSaved={changed}
          />
        </section>
      )}
      {confirmOff && (
        <Confirm
          title="停用默认判分模型？"
          label="停用"
          danger
          onClose={() => setConfirmOff(false)}
          onConfirm={async () => {
            await switchDefault(false);
            setFeedback({ part: 'default', tone: 'green', text: '默认配置已停用' });
          }}
        >
          <p>
            未单独配置判分模型的用户将无法发起新评测，他们使用默认配置排队及进行中的评测会停止判分并中断；重新启用后可恢复这些评测。已保存的个人配置不受影响，内置
            NL2SQL 查询仍使用默认配置。
          </p>
        </Confirm>
      )}
    </>
  );
}
