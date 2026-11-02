// 登记数据源抽屉（设计稿 #evdWiz）：类型、连接参数、凭据（只写不读）、范围与限制、授权说明与负责人、测试连接。
// 管理员：新登记走 EV-02（登记后立即授权本项目并测试连接），编辑走 EV-04，凭据轮换 / 删除走 EV-06 / EV-07，测试走 EV-08；
// 普通用户：只能填不含密钥的部分，「提交给管理员」走 EV-03（契约 8.1 #45）。
import { useEffect, useMemo, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Area, Button, Field, Icon, Inp, Note, Seg, Select, cx } from '../../ui/index.jsx';
import {
  CHECK_LABEL, KIND_ICON, KIND_LABEL, MASKING, checkDetail, connFields, emptyForm, fmtDay, fmtEgress, formFromSource,
  parsePortDb, registerBody, secretField, validateForm,
} from './evidenceModel.js';
import { EvdDrawer } from './parts.jsx';

const STEP_TITLES = ['选择类型', '连接参数', '凭据', '范围与限制', '授权说明与负责人', '测试连接'];

function Step({ n, state, title, extra, right, children }) {
  return (
    <div className={cx('evd-step', state === 'cur' && 'cur')} data-step={n}>
      <div className="sh">
        <span className={cx('sn', state === 'done' && 'done', state === 'todo' && 'todo')}>{state === 'done' ? <Icon name="check" size={12} /> : n}</span>
        <b>{title}</b>
        {extra}
        {right ? <><span className="sp" />{right}</> : null}
      </div>
      {children}
    </div>
  );
}

// 范围白名单：标签 + 「+ 添加」
function ScopeTags({ value, onChange, disabled }) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState('');
  const add = () => {
    const parts = text.split(/[\s,，]+/).filter(Boolean).filter((x) => !value.includes(x));
    if (parts.length) onChange([...value, ...parts]);
    setText('');
    setAdding(false);
  };
  return (
    <div className="evd-tags">
      {value.map((t) => (
        <span className="tag" key={t}>
          {t}
          {disabled ? null : <span className="x" title="移除" onClick={() => onChange(value.filter((x) => x !== t))}><Icon name="x" size={12} /></span>}
        </span>
      ))}
      {disabled ? null : adding ? (
        <input
          autoFocus
          value={text}
          placeholder="如 erp.store*"
          onChange={(e) => setText(e.target.value)}
          onBlur={add}
          onKeyDown={(e) => { if (e.key === 'Enter') add(); if (e.key === 'Escape') { setText(''); setAdding(false); } }}
        />
      ) : <span className="link sm" style={{ cursor: 'pointer' }} onClick={() => setAdding(true)}>+ 添加</span>}
    </div>
  );
}

