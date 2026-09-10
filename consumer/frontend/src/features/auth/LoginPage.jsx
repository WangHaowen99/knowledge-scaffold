import { useEffect, useState } from 'react';
import {
  DatabaseZap,
  LoaderCircle,
  PackageOpen,
  ShieldCheck,
} from 'lucide-react';
import { useAuthContext } from './useAuthContext';
import './login.css';

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
const REGISTER_VALIDATION_MESSAGE = '请先完成表单必填项和格式校验后再注册';

const parseRegisterError = (status, data) => {
  if (status === 503) {
    return { message: '注册功能未开启', violations: [] };
  }
  if (status === 429) {
    return { message: '请求过于频繁，请稍后再试', violations: [] };
  }
  const violations =
    data.code === 'PASSWORD_POLICY_FAILED' ?
      (data.violations || []).map((code) => VIOLATION_LABELS[code] || code) :
      [];
  return { message: data.error || '注册失败', violations };
};

const postJson = async (url, body) => {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
};

export default function LoginPage() {
  const { login } = useAuthContext();
  const [view, setView] = useState('login');

  // 登录
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  // 注册：一步填资料直接建号（无邮箱验证码）
  const [regUsername, setRegUsername] = useState('');
  const [regEmail, setRegEmail] = useState('');
  const [regPassword, setRegPassword] = useState('');
  const [regError, setRegError] = useState(NO_REG_ERROR);
  const [regBusy, setRegBusy] = useState(false);
  const [registrationEnabled, setRegistrationEnabled] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch('/auth/capabilities')
      .then((res) => (res.ok ? res.json() : {}))
      .then((data) => {
        if (!cancelled) {
          setRegistrationEnabled(data.registration_enabled === true);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    if (!username || !password || busy) {
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    const result = await login(username, password);
    setBusy(false);
    if (!result.success) {
      setError(result.error || '登录失败');
    }
  };

  const showRegister = () => {
    setView('register');
    setRegError(NO_REG_ERROR);
    setNotice('');
  };

  const showLogin = () => {
    setView('login');
    setError('');
  };

  const submitRegister = async (e) => {
    e.preventDefault();
    if (regBusy) {
      return;
    }
    const form = e.currentTarget;
    const invalidForm =
      typeof form?.checkValidity === 'function' ?
        !form.checkValidity() :
        !regUsername || !regEmail || !regPassword;
    if (invalidForm) {
      if (typeof form?.reportValidity === 'function') {
        form.reportValidity();
      }
      setRegError({ message: REGISTER_VALIDATION_MESSAGE, violations: [] });
      return;
    }
    setRegBusy(true);
    setRegError(NO_REG_ERROR);
    try {
      const { ok, status, data } = await postJson('/auth/register', {
        username: regUsername,
        email: regEmail,
        password: regPassword,
      });
      if (!ok) {
        setRegError(parseRegisterError(status, data));
        return;
      }
      // 一步建号成功：不自动登录，回登录视图并预填用户名
      setUsername(regUsername);
      setPassword('');
      setNotice('注册成功，请登录');
      setError('');
      setView('login');
      setRegPassword('');
    } catch {
      setRegError({ message: '网络异常，请稍后再试', violations: [] });
    } finally {
      setRegBusy(false);
    }
  };

  const regErrorBox = regError.message && (
    <div className="lerr" role="alert">
      {regError.message}
      {regError.violations.length > 0 && (
        <ul>
          {regError.violations.map((v) => (
            <li key={v}>{v}</li>
          ))}
        </ul>
      )}
    </div>
  );

  const loginView = (
    <>
      <form className="lcard" onSubmit={submit}>
        <h2>欢迎回来</h2>
        <div className="ld">登录 知识工程工具</div>
        <div className="lfield">
          <label htmlFor="login-username">用户名</label>
          <input
            id="login-username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
          />
        </div>
        <div className="lfield">
          <label htmlFor="login-password">密码</label>
          <input
            id="login-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        {notice && <div className="lok">{notice}</div>}
        {error && <div className="lerr">{error}</div>}
        <button className="lbtn" type="submit" disabled={busy}>
          {busy && <LoaderCircle className="spin" size={16} />}
          {busy ? '登录中…' : '登录'}
        </button>
        <div className="lnote">登录即代表同意平台使用规范</div>
      </form>
      {registrationEnabled && (
        <div className="lswitch">
          还没有账号？
          <button type="button" className="llink" onClick={showRegister}>
            注册账号
          </button>
        </div>
      )}
    </>
  );

  const registerView = (
    <>
      <form className="lcard" onSubmit={submitRegister}>
        <h2>注册账号</h2>
        <div className="ld">创建你的 知识工程工具 账号</div>
        <div className="lfield">
          <label htmlFor="register-username">用户名</label>
          <input
            id="register-username"
            value={regUsername}
            onChange={(e) => setRegUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            required
          />
        </div>
        <div className="lfield">
          <label htmlFor="register-email">邮箱</label>
          <input
            id="register-email"
            type="email"
            value={regEmail}
            onChange={(e) => setRegEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </div>
        <div className="lfield">
          <label htmlFor="register-password">密码</label>
          <input
            id="register-password"
            type="password"
            value={regPassword}
            onChange={(e) => setRegPassword(e.target.value)}
            autoComplete="new-password"
            required
          />
        </div>
        {regErrorBox}
        <button className="lbtn" type="submit" disabled={regBusy}>
          {regBusy && <LoaderCircle className="spin" size={16} />}
          {regBusy ? '提交中…' : '注册'}
        </button>
      </form>
      <div className="lswitch">
        已有账号？
        <button type="button" className="llink" onClick={showLogin}>
          返回登录
        </button>
      </div>
    </>
  );

  const card = view === 'register' ? registerView : loginView;

  return (
    <div className="mwr login">
      <div className="lbrand">
        <img className="mk" src="/brand-mark.png" alt="知识工程工具 Logo" />
        <h1>知识工程工具</h1>
        <div className="tg">
          企业本体驱动的 Agent 工作台。把领域知识包、业务数据和多 Agent
          编排放进同一个工作空间。
        </div>
        <div className="feats">
          <div className="feat">
            <div className="fi">
              <PackageOpen size={17} />
            </div>
            <div>
              <div className="ft">知识包驱动</div>
              <div className="fd">
                业务对象、操作与受控脚本按包导入，语义与口径可追溯
              </div>
            </div>
          </div>
          <div className="feat">
            <div className="fi">
              <DatabaseZap size={17} />
            </div>
            <div>
              <div className="ft">连接真实数据</div>
              <div className="fd">对象经映射直查业务库，不编造字段与指标</div>
            </div>
          </div>
          <div className="feat">
            <div className="fi">
              <ShieldCheck size={17} />
            </div>
            <div>
              <div className="ft">分层配置与权限</div>
              <div className="fd">
                全局 / 空间 / 个人三层作用域，按角色最小授权
              </div>
            </div>
          </div>
        </div>
      </div>
      <div className="lform">
        <div>{card}</div>
      </div>
    </div>
  );
}
