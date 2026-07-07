import { useState } from 'react';
import { useAuthContext } from './useAuthContext';

export default function LoginPage() {
  const { login } = useAuthContext();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!username || !password || busy) return;
    setBusy(true);
    setError('');
    const result = await login(username, password);
    setBusy(false);
    if (!result.success) setError(result.error || '登录失败');
  };

  return (
    <div className="mwr login">
      <div className="lbrand">
        <div className="mk">M</div>
        <h1>meta-wise</h1>
        <div className="tg">企业本体驱动的 Agent 工作台。把领域知识包、业务数据和多 Agent 编排放进同一个工作空间。</div>
        <div className="feats">
          <div className="feat"><div className="fi">📦</div><div><div className="ft">知识包驱动</div><div className="fd">业务对象、操作与受控脚本按包导入，语义与口径可追溯</div></div></div>
          <div className="feat"><div className="fi">🔌</div><div><div className="ft">连接真实数据</div><div className="fd">对象经映射直查业务库，不编造字段与指标</div></div></div>
          <div className="feat"><div className="fi">🛡️</div><div><div className="ft">分层配置与权限</div><div className="fd">全局 / 空间 / 个人三层作用域，按角色最小授权</div></div></div>
        </div>
      </div>
      <div className="lform">
        <form className="lcard" onSubmit={submit}>
          <h2>欢迎回来</h2>
          <div className="ld">登录 meta-wise 控制台</div>
          <div className="lfield">
            <label>用户名</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" />
          </div>
          <div className="lfield">
            <label>密码</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
          </div>
          {error && <div className="lerr">{error}</div>}
          <button className="lbtn" type="submit" disabled={busy}>{busy ? '登录中…' : '登 录'}</button>
          <div className="lnote">首次登录将要求修改初始密码 · 登录即代表同意平台使用规范</div>
        </form>
      </div>
    </div>
  );
}
