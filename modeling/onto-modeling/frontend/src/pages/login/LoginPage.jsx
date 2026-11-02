// 登录与注册（设计稿 login）：A-01 登录、A-03 注册、A-02 本地模式进入
import { useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { Chip, Field, Icon, Note, Seg, cx } from '../../ui/index.jsx';
import { passwordRules } from './passwordRules.js';

const FEATS = [
  ['book', '资料分析', '自动分析资料并沉淀领域知识'],
  ['cube', '对象与逻辑提取', '把资料结构化为业务实体与流程'],
  ['shieldok', '校验与追踪', '规则 + 语义检查、场景推演验证'],
  ['rocket', 'L4 自主构建', '设好范围和预算，一路建完再集中复核', true],
];
const RULES = [['len', '至少 8 位'], ['max', '不超过 128 位'], ['classes', '至少含三类字符'], ['contains_username', '不能包含用户名'], ['common', '不是常见密码'], ['edge_space', '首尾不能有空格']];
function Graph() {
  const lines = [[60, 60, 180, 120], [180, 120, 320, 70], [180, 120, 250, 230], [320, 70, 420, 150], [250, 230, 420, 150], [60, 60, 90, 210], [90, 210, 250, 230]];
  const dots = [[60, 60, 6], [180, 120, 11], [320, 70, 7], [250, 230, 8], [420, 150, 6], [90, 210, 5]];
  return (
    <svg className="en-lg-graph" width="460" height="300" viewBox="0 0 460 300">
      {lines.map(([a, b, c, d], i) => <line key={i} x1={a} y1={b} x2={c} y2={d} stroke="rgba(255,255,255,.5)" strokeWidth="1.2" />)}
      {dots.map(([x, y, r], i) => (
        <g key={i}>
          <circle cx={x} cy={y} r={r} fill="rgba(255,255,255,.85)" />
          <circle cx={x} cy={y} r={r + 7} fill="none" stroke="rgba(255,255,255,.25)" />
        </g>
      ))}
    </svg>
  );
}

export default function LoginPage() {
  const { api, services, config, signedIn } = useApp();
  const local = config?.auth?.enabled === false;
  const regClosedByConfig = config?.auth?.registration_enabled === false;
  const [tab, setTab] = useState('in');
  const [u, setU] = useState('');
  const [p, setP] = useState('');
  const [showPw, setShowPw] = useState(false);
  const [ru, setRu] = useState('');
  const [rp, setRp] = useState('');
  const [rp2, setRp2] = useState('');
  const [busy, setBusy] = useState(false);
  const [loginErr, setLoginErr] = useState(null);
  const [regErr, setRegErr] = useState(null);
  const [regClosed, setRegClosed] = useState(regClosedByConfig);
  const [serverFailed, setServerFailed] = useState([]);

  const rules = useMemo(() => passwordRules(ru, rp), [ru, rp]);
  const mismatch = rp2 && rp2 !== rp;
  const regOk = Object.values(rules).every(Boolean) && !mismatch && ru.length >= 3 && rp2 === rp;

  const doLogin = async (e) => {
    e?.preventDefault();
    if (!u || !p || busy) return;
    setBusy(true);
    setLoginErr(null);
    try {
      const r = await api.login(u.trim(), p);
      services.tokens.save(r);
      await signedIn(r.user);
    } catch (err) {
      setLoginErr(err);
    } finally {
      setBusy(false);
    }
  };

  const doRegister = async (e) => {
    e?.preventDefault();
    if (!regOk || busy || regClosed) return;
    setBusy(true);
    setRegErr(null);
    try {
      const r = await api.register(ru.trim(), rp);
      services.tokens.save(r);
      await signedIn(r.user);
    } catch (err) {
      if (err.code === 'registration_disabled') setRegClosed(true);
      if (err.code === 'weak_password') setServerFailed(err.details?.failed_rules || []);
      setRegErr(err);
    } finally {
      setBusy(false);
    }
  };

  const enterLocal = async () => {
    setBusy(true);
    try {
      const r = await api.enterLocal();
      services.tokens.enterLocal();
      await signedIn(r.user);
    } catch (err) {
      setLoginErr(err);
    } finally {
      setBusy(false);
    }
  };

  const loginFail = loginErr && (loginErr.code === 'invalid_credentials' || loginErr.code === 'account_locked');
  const loginRate = loginErr && loginErr.code === 'rate_limited';
  const regRate = regErr && regErr.code === 'rate_limited';

  return (
    <div className="app">
      <div className="en-lg">
        <section className="en-lg-l">
          <div className="en-lg-brand">
            <div className="logo">本</div>本体建模工作台
            <span className="chip" style={{ background: 'rgba(255,255,255,.12)', color: '#CFE0FF', borderColor: 'transparent' }}>onto-modeling</span>
          </div>
          <div className="en-lg-h">把领域业务知识<br />整理成结构化、可验证的本体模型</div>
          <div className="en-lg-sub">上传业务资料，或者连接正在运行的业务系统，助手陪你把对象、操作、规则一步步建出来，每条结论都能指回出处。</div>
          <div className="en-lg-feats">
            {FEATS.map(([i, t, d, n]) => (
              <div className="en-lg-f" key={t}>
                <span className="ii"><Icon name={i} size={18} /></span>
                <b>{t}{n ? <span className="badge-new">新</span> : null}</b>
                <span>{d}</span>
              </div>
            ))}
          </div>
          <Graph />
          <div className="en-lg-foot"><span>一个项目就是一个知识包，只归你一个人</span><span>·</span><span>支持离线部署</span></div>
        </section>
        <section className="en-lg-r">
          <div className="en-lg-card">
            {local ? (
              <div className="en-lg-local">
                <div className="ic"><Icon name="unlock" size={24} /></div>
                <h2 className="mt12" style={{ fontSize: 19 }}>当前部署未启用登录鉴权</h2>
                <p className="muted mt8 sm">本地模式：没有配置 auth-server，直接以本机用户进入。项目仍然按「一个项目只归一个人」管理。</p>
                <div className="row mt16" style={{ justifyContent: 'center' }}><Chip tone="amber" icon="info">本地模式</Chip></div>
                {loginErr ? <Note tone="red" className="mt12">{errorText(loginErr)}</Note> : null}
                <button type="button" className="btn primary lg mt16" style={{ width: '100%' }} onClick={enterLocal} disabled={busy}>进入工作台</button>
              </div>
            ) : (
              <div>
                <div className="row"><Seg items={[['in', '登录'], ['up', '注册']]} value={tab} onChange={setTab} /></div>
                {tab === 'in' ? (
                  <form onSubmit={doLogin}>
                    <h2 className="mt20">欢迎回来</h2>
                    <p className="muted">登录后开始本体建模</p>
                    {loginFail ? <div className="note red mt12">{errorText(loginErr)}</div> : null}
                    {loginRate ? <div className="note amber mt12">{errorText(loginErr)}</div> : null}
                    {loginErr && !loginFail && !loginRate ? <div className="note red mt12">{errorText(loginErr)}</div> : null}
                    <Field label="用户名">
                      <div className="inp"><Icon name="user" size={14} /><input value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" placeholder="输入用户名" /></div>
                    </Field>
                    <Field label="密码">
                      <div className={cx('inp', loginFail && 'err')}>
                        <Icon name="lock" size={14} />
                        <input type={showPw ? 'text' : 'password'} value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" placeholder="输入密码" />
                        <span style={{ cursor: 'pointer', display: 'flex' }} onClick={() => setShowPw((v) => !v)} title={showPw ? '隐藏密码' : '显示密码'}><Icon name="eye" size={14} /></span>
                      </div>
                    </Field>
                    <button type="submit" className="btn primary lg mt20" style={{ width: '100%' }} disabled={busy || !u || !p}>登 录</button>
                    <div className="mt12 sm muted" style={{ textAlign: 'center' }}>
                      还没有账号？<a className="link" onClick={() => setTab('up')}>注册账号</a>
                    </div>
                  </form>
                ) : (
                  <form onSubmit={doRegister}>
                    <h2 className="mt20">注册账号</h2>
                    <p className="muted">创建你的本体建模工作台账号</p>
                    {regClosed ? <div className="note gray mt12">注册功能未开启，请联系管理员开通账号。</div> : null}
                    {regRate ? <div className="note amber mt12">请求过于频繁，请稍后再试。</div> : null}
                    {regErr && !regRate && regErr.code !== 'registration_disabled' ? <div className="note red mt12">{errorText(regErr)}</div> : null}
                    <div style={{ opacity: regClosed ? 0.45 : 1 }}>
                      <Field label="用户名">
                        <div className={cx('inp', regErr?.code === 'username_taken' && 'err')}><Icon name="user" size={14} /><input value={ru} onChange={(e) => setRu(e.target.value)} disabled={regClosed} autoComplete="username" placeholder="3–64 位字母、数字或 _ . -" /></div>
                      </Field>
                      <Field label="密码">
                        <div className="inp"><Icon name="lock" size={14} /><input type="password" value={rp} onChange={(e) => { setRp(e.target.value); setServerFailed([]); }} disabled={regClosed} autoComplete="new-password" placeholder="设置密码" /></div>
                      </Field>
                      <div className="en-lg-rules">
                        {RULES.map(([k, t]) => {
                          const ok = rules[k] && !serverFailed.includes(k);
                          return (
                            <div key={k} className={rp ? (ok ? 'ok' : 'bad') : ''} title={k === 'classes' ? '大写、小写、数字、符号中至少三类' : undefined}>
                              <i><Icon name={!rp || ok ? 'check' : 'x'} size={12} /></i>{t}
                            </div>
                          );
                        })}
                      </div>
                      <Field label="确认密码" hint={mismatch ? <span style={{ color: 'var(--red)' }}>两次输入不一致</span> : null}>
                        <div className={cx('inp', mismatch && 'err')}><Icon name="lock" size={14} /><input type="password" value={rp2} onChange={(e) => setRp2(e.target.value)} disabled={regClosed} autoComplete="new-password" placeholder="再输一次密码" /></div>
                      </Field>
                    </div>
                    <button type="submit" className="btn primary lg mt16" style={{ width: '100%' }} disabled={!regOk || busy || regClosed}>注册并进入</button>
                  </form>
                )}
              </div>
            )}
          </div>
          <div className="en-lg-corner"><Icon name="shield" size={12} />登录令牌到期前自动续期（无感刷新），不会打断正在进行的建模或自主构建</div>
        </section>
      </div>
    </div>
  );
}
