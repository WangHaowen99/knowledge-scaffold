import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { Alert, Button, Input, Typography } from 'antd';
import { useState } from 'react';
import { api } from '../lib/api.js';

export default function LoginPage({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (!username || !password) return;
    setLoading(true);
    setError('');
    try {
      await api.login(username, password);
      onLogin();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-wrap">
      <div className="login-card msg-in msg-left">
        <div className="home-title" style={{ justifyContent: 'center', marginBottom: 4 }}>
          <span className="brand-dot" />
          <Typography.Title level={4} style={{ margin: 0 }}>
            本体建模工作台
          </Typography.Title>
        </div>
        <Typography.Paragraph type="secondary" style={{ textAlign: 'center' }}>
          登录后开始建模
        </Typography.Paragraph>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        <Input
          size="large"
          prefix={<UserOutlined />}
          placeholder="用户名"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          style={{ marginBottom: 12 }}
        />
        <Input.Password
          size="large"
          prefix={<LockOutlined />}
          placeholder="密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onPressEnter={submit}
          style={{ marginBottom: 16 }}
        />
        <Button type="primary" size="large" block loading={loading} onClick={submit}>
          登录
        </Button>
      </div>
    </div>
  );
}
