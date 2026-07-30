import { getAuthClient, type RefreshResult } from '../auth/client.js';

type RefreshFn = (refreshToken: string) => Promise<RefreshResult | null>;

export interface EvaluationAuthSessionOptions {
  accessToken: string;
  refreshToken?: string;
  refresh?: RefreshFn;
  now?: () => number;
  refreshSkewMs?: number;
}

function accessTokenExpiryMs(token: string): number | undefined {
  try {
    const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString()) as { exp?: unknown };
    const exp = Number(payload.exp);
    return Number.isFinite(exp) ? exp * 1_000 : undefined;
  } catch {
    return undefined;
  }
}

/** Keeps rotating credentials in memory for one background evaluation round. */
export class EvaluationAuthSession {
  private accessToken: string;
  private refreshToken?: string;
  private readonly refresh: RefreshFn;
  private readonly now: () => number;
  private readonly refreshSkewMs: number;
  private refreshInFlight?: Promise<string>;

  constructor(options: EvaluationAuthSessionOptions) {
    this.accessToken = options.accessToken;
    this.refreshToken = options.refreshToken?.trim() || undefined;
    this.refresh = options.refresh ?? ((token) => getAuthClient().refresh(token));
    this.now = options.now ?? Date.now;
    this.refreshSkewMs = options.refreshSkewMs ?? 60_000;
  }

  async getAccessToken(): Promise<string> {
    const expiresAt = accessTokenExpiryMs(this.accessToken);
    if (this.refreshToken && expiresAt !== undefined && expiresAt - this.now() <= this.refreshSkewMs) {
      return this.rotate();
    }
    return this.accessToken;
  }

  async refreshAfterUnauthorized(): Promise<string> {
    if (!this.refreshToken) {
      throw new Error('评测访问凭据已过期，请重新登录后再启动评测');
    }
    return this.rotate();
  }

  private rotate(): Promise<string> {
    if (!this.refreshInFlight) {
      this.refreshInFlight = (async () => {
        const refreshToken = this.refreshToken;
        if (!refreshToken) {
          throw new Error('评测访问凭据已过期，请重新登录后再启动评测');
        }
        const result = await this.refresh(refreshToken);
        if (!result?.token || !result.refreshToken) {
          throw new Error('评测登录续期已失效，请重新登录后再启动评测');
        }
        this.accessToken = result.token;
        this.refreshToken = result.refreshToken;
        return this.accessToken;
      })().finally(() => {
        this.refreshInFlight = undefined;
      });
    }
    return this.refreshInFlight;
  }
}
