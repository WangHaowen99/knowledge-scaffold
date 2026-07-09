import { config } from '../config.js';
import type { AuthUser } from '../types.js';

// AuthClient —— OIDC 形状的认证抽象（design.md 决策 3）。
// 当前 issuer=consumer：login 转发、verify 用 introspect（调 issuer 的 /auth/me）。
// 将来切独立鉴权服务 / 客户 IdP：新增 jwks 实现，改配置即可，路由与业务不动。
export interface AuthClient {
  login(username: string, password: string): Promise<{ token: string; user: AuthUser } | null>;
  verify(token: string): Promise<AuthUser | null>;
}

class ConsumerIntrospectAuthClient implements AuthClient {
  constructor(private issuer: string) {}

  async login(username: string, password: string) {
    try {
      const res = await fetch(`${this.issuer}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { token?: string; user?: { username: string; role: string } };
      if (!data.token || !data.user) return null;
      return { token: data.token, user: { username: data.user.username, role: data.user.role } };
    } catch {
      // issuer 不可达等价于登录失败（route 层返回 401）
      return null;
    }
  }

  async verify(token: string) {
    try {
      const res = await fetch(`${this.issuer}/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { user?: { username: string; role: string } };
      if (!data.user) return null;
      return { username: data.user.username, role: data.user.role };
    } catch {
      return null;
    }
  }
}

let _client: AuthClient | null = null;

export function getAuthClient(): AuthClient {
  if (_client) return _client;
  if (config.auth.verifyMode === 'jwks') {
    // 预留：本地验签实现（M1 不需要）。
    throw new Error('jwks 验证模式尚未实现，请使用 introspect');
  }
  _client = new ConsumerIntrospectAuthClient(config.auth.issuerUrl);
  return _client;
}