export function SourceWizard({ open, mode, source, focus, providers, admin, onClose, onSaved }) {
  const { api, user } = useApp();
  const { pid } = useProject();
  const editing = mode === 'edit';
  const [form, setForm] = useState(() => emptyForm(user));
  const [cred, setCred] = useState(null);
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  const [showErr, setShowErr] = useState(false);
  const [serverErr, setServerErr] = useState({});
  const [busy, setBusy] = useState(false);
  const [egressTouched, setEgressTouched] = useState(false);

  useEffect(() => {
    if (!open) return;
    setForm(editing && source ? formFromSource(source) : emptyForm(user));
    setCred(editing && source ? { ref: source.cred_ref, at: source.secret_fields?.find((x) => x.set)?.written_at || null } : null);
    setTest(null);
    setShowErr(false);
    setServerErr({});
    setEgressTouched(editing);
  }, [open, editing, source, user]);
  // 从「⋯ 轮换凭据」进来时直接滚到凭据一步
  useEffect(() => {
    if (!open || focus !== 'cred') return undefined;
    const t = setTimeout(() => document.querySelector('#evdWiz [data-step="3"]')?.scrollIntoView({ block: 'start' }), 0);
    return () => clearTimeout(t);
  }, [open, focus]);

  const items = providers?.items || [];
  const provider = items.find((p) => p.id === form.provider) || null;
  const { fields, portDb } = connFields(provider);
  const sf = secretField(provider);
  const v = validateForm(form, { provider, admin, editing, hasCred: !!cred?.ref, tested: !!test?.ok });
  const errs = { ...v.errors, ...serverErr };
  const stepState = (i) => (v.done[i] ? 'done' : i === v.current ? 'cur' : 'todo');
  const err = (k) => (showErr && errs[k] ? <span className="evp-err">{errs[k]}</span> : null);

  const set = (patch) => {
    setForm((f) => {
      const next = { ...f, ...patch };
      // 出口地址没手动改过时，跟着主机和端口走
      if (!egressTouched && (patch.conn || patch.port_db !== undefined)) {
        const port = parsePortDb(next.port_db)?.port;
        next.egress = next.conn.host && port ? fmtEgress([{ host: next.conn.host, port }]) : next.egress;
      }
      return next;
    });
    if (patch.id !== undefined) setServerErr((e) => ({ ...e, id: undefined }));
  };
  const setConn = (name, value) => set({ conn: { ...form.conn, [name]: value } });

  const types = useMemo(() => items.filter((p) => p.kind !== 'file'), [items]);
  const pickType = (p) => {
    if (editing) return;
    if (!p.installed) { toast(`「${p.label}」提供方未安装`, 'puzzle'); return; }
    if (p.id === form.provider) return;
    set({ provider: p.id, conn: {}, port_db: '' });
  };

  const runTest = async (sid = source?.id) => {
    if (!sid) return null;
    setTesting(true);
    try {
      const r = await api.testEvidenceSource(sid, { project_id: pid });
      setTest(r);
      return r;
    } catch (e) {
      toast(errorText(e), 'alert');
      return null;
    } finally {
      setTesting(false);
    }
  };

  const rotate = async () => {
    if (!sf) return;
    if (!form.password) { toast('先在下面写入新密码，再轮换', 'key'); return; }
    try {
      const r = await api.writeEvidenceSecret(source.id, { fields: { [sf.name]: form.password } });
      setCred({ ref: r.cred_ref, at: r.written_at });
      set({ password: '' });
      toast(`已写入 ${r.cred_ref}`, 'key');
      onSaved?.({ silent: true });
    } catch (e) { toast(errorText(e), 'alert'); }
  };
  const dropCred = async () => {
    try {
      await api.deleteEvidenceSecret(source.id);
      setCred({ ref: null, at: null });
      setTest(null);
      toast('已删除凭据', 'trash');
      onSaved?.({ silent: true });
    } catch (e) { toast(errorText(e), 'alert'); }
  };

  const save = async () => {
    const blocking = Object.keys(v.errors);
    if (blocking.length) { setShowErr(true); toast('还有没填好的地方，看标红的字段', 'alert'); return; }
    setBusy(true);
    try {
      if (!admin) {
        await api.requestEvidenceSource(registerBody(form, provider, { withSecret: false }));
        toast('已提交登记申请', 'send');
        onSaved?.();
        return;
      }
      if (editing) {
        if (form.password && sf) {
          const r = await api.writeEvidenceSecret(source.id, { fields: { [sf.name]: form.password } });
          setCred({ ref: r.cred_ref, at: r.written_at });
        }
        const { id, provider: p, secret, ...rest } = registerBody(form, provider, { withSecret: false });
        await api.updateEvidenceSource(source.id, rest);
        toast(`已保存 ${source.id} 的登记`, 'check');
        onSaved?.();
        return;
      }
      const created = await api.registerEvidenceSource(registerBody(form, provider, { grantProjectId: pid }));
      const r = await api.testEvidenceSource(created.id, { project_id: pid }).catch(() => null);
      toast(r?.ok ? `已登记 ${created.id} 并授权本项目` : `已登记 ${created.id}，测试连接没通过`, r?.ok ? 'check' : 'alert');
      onSaved?.();
    } catch (e) {
      if (e.code === 'source_id_taken') { setServerErr({ id: '这个 ID 已被占用' }); setShowErr(true); }
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const title = !admin ? '申请登记数据源' : editing ? `编辑登记（管理员）` : '登记数据源（管理员）';
  const sub = admin && editing ? source?.id : null;
  const foot = (
    <>
      <Button onClick={onClose}>取消</Button>
      {admin
        ? <Button kind="primary" icon="check" disabled={busy} onClick={save}>{editing ? '保存修改' : '保存登记并授权本项目'}</Button>
        : <Button kind="primary" icon="send" disabled={busy} onClick={save}>提交给管理员</Button>}
    </>
  );
  const owners = [{ value: user?.username, label: `${user?.display_name || user?.username}（我）` }];
  if (form.owner && form.owner !== user?.username) owners.push({ value: form.owner, label: form.owner });

  return (
    <EvdDrawer id="evdWiz" open={open} onClose={onClose} icon="plug" title={title} sub={sub} foot={foot}>
      {!admin ? <div className="mb12"><Note tone="amber" icon="lock">需要管理员登记，先填不含密钥的部分提交给管理员。</Note></div> : null}
      <div className="evd-stepper">{STEP_TITLES.map((t, i) => <i key={t} className={v.done[i] || i === v.current ? 'on' : ''} />)}</div>

      <Step n={1} state={stepState(0)} title="选择类型" right={provider ? <span className="xs muted">{KIND_LABEL[provider.kind]} · {provider.label}</span> : null}>
        <div className="evd-types">
          {types.map((p) => (
            <div key={p.id} className={cx('t', p.id === form.provider && 'on', !p.installed && 'na')} style={editing && p.id !== form.provider ? { opacity: 0.45, cursor: 'not-allowed' } : undefined} onClick={() => pickType(p)}>
              <Icon name={KIND_ICON[p.kind] || 'dot'} size={14} />{p.label}{p.installed ? null : <span className="xs faint">未安装</span>}
            </div>
          ))}
        </div>
      </Step>

      <Step n={2} state={stepState(1)} title="连接参数">
        {provider ? (
          <div className="evd-g2">
            <Field label="显示名" hint={err('display_name')}><Inp value={form.display_name} placeholder="如 ERP 生产库" onChange={(e) => set({ display_name: e.target.value })} error={showErr && errs.display_name} /></Field>
            <Field label="数据源 ID" hint={err('id') || '登记后不能改'}><Inp mono value={form.id} placeholder="如 erp-prod" disabled={editing} onChange={(e) => set({ id: e.target.value.trim() })} error={showErr && errs.id} /></Field>
            {(provider.cred_fields || []).filter((f) => !f.secret).map((f) => {
              if (portDb && f.name === 'database') return null;
              if (portDb && f.name === 'port') {
                return <Field key="port_db" label="端口 / 库名" hint={err('port_db')}><Inp mono value={form.port_db} placeholder="3306 / erp" onChange={(e) => set({ port_db: e.target.value })} error={showErr && errs.port_db} /></Field>;
              }
              if (!fields.some((x) => x.name === f.name)) return null;
              return <Field key={f.name} label={f.label} hint={err(`conn.${f.name}`)}><Inp mono value={form.conn[f.name] ?? ''} onChange={(e) => setConn(f.name, e.target.value)} error={showErr && errs[`conn.${f.name}`]} /></Field>;
            })}
            <Field label="环境" hint="生产环境每次采集都要你确认"><Seg items={[['production', '生产'], ['test', '测试']]} value={form.environment} onChange={(k) => set({ environment: k })} /></Field>
          </div>
        ) : <div className="sm muted">先选类型</div>}
      </Step>

      <Step n={3} state={stepState(2)} title="凭据">
        {provider && !sf ? <div className="sm muted">这类数据源不需要凭据。</div> : !admin ? (
          <>
            <div className="evd-cred none"><Icon name="key" size={18} /><div className="grow sm muted">凭据由管理员写入</div></div>
            <div className="mt8"><Field label="写入新密码"><Inp icon="lock" type="password" disabled placeholder="由管理员写入" /></Field></div>
          </>
        ) : (
          <>
            {editing && cred?.ref ? (
              <div className="evd-cred">
                <Icon name="key" size={18} />
                <div className="grow">
                  <div className="anchor" style={{ fontSize: 12.5 }}>{cred.ref}</div>
                  {cred.at ? <div className="xs muted mt4">{fmtDay(cred.at)} 写入</div> : null}
                </div>
                <Button size="sm" icon="refresh" onClick={rotate}>轮换</Button>
                <Button size="sm" icon="trash" kind="ghost" title="删除凭据" onClick={dropCred} />
              </div>
            ) : (
              <div className="evd-cred none"><Icon name="key" size={18} /><div className="grow sm muted">{editing ? '凭据已删除，写入新密码后才能采集。' : '还没有凭据'}</div></div>
            )}
            <div className="mt8">
              <Field label="写入新密码">
                <Inp icon="lock" type="password" autoComplete="new-password" value={form.password} placeholder={sf?.label ? `写入${sf.label}` : ''} onChange={(e) => set({ password: e.target.value })} />
              </Field>
            </div>
          </>
        )}
      </Step>

      <Step n={4} state={stepState(3)} title="范围与限制">
        <Field label="范围白名单" hint={err('scope') || '只采这些对象'}><ScopeTags value={form.scope} onChange={(scope) => set({ scope })} /></Field>
        <div className="evd-g2 mt8">
          <Field label="允许连接的地址" hint={err('egress')}><Inp mono value={form.egress} placeholder="192.0.2.14:3306" onChange={(e) => { setEgressTouched(true); set({ egress: e.target.value }); }} error={showErr && errs.egress} /></Field>
          <Field label="访问时间窗口" hint={err('window')}><Inp icon="clock" value={form.window} placeholder="全天" onChange={(e) => set({ window: e.target.value })} error={showErr && errs.window} /></Field>
          <Field label="预算" hint={err('budget')}><Inp value={form.budget} onChange={(e) => set({ budget: e.target.value })} error={showErr && errs.budget} /></Field>
          <Field label="限流" hint={err('rate')}><Inp value={form.rate} onChange={(e) => set({ rate: e.target.value })} error={showErr && errs.rate} /></Field>
        </div>
        <div className="mt8">
          <Field label="脱敏策略" hint="样本每表最多 20 行">
            <Select value={form.masking} options={MASKING.map(([value, label]) => ({ value, label }))} onChange={(masking) => set({ masking })} />
          </Field>
        </div>
      </Step>

      <Step n={5} state={stepState(4)} title="授权说明与负责人">
        <Field label="授权说明" hint={err('note') || 'REA 逆向类数据源必填，要写明软件授权范围'}>
          <Area minHeight={56} value={form.note} placeholder="谁在什么时候开通的只读账号，用于什么" onChange={(e) => set({ note: e.target.value })} />
        </Field>
        <div className="mt8"><Field label="负责人" hint={err('owner')}><Select icon="user" value={form.owner} options={owners} onChange={(owner) => set({ owner })} /></Field></div>
      </Step>

      <Step
        n={6}
        state={stepState(5)}
        title="测试连接"
        right={admin && editing ? <Button size="xs" icon="refresh" disabled={testing} onClick={() => runTest()}>{test ? '重新测试' : '测试连接'}</Button> : null}
      >
        {test ? (
          <>
            {test.checks.map((c) => (
              <div className="evd-tst" key={c.name}>
                <span className={cx('ok', !c.ok && 'no')}><Icon name={c.ok ? 'check' : 'x'} size={12} /></span>
                <b>{CHECK_LABEL[c.name] || c.name}</b><span className="muted">{checkDetail(c)}</span>
              </div>
            ))}
          </>
        ) : testing ? <div className="sm muted row g4"><span className="spin" />正在测试…</div> : admin && !editing ? <div className="sm muted">保存后自动测试</div> : null}
      </Step>
    </EvdDrawer>
  );
}
