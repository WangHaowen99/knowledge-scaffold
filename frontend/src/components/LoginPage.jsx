import { useState } from 'react';

function LoginPage({ onLogin, loading = false, error = null }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const handleSubmit = (event) => {
    event.preventDefault();
    if (!username || !password || loading) return;
    onLogin(username.trim(), password);
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-header">
          <div className="login-brand">本体消费智能体</div>
          <p className="login-subtitle">请使用账号密码登录后继续</p>
        </div>
        <form className="login-form" onSubmit={handleSubmit}>
          <label className="login-field">
            <span>账号</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="请输入账号"
              autoComplete="username"
            />
          </label>
          <label className="login-field">
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="请输入密码"
              autoComplete="current-password"
            />
          </label>
          {error && <div className="login-error">{error}</div>}
          <button className="btn login-submit" type="submit" disabled={loading || !username || !password}>
            {loading ? '登录中...' : '登录'}
          </button>
        </form>
      </div>
    </div>
  );
}

export default LoginPage;
