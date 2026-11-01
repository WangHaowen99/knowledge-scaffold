// 个人设置 · 我的模型配置（A-09～A-11）：每个引擎一张卡，部署默认 / 管理员配置 / 个人 Key 三选一。
// Key 加密存在服务端、不回显，留空表示保持不变；网关、模型只在改过时提交。保存从下一条消息生效。
// 管理员关闭个人 Key（personal_allowed=false）时整卡变灰、不能改。
import { useCallback, useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Button, Chip, Field, Icon, Inp, Note, cx } from '../../ui/index.jsx';
import {
  canClear, clearedToast, contextText, engineCards, modelDirty, modelForm, modelProblems, modelRequest, placeholders, sourceChip, sourceOptions,
} from './meModel.js';

const FIELD_ERR = { required: '这一项必填', invalid_url: '网关地址要以 http:// 或 https:// 开头' };
const red = (t) => (t ? <span style={{ color: 'var(--red)' }}>{t}</span> : null);

function EngineCard({ card, allowed, onChange, onForbidden }) {
  const { api } = useApp();
  const { cfg } = card;
  const [f, setF] = useState(() => modelForm(cfg));
  const [tried, setTried] = useState(false);
  const [serverErr, setServerErr] = useState({});
  const [busy, setBusy] = useState(false);
  useEffect(() => { setF(modelForm(cfg)); setTried(false); setServerErr({}); }, [cfg]);

  const dirty = modelDirty(cfg, f);
  const problems = { ...(tried ? modelProblems(cfg, f) : {}), ...serverErr };
  const ph = placeholders(cfg);
  const chip = sourceChip(cfg.source);
  const ctx = contextText(cfg.context);
  // 填 Key、网关或模型就是要用个人 Key
  const edit = (k) => (e) => {
    const v = e.target.value;
    setF((x) => ({ ...x, [k]: v, source: 'personal' }));
    setServerErr(({ [k]: _drop, ...rest }) => rest);
  };

  const save = async () => {
    setTried(true);
    if (busy || !dirty || Object.keys(modelProblems(cfg, f)).length) return;
    setBusy(true);
    try {
      const r = await api.saveModelSettings(card.id, modelRequest(cfg, f));
      onChange(card.id, r, 'saved');
    } catch (e) {
      if (e.code === 'forbidden') onForbidden();
      const fields = e.code === 'invalid_argument' ? e.details?.fields || {} : {};
      const fe = Object.fromEntries(Object.entries(fields).filter(([k, v]) => v && ['api_key', 'base_url', 'model'].includes(k)).map(([k, v]) => [k, FIELD_ERR[v] || '填写的内容不符合要求']));
      setServerErr(fe);
      if (!Object.keys(fe).length) toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const clear = async () => {
    setBusy(true);
    try {
      const r = await api.clearModelSettings(card.id);
      onChange(card.id, r, 'cleared');
      toast(clearedToast(r), 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={cx('card pl-eng', !allowed && 'off')} data-engine={card.id}>
      <div className="card-h">
        <span className={cx('pl-ico', card.icon)} style={{ width: 30, height: 30 }}><Icon name="cpu" size={14} /></span>
        <h3>{card.title}</h3>
        <span className="sub">{card.sub}</span>
        <span className="sp" />
        <Chip tone={chip.tone} icon={chip.icon} sm>{chip.text}</Chip>
      </div>
      <div className="card-b">
        {!allowed ? <div className="mb12"><Note tone="gray" icon="lock">管理员已关闭个人 API Key，当前统一使用管理员配置。</Note></div> : null}
        <div className="pl-engb">
          <div className="pl-radio c3">
            {sourceOptions(cfg).map((o) => (
              <div key={o.key} className={cx('opt', f.source === o.key && 'on')} role="radio" aria-checked={f.source === o.key}
                onClick={() => allowed && setF((x) => ({ ...x, source: o.key }))}>
                <span className={cx('rad', f.source === o.key && 'on')} />
                <div><div className="t">{o.title}</div><div className="c">{o.desc}</div></div>
              </div>
            ))}
          </div>
          <div className="grid2 mt12">
            <Field label="API Key" req hint={red(problems.api_key) || 'Key 加密保存在服务端，界面不回显；留空表示保持不变'}>
              <Inp icon="key" mono type="password" autoComplete="new-password" value={f.api_key} onChange={edit('api_key')} placeholder={ph.api_key}
                error={!!problems.api_key} disabled={!allowed} after={cfg.api_key_set ? <Chip tone="teal" sm>已设置</Chip> : null} />
            </Field>
            <Field label="网关地址（可选）" hint={red(problems.base_url)}>
              <Inp value={f.base_url} onChange={edit('base_url')} placeholder={ph.base_url} error={!!problems.base_url} disabled={!allowed} autoComplete="off" spellCheck={false} />
            </Field>
          </div>
          <div className="grid2 mt12">
            <Field label="模型（可选）" hint={red(problems.model)}>
              <Inp value={f.model} onChange={edit('model')} placeholder={ph.model} error={!!problems.model} disabled={!allowed} autoComplete="off" spellCheck={false} />
            </Field>
            <div className="row me-eng-acts">
              {ctx ? <span className="xs muted grow me-ctx" title="上下文参数按管理员配置；个人 Key 只换网关、Key 和模型">{ctx}</span> : null}
              <Button kind="ghost" onClick={clear} disabled={!allowed || busy || !canClear(cfg)}>清除个人配置</Button>
              <Button kind="primary" onClick={save} disabled={!allowed || busy || !dirty}>保存</Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ModelSection() {
  const { api } = useApp();
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [okMsg, setOkMsg] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [settings, engines] = await Promise.all([api.modelSettings(), api.engines().catch(() => null)]);
      setD({ settings, engines });
    } catch (e) {
      setError(e);
    }
  }, [api]);
  useEffect(() => { load(); }, [load]);

  const onChange = (id, cfg, how) => {
    setD((x) => ({ ...x, settings: { ...x.settings, engines: { ...x.settings.engines, [id]: cfg } } }));
    if (how === 'saved') {
      setOkMsg(true);
      toast('已保存，你下一次发送消息时生效', 'check');
    }
  };

  const allowed = d?.settings?.personal_allowed !== false;
  const cards = d ? engineCards(d.settings, d.engines) : [];

  return (
    <>
      <PageHead eyebrow="个人设置" title="我的模型配置" desc="你的项目只有你在用，发消息时按这里选的配置调用模型。" />
      {error ? (
        <Note tone="red" className="mb12">
          <div className="row"><span className="grow">模型配置读不到：{errorText(error)}</span><Button size="xs" onClick={load}>重试</Button></div>
        </Note>
      ) : null}
      {okMsg && allowed ? <div className="mb12"><Note tone="teal" icon="check">已保存，你下一次发送消息时生效。正在进行的一轮不受影响。</Note></div> : null}
      <div className="col" style={{ gap: 16 }}>
        {cards.map((c) => <EngineCard key={c.id} card={c} allowed={allowed} onChange={onChange} onForbidden={load} />)}
      </div>
    </>
  );
}
