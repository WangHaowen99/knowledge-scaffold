// 系统设置 · 模型接入（SY-01～SY-04，C-06 的安装状态已在 SY-01 里）：每个引擎一张卡、默认引擎、个人 Key 策略。
// 填模型名时按推荐表（SY-08）带出上下文窗口和最大输出；自动压缩阈值只校验 50–95，由服务端推算（B2 第 9 条）。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Chip, Field, Inp, Toggle, cx,
} from '../../ui/index.jsx';
import { FieldErr, LoadNote, Loading, SysHead, useLoad } from './parts.jsx';
import {
  ENGINE_META, compactHint, contextSummary, defaultPlaceholder, engineBody, engineForm, engineProblems, engineSavedText, installChip,
} from './sysModel.js';

// 填模型名时查推荐表：只在改了模型名时把推荐值带进表单
function usePresetMatch(model, saved, onFill) {
  const { api } = useApp();
  const [preset, setPreset] = useState(null);
  useEffect(() => {
    const name = model.trim();
    if (!name) { setPreset(null); return undefined; }
    let alive = true;
    const t = setTimeout(() => {
      api.matchContextPreset({ model: name }).then((r) => {
        if (!alive) return;
        setPreset(r.preset);
        if (r.preset && name !== saved) onFill(r.preset);
      }).catch(() => alive && setPreset(null));
    }, 350);
    return () => { alive = false; clearTimeout(t); };
  }, [api, model, saved]); // onFill 每次渲染都是新函数，只跟模型名走
  return preset;
}

