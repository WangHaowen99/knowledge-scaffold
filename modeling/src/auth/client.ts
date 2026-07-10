import { createRemoteJWKSet, jwtVerify } from 'jose';
import { config } from '../config.js';
import type { AuthUser } from '../types.js';

// AuthClient —— 直连独立 auth-server（design.md 决策 3）：
// 登录/续期走 OAuth2 /token（password/refresh grant），校验用 JWKS 本地 RS256 验签，
// 注册转发 auth-server 的 /register*。issuer 固定指向 auth-server。
export interface LoginResult {
  token: string;
  user: AuthUser;
  refreshToken?: string;
  expiresIn?: number;
}

export interface RefreshResult {
  token: string;
  refreshToken: string;
  expiresIn?: number;
}

// 注册代理：上游状态码 + body 原样透传给路由层
export interface ProxyResult {
  status: number;
  body: unknown;
}

// 强制改密用户禁止进入本服务（modeling 无改密入口，须先在 issuer 侧完成改密），
// 与 consumer require_auth 对 password_change_required 的 403 拦截语义对齐
export class PasswordChangeRequiredError extends Error {
  constructor() {
    super('该账号需要先修改初始密码：请在知识工程平台完成改密后再登录');
  }
}

export interface AuthClient {
  // null=凭据被拒（路由→401），PasswordChangeRequiredError→403，其他抛错=上游不可达/配置错（路由→503）
  login(username: string, password: string): Promise<LoginResult | null>;
  verify(token: string): Promise<AuthUser | null>;
  // null=上游拒绝（路由→401），抛错=上游不可达（路由→503）
  refresh(refreshToken: string): Promise<RefreshResult | null>;
  // 抛错=网络错（路由→502）
  register(body: unknown, clientIp: string): Promise<ProxyResult>;
  registerVerify(body: unknown, clientIp: string): Promise<ProxyResult>;
  registerResend(body: unknown, clientIp: string): Promise<ProxyResult>;
}

const FETCH_TIMEOUT_MS = 10_000;

async function postJson(url: string, body: unknown, headers: Record<string, string> = {}) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
}

// 注册代理转发：状态码 + JSON body 透传；上游响应非 JSON 时兜底但保留状态码。
// fetch 网络异常向上抛，由路由映射 502。
async function proxyPost(url: string, body: unknown, clientIp: string): Promise<ProxyResult> {
  const res = await postJson(url, body ?? {}, { 'X-Forwarded-For': clientIp });
  let payload: unknown;
  try {
    payload = await res.json();
  } catch {
    payload = { error: '注册服务响应异常' };
  }
  return { status: res.status, body: payload };
}

// 直连 auth-server：/token 签发、JWKS 本地 RS256 验签。
// 构造函数注入配置使类零全局依赖（可测），getAuthClient 负责从 config 装配。
export class AuthServerJwksClient implements AuthClient {
  private issuerUrl: string;
  private jwtIssuer: string;
  private audience: string;
  // createRemoteJWKSet 自带缓存 / 未知 kid 重取 / 冷却，不另做缓存层
  private jwks: ReturnType<typeof createRemoteJWKSet>;

  constructor(opts: { issuerUrl: string; jwtIssuer: string; audience: string }) {
    this.issuerUrl = opts.issuerUrl.replace(/\/+$/, '');
    this.jwtIssuer = opts.jwtIssuer;
    this.audience = opts.audience;
    this.jwks = createRemoteJWKSet(new URL(`${this.issuerUrl}/jwks.json`));
  }

  async login(username: string, password: string): Promise<LoginResult | null> {
    const res = await postJson(`${this.issuerUrl}/token`, {
      grant_type: 'password', username, password,
    });
    // 凭据被拒（invalid_grant）→null（路由 401）；上游 5xx/不可达→抛错（路由 503），不吞成登录失败
    if (res.status === 400 || res.status === 401) return null;
    if (!res.ok) throw new Error(`auth-server /token 返回 ${res.status}`);
    const data = (await res.json()) as {
      access_token?: string; refresh_token?: string; expires_in?: number;
    };
    if (!data.access_token) return null;
    // 刚签发的 token 立即走本地验签同一条路径，顺带预热 JWKS；
    // 签发成功却验签失败只可能是 AUTH_JWT_ISSUER/AUTH_JWT_AUDIENCE 配置错——抛错暴露而非静默 401
    const user = await this.verify(data.access_token);
    if (!user) {
      if (this.isPasswordChangeRequired(data.access_token)) throw new PasswordChangeRequiredError();
      throw new Error('access token 本地验签失败：检查 AUTH_JWT_ISSUER / AUTH_JWT_AUDIENCE 是否与 auth-server 一致');
    }
    return {
      token: data.access_token,
      user,
      refreshToken: data.refresh_token,
      expiresIn: data.expires_in,
    };
  }

  async verify(token: string): Promise<AuthUser | null> {
    try {
      const { payload } = await jwtVerify(token, this.jwks, {
        algorithms: ['RS256'],
        issuer: this.jwtIssuer,
        audience: this.audience,
        requiredClaims: ['sub', 'exp'],
      });
      // refresh token 不得当访问凭据
      if (payload.typ !== 'access') return null;
      // 强制改密用户禁止进入业务路由（modeling 无改密入口，拦截职责在依赖方，见 consumer ADR D3/D4）
      if (payload.pwd_cr === true) return null;
      return { username: String(payload.sub), role: String(payload.role ?? 'user') };
    } catch {
      return null;
    }
  }

  // 登录失败归因用：区分「验签不过」与「验签能过但被 pwd_cr 拦下」，给前端明确改密提示
  private isPasswordChangeRequired(token: string): boolean {
    try {
      const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
      return payload.pwd_cr === true;
    } catch {
      return false;
    }
  }

  async refresh(refreshToken: string): Promise<RefreshResult | null> {
    const res = await postJson(`${this.issuerUrl}/token`, {
      grant_type: 'refresh_token', refresh_token: refreshToken,
    });
    // auth-server 对无效 refresh 返回 400 invalid_grant；401 同义处理
    if (res.status === 400 || res.status === 401) return null;
    if (!res.ok) throw new Error(`auth-server /token 返回 ${res.status}`);
    const data = (await res.json()) as {
      access_token: string; refresh_token: string; expires_in?: number;
    };
    return { token: data.access_token, refreshToken: data.refresh_token, expiresIn: data.expires_in };
  }

  // 注册端点在 auth-server 上无 /auth 前缀
  register(body: unknown, clientIp: string) {
    return proxyPost(`${this.issuerUrl}/register`, body, clientIp);
  }

  registerVerify(body: unknown, clientIp: string) {
    return proxyPost(`${this.issuerUrl}/register/verify`, body, clientIp);
  }

  registerResend(body: unknown, clientIp: string) {
    return proxyPost(`${this.issuerUrl}/register/resend`, body, clientIp);
  }
}

let _client: AuthClient | null = null;

export function getAuthClient(): AuthClient {
  if (_client) return _client;
  _client = new AuthServerJwksClient({
    issuerUrl: config.auth.issuerUrl,
    jwtIssuer: config.auth.jwtIssuer,
    audience: config.auth.jwtAudience,
  });
  return _client;
}
