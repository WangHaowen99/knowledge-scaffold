import { useEffect, useState } from 'react';
import { login, register, registerVerify, registerResend, setSession } from './api.js';

// 后端 PASSWORD_POLICY_FAILED violations 代码 → 中文文案
const VIOLATION_LABELS = {
  min_length: '至少 8 位',
  max_length: '不超过 128 位',
  character_classes: '需包含大写/小写/数字/符号中至少三类',
  contains_username: '不能包含用户名',
  common_password: '过于常见',
  surrounding_whitespace: '首尾不能有空格',
  same_as_old: '不能与旧密码相同',
};

const NO_REG_ERROR = { message: '', violations: [] };

const parseRegisterError = (e) => {
  if (e.status === 503) return { message: '注册功能未开启（请联系管理员）', violations: [] };
  if (e.status === 429) return { message: '请求过于频繁，请稍后再试', violations: [] };
  const data = e.data || {};
  const violations = e.status === 400 && data.code === 'PASSWORD_POLICY_FAILED'
    ? (data.violations || []).map((code) => VIOLATION_LABELS[code] || code)
    : [];
  return { message: data.error || e.message || '注册失败', violations };
};

export default function LoginPage({ onLogin }) {
  const [view, setView] = useState('login'); // login | register

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  // 注册：step 1 填资料 → step 2 邮箱验证码
  const [regStep, setRegStep] = useState(1);
  const [regUsername, setRegUsername] = useState('');
  const [regEmail, setRegEmail] = useState('');
  const [regPassword, setRegPassword] = useState('');
  const [regCode, setRegCode] = useState('');
  const [regErr, setRegErr] = useState(NO_REG_ERROR);
  const [regBusy, setRegBusy] = useState(false);
  const [cooldown, setCooldown] = useState(0);

  useEffect(() => {
    if (cooldown <= 0) return undefined;
    const timer = setTimeout(() => setCooldown((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  const submit = async (e) => {
    e.preventDefault();
    if (!username || !password || busy) return;
    setBusy(true); setErr(''); setNotice('');
    try {
      const data = await login(username, password);
      setSession(data);
      onLogin(data.user);
    } catch (e2) {
      setErr(e2.message || '登录失败');
    } finally {
      setBusy(false);
    }
  };

  const showRegister = () => { setView('register'); setRegStep(1); setRegErr(NO_REG_ERROR); setNotice(''); };
  const showLogin = () => { setView('login'); setErr(''); };

  const submitRegister = async (e) => {
    e.preventDefault();
    if (!regUsername || !regEmail || !regPassword || regBusy) return;
    setRegBusy(true); setRegErr(NO_REG_ERROR);
    try {
      await register(regUsername, regEmail, regPassword);
      setRegStep(2);
      setRegCode('');
      setCooldown(60);
    } catch (e2) {
      setRegErr(parseRegisterError(e2));
    } finally {
      setRegBusy(false);
    }
  };

  const submitVerify = async (e) => {
    e.preventDefault();
    if (regCode.length !== 6 || regBusy) return;
    setRegBusy(true); setRegErr(NO_REG_ERROR);
    try {
      await registerVerify(regEmail, regCode);
      setUsername(regUsername);
      setPassword('');
      setNotice('注册成功，请登录');
      setErr('');
      setView('login');
      setRegStep(1);
      setRegPassword('');
      setRegCode('');
    } catch (e2) {
      setRegErr(parseRegisterError(e2));
    } finally {
      setRegBusy(false);
    }
  };

  const resendCode = async () => {
    if (cooldown > 0 || regBusy) return;
    setRegBusy(true); setRegErr(NO_REG_ERROR);
    try {
      await registerResend(regEmail);
      setCooldown(60);
    } catch (e2) {
      setRegErr(parseRegisterError(e2));
    } finally {
      setRegBusy(false);
    }
  };

  // 中途离开/刷新后重回验证码步骤的入口（验证码 15 分钟内有效，冷却由服务端把关）
  const goVerify = () => {
    if (!regEmail) { setRegErr({ message: '请先填写注册时使用的邮箱', violations: [] }); return; }
    setRegStep(2); setRegCode(''); setRegErr(NO_REG_ERROR);
  };

  const regErrBox = regErr.message && (
    <div className="lerr">
      {regErr.message}
      {regErr.violations.length > 0 && (
        <ul>{regErr.violations.map((v) => <li key={v}>{v}</li>)}</ul>
      )}
    </div>
  );

  const loginView = (
    <>
      <form className="lcard" onSubmit={submit}>
        <h2>欢迎回来</h2>
        <div className="ld">使用知识工程平台账号登录</div>
        <div className="field"><label>用户名</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" /></div>
        <div className="field"><label>密码</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></div>
        {notice && <div className="lok">{notice}</div>}
        {err && <div className="lerr">{err}</div>}
        <button className="lbtn" type="submit" disabled={busy}>{busy ? '登录中…' : '登 录'}</button>
        <div className="lswitch">
          还没有账号？<button type="button" className="llink" onClick={showRegister}>注册新账号</button>
        </div>
      </form>
    </>
  );

  const registerView = (
    <>
      <form className="lcard" onSubmit={submitRegister}>
        <h2>注册账号</h2>
        <div className="ld">创建你的知识包建模工具账号</div>
        <div className="field"><label>用户名</label>
          <input value={regUsername} onChange={(e) => setRegUsername(e.target.value)} autoFocus autoComplete="username" /></div>
        <div className="field"><label>邮箱</label>
          <input type="email" value={regEmail} onChange={(e) => setRegEmail(e.target.value)} autoComplete="email" /></div>
        <div className="field"><label>密码</label>
          <input type="password" value={regPassword} onChange={(e) => setRegPassword(e.target.value)} autoComplete="new-password" /></div>
        {regErrBox}
        <button className="lbtn" type="submit" disabled={regBusy}>{regBusy ? '提交中…' : '下一步'}</button>
        <div className="lswitch">
          已收到验证码？<button type="button" className="llink" onClick={goVerify}>直接输入验证码</button>
        </div>
        <div className="lswitch">
          已有账号？<button type="button" className="llink" onClick={showLogin}>返回登录</button>
        </div>
      </form>
    </>
  );

  const verifyView = (
    <>
      <form className="lcard" onSubmit={submitVerify}>
        <h2>验证邮箱</h2>
        <div className="ld">验证码已发送至 {regEmail}，请查收邮件完成验证</div>
        <div className="field"><label>验证码</label>
          <input value={regCode} onChange={(e) => setRegCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            autoFocus inputMode="numeric" autoComplete="one-time-code" placeholder="6 位数字验证码" /></div>
        {regErrBox}
        <button className="lbtn" type="submit" disabled={regBusy || regCode.length !== 6}>
          {regBusy ? '验证中…' : '验证并完成注册'}
        </button>
        <div className="lswitch">
          没收到验证码？
          <button type="button" className="llink" disabled={cooldown > 0 || regBusy} onClick={resendCode}>
            {cooldown > 0 ? `重新发送（${cooldown}s）` : '重新发送'}
          </button>
        </div>
        <div className="lswitch">
          <button type="button" className="llink" onClick={showLogin}>返回登录</button>
        </div>
      </form>
    </>
  );

  let card = loginView;
  if (view === 'register') card = regStep === 1 ? registerView : verifyView;

  return (
    <div className="mwr login">
      <div className="lbrand">
        <div className="mk">知</div>
        <h1>知识包建模工具</h1>
        <div className="tg">通过访谈式交互，把领域资料与数据表结构化为可实测、可迭代的知识包。</div>
        <div className="feats">
          <div className="feat"><div className="fi">📥</div><div><div className="ft">资料驱动</div><div className="fd">文档 + Excel 数据表自动解析为数据字典</div></div></div>
          <div className="feat"><div className="fi">🗣️</div><div><div className="ft">访谈式建模</div><div className="fd">出题选项式访谈澄清对象、字段与口径</div></div></div>
          <div className="feat"><div className="fi">🔁</div><div><div className="ft">评测闭环</div><div className="fd">导入 consumer 实测，按评测反馈迭代优化</div></div></div>
        </div>
      </div>
      <div className="lform">
        <div>{card}</div>
      </div>
    </div>
  );
}