function EngineCard({ id, e, onSaved, onGo, className }) {
  const { api } = useApp();
  const [f, setF] = useState(() => engineForm(e));
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [filled, setFilled] = useState(false);
  const [override, setOverride] = useState(false);
  useEffect(() => { setF(engineForm(e)); setTried(false); setFilled(false); }, [e]);
  const preset = usePresetMatch(f.model, e.model || '', (p) => {
    setF((x) => ({ ...x, window: String(p.window), max_output: String(p.max_output) }));
    setFilled(true);
  });
  const set = (k) => (ev) => setF((x) => ({ ...x, [k]: ev.target.value }));
  const problems = tried ? engineProblems(f) : {};
  const body = engineBody(e, f);
  const meta = ENGINE_META[id] || ENGINE_META.jiuwen;
  const chip = installChip(e);
  const d = e.context_defaults || {};
  const stable = id === 'claude';

  const save = async () => {
    setTried(true);
    if (Object.keys(engineProblems(f)).length || !body) return;
    setBusy(true);
    try {
      const saved = await api.saveAdminModelSettings(id, body);
      onSaved(id, saved);
      toast(engineSavedText(e.label, body, saved), 'check');
      setOverride(false);
    } catch (err) {
      toast(errorText(err), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const hint = (k, text) => (problems[k] ? <FieldErr>{problems[k]}</FieldErr> : text);
  const ctxFields = (
    <div className="grid3">
      <Field label="上下文窗口" hint={hint('window', null)}>
        <Inp mono value={f.window} error={!!problems.window} placeholder={defaultPlaceholder(d.window)} onChange={set('window')} />
      </Field>
      <Field label="最大输出" hint={hint('max_output', null)}>
        <Inp mono value={f.max_output} error={!!problems.max_output} placeholder={defaultPlaceholder(d.max_output)} onChange={set('max_output')} />
      </Field>
      <Field label="自动压缩阈值" hint={hint('compact', compactHint(d.compact_at_pct))}>
        <Inp mono value={f.compact} error={!!problems.compact} placeholder="50%–95%" onChange={set('compact')} />
      </Field>
    </div>
  );
  const keyField = (
    <Field label="API Key" hint={hint('api_key', '留空表示不修改')}>
      <Inp mono icon="key" type="password" autoComplete="new-password" value={f.api_key} error={!!problems.api_key}
        placeholder={e.api_key_set ? e.api_key_masked || 'sk-••••' : '还没有设置 Key'} onChange={set('api_key')} />
    </Field>
  );
  const urlField = (
    <Field label="网关地址" hint={problems.base_url ? <FieldErr>{problems.base_url}</FieldErr> : null}>
      <Inp mono value={f.base_url} error={!!problems.base_url} placeholder="https://gateway.example.com/…" onChange={set('base_url')} />
    </Field>
  );
  const saveBtn = <Button kind="primary" disabled={busy || !body || !e.installed} onClick={save}>保存</Button>;
  const summary = contextSummary(e, f, preset);

  return (
    <Card
      className={className}
      title={e.label}
      sub={meta.sub}
      icon="cpu"
      extra={(
        <span className="row g4">
          {e.installed && !e.configured ? <Chip tone="gray" sm>未配置 · 用部署默认值</Chip> : null}
          <Chip tone={chip.tone} icon={chip.icon} sm>{chip.text}</Chip>
        </span>
      )}
    >
      <div className={cx('pl-eng', !e.installed && 'off')}>
        <div className="pl-engb">
          {stable ? (
            <>
              <div className="grid2">{urlField}{keyField}</div>
              <div className="grid2 mt12">
                <Field label="模型（可选）" hint={filled && preset ? '已按推荐表填入' : null}>
                  <Inp mono value={f.model} placeholder="留空使用网关默认模型" onChange={set('model')} />
                </Field>
                <div />
              </div>
              <div className="sec-t" style={{ marginTop: 16 }}>上下文与自动压缩</div>
              {ctxFields}
              <div className="row mt12">
                <a className="link sm" onClick={() => onGo('ctx')}>查看或编辑推荐表 →</a>
                <span className="sp" />
                {saveBtn}
              </div>
            </>
          ) : (
            <>
              <div className="grid3">
                {urlField}
                {keyField}
                <Field label="模型" hint={filled && preset ? '已按推荐表填入' : null}>
                  <Inp mono value={f.model} placeholder="model-standard" onChange={set('model')} />
                </Field>
              </div>
              {override ? <div className="mt12">{ctxFields}</div> : null}
              <div className="row mt12">
                {override ? null : (
                  <span className="xs muted">
                    上下文窗口 {summary.window ?? '—'} · 最大输出 {summary.max_output ?? '—'}（{summary.origin}，
                    <a className="link" onClick={() => setOverride(true)}>可覆盖</a>）
                  </span>
                )}
                <span className="sp" />
                {saveBtn}
              </div>
            </>
          )}
        </div>
      </div>
    </Card>
  );
}

export function ModelSection({ onGo }) {
  const { api } = useApp();
  const { data, error, setData, reload } = useLoad(() => api.adminModelSettings());
  const [busy, setBusy] = useState(false);

  const onSaved = (id, view) => setData((x) => ({ ...x, engines: { ...x.engines, [id]: view } }));
  const pickDefault = async (id) => {
    if (id === data.default_engine || busy) return;
    const e = data.engines[id];
    if (!e?.installed) { toast(`当前部署未安装${e?.label || '这个引擎'}`, 'alert'); return; }
    setBusy(true);
    try {
      const r = await api.setDefaultEngine(id);
      setData((x) => ({ ...x, default_engine: r.default_engine }));
      toast(`默认引擎已改为${e.label}`, 'check');
    } catch (err) {
      toast(errorText(err), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const setPolicy = async (allow) => {
    setBusy(true);
    try {
      const r = await api.setPersonalKeyPolicy({ allow });
      setData((x) => ({ ...x, personal_key_allowed: r.allow }));
      toast(r.allow ? '已允许个人 API Key' : '已关闭个人 API Key', r.allow ? 'key' : 'lock');
    } catch (err) {
      toast(errorText(err), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const ids = data?.engines ? ['claude', 'jiuwen'].filter((k) => data.engines[k]).concat(Object.keys(data.engines).filter((k) => !['claude', 'jiuwen'].includes(k))) : [];
  return (
    <>
      <SysHead title="模型接入" />
      {!data ? (error ? <LoadNote error={error} what="模型接入" onRetry={reload} /> : <Loading />) : (
        <>
          {ids.map((id, i) => (
            <EngineCard key={id} id={id} e={data.engines[id]} onSaved={onSaved} onGo={onGo} className={i ? 'mt16' : undefined} />
          ))}
          <div className="grid2 mt16">
            <Card title="默认引擎" icon="star">
              <div className="seg">
                {ids.map((id) => {
                  const e = data.engines[id];
                  return (
                    <button key={id} type="button" className={id === data.default_engine ? 'on' : ''} disabled={!e.installed || busy}
                      title={e.installed ? undefined : '当前部署未安装'} onClick={() => pickDefault(id)}>
                      {e.label}
                    </button>
                  );
                })}
              </div>
              <div className="xs muted mt8">只影响之后新建、导入的项目</div>
            </Card>
            <Card title="个人 API Key" icon="key" extra={<Toggle on={data.personal_key_allowed} disabled={busy} onChange={setPolicy} />}>
              <div className="sm">允许用户用自己的 Key</div>
              <div className="xs muted mt4">关闭后全部会话改用本页配置</div>
            </Card>
          </div>
        </>
      )}
    </>
  );
}
