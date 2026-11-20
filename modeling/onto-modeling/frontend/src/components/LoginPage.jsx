import { useState } from 'react';
import { api, authStore } from '../lib/api.js';

// auth-server PASSWORD_POLICY_FAILED violations 代码 → 中文文案（与 modeling 服务一致）
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
  const violations =
    e.status === 400 && data.code === 'PASSWORD_POLICY_FAILED'
      ? (data.violations || []).map((code) => VIOLATION_LABELS[code] || code)
      : [];
  return { message: data.error || e.message || '注册失败', violations };
};

const FEATS = [
  { icon: '📚', title: '资料分析', desc: '自动分析资料并沉淀领域知识' },
  { icon: '🧩', title: '对象与逻辑提取', desc: '把资料结构化为业务实体与流程' },
  { icon: '✅', title: '校验与追踪', desc: '规则 + 语义检查、场景推演验证' },
];

export default function LoginPage({ onLogin }) {
  const [view, setView] = useState('login');

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const [regUsername, setRegUsername] = useState('');
  const [regPassword, setRegPassword] = useState('');
  const [regErr, setRegErr] = useState(NO_REG_ERROR);
  const [regBusy, setRegBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!username || !password || busy) return;
    setBusy(true);
    setErr('');
    setNotice('');
    authStore.clear();
    try {
      await api.login(username, password);
      const { user } = await api.currentUser();
      onLogin(user);
    } catch (e2) {
      if (e2.status === 401) authStore.clear();
      setErr(e2.message || '登录失败');
    } finally {
      setBusy(false);
    }
  };

  const submitRegister = async (e) => {
    e.preventDefault();
    if (!regUsername || !regPassword || regBusy) return;
    setRegBusy(true);
    setRegErr(NO_REG_ERROR);
    authStore.clear();
    try {
      await api.register(regUsername, regPassword);
      // 注册成功直接登录进入，不再回登录页
      await api.login(regUsername, regPassword);
      const { user } = await api.currentUser();
      onLogin(user);
    } catch (e2) {
      if (e2.status === 401) authStore.clear();
      setRegErr(parseRegisterError(e2));
    } finally {
      setRegBusy(false);
    }
  };

  const loginView = (
    <form className="lcard" key="login" onSubmit={submit}>
      <h2>欢迎回来</h2>
      <div className="ld">登录后开始本体建模</div>
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
      {err && <div className="lerr">{err}</div>}
      <button className="lbtn" type="submit" disabled={busy}>
        {busy ? '登录中…' : '登 录'}
      </button>
      <div className="lswitch">
        还没有账号？
        <button
          type="button"
          className="llink"
          onClick={() => {
            setView('register');
            setRegErr(NO_REG_ERROR);
            setNotice('');
          }}
        >
          注册新账号
        </button>
      </div>
    </form>
  );

  const registerView = (
    <form className="lcard" key="register" onSubmit={submitRegister}>
      <h2>注册账号</h2>
      <div className="ld">创建你的本体建模工作台账号</div>
      <div className="lfield">
        <label htmlFor="register-username">用户名</label>
        <input
          id="register-username"
          value={regUsername}
          onChange={(e) => setRegUsername(e.target.value)}
          autoFocus
          autoComplete="username"
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
        />
      </div>
      {regErr.message && (
        <div className="lerr">
          {regErr.message}
          {regErr.violations.length > 0 && (
            <ul>
              {regErr.violations.map((v) => (
                <li key={v}>{v}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      <button className="lbtn" type="submit" disabled={regBusy}>
        {regBusy ? '注册并登录中…' : '注册并进入'}
      </button>
      <div className="lswitch">
        已有账号？
        <button
          type="button"
          className="llink"
          onClick={() => {
            setView('login');
            setErr('');
          }}
        >
          返回登录
        </button>
      </div>
    </form>
  );

  return (
    <div className="login-split">
      <div className="lbrand">
        <div className="lmk">本</div>
        <h1>本体建模工作台</h1>
        <div className="ltg">把领域业务知识整理成结构化、可验证的本体模型。</div>
        <div className="lfeats">
          {FEATS.map((feat, index) => (
            <div
              className="lfeat"
              key={feat.title}
              style={{ animationDelay: `${200 + index * 120}ms` }}
            >
              <div className="lfi">{feat.icon}</div>
              <div>
                <div className="lft">{feat.title}</div>
                <div className="lfd">{feat.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="lform">{view === 'register' ? registerView : loginView}</div>
    </div>
  );
}
