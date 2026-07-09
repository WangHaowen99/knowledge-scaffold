import { useState } from 'react';
import { login, setToken } from './api.js';

export default function LoginPage({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!username || !password || busy) return;
    setBusy(true); setErr('');
    try {
      const data = await login(username, password);
      setToken(data.token);
      onLogin(data.user);
    } catch (e2) {
      setErr(e2.message || '登录失败');
    } finally {
      setBusy(false);
    }
  };

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
        <form className="lcard" onSubmit={submit}>
          <h2>欢迎回来</h2>
          <div className="ld">使用知识工程平台账号登录</div>
          <div className="field"><label>用户名</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" /></div>
          <div className="field"><label>密码</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></div>
          {err && <div className="lerr">{err}</div>}
          <button className="lbtn" type="submit" disabled={busy}>{busy ? '登录中…' : '登 录'}</button>
        </form>
      </div>
    </div>
  );
}
