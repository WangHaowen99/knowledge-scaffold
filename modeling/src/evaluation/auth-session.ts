import { getAuthClient, type RefreshResult } from '../auth/client.js';

type RefreshFn = (refreshToken: string) => Promise<RefreshResult | null>;

export class EvaluationAuthenticationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'EvaluationAuthenticationError';
  }
}

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
  private revision = 0;

  constructor(options: EvaluationAuthSessionOptions) {
    this.accessToken = options.accessToken;
    this.refreshToken = options.refreshToken?.trim() || undefined;
    this.refresh = options.refresh ?? ((token) => getAuthClient().refresh(token));
    this.now = options.now ?? Date.now;
    this.refreshSkewMs = options.refreshSkewMs ?? 60_000;
  }

  async getAccessToken(): Promise<string> {
    const expiresAt = accessTokenExpiryMs(this.accessToken);
    if (expiresAt !== undefined && expiresAt - this.now() <= this.refreshSkewMs) {
      if (this.refreshToken) return this.rotate();
      throw new EvaluationAuthenticationError('评测访问凭据即将过期，请重新登录后再启动评测');
    }
    return this.accessToken;
  }

  async refreshAfterUnauthorized(): Promise<string> {
    if (!this.refreshToken) {
      throw new EvaluationAuthenticationError('评测访问凭据已过期，请重新登录后再启动评测');
    }
    return this.rotate();
  }

  /** Re-login updates the stable session object already held by a running evaluation. */
  updateCredentials(accessToken: string, refreshToken?: string): void {
    this.accessToken = accessToken;
    this.refreshToken = refreshToken?.trim() || undefined;
    this.revision += 1;
  }

  private rotate(): Promise<string> {
    if (!this.refreshInFlight) {
      const revision = this.revision;
      const source: Promise<string> = (async () => {
        const refreshToken = this.refreshToken;
        if (!refreshToken) {
          throw new EvaluationAuthenticationError('评测访问凭据已过期，请重新登录后再启动评测');
        }
        let result: RefreshResult | null;
        try {
          result = await this.refresh(refreshToken);
        } catch (error) {
          if (this.revision !== revision) return this.accessToken;
          throw error;
        }
        if (!result?.token || !result.refreshToken) {
          if (this.revision !== revision) return this.accessToken;
          throw new EvaluationAuthenticationError('评测登录续期已失效，请重新登录后再启动评测');
        }
        // A browser re-login may replace the credentials while refresh is in flight.
        // Never let the older refresh response overwrite that newer login session.
        if (this.revision === revision) {
          this.accessToken = result.token;
          this.refreshToken = result.refreshToken;
          this.revision += 1;
        }
        return this.accessToken;
      })();
      let rotation!: Promise<string>;
      rotation = source.finally(() => {
        if (this.refreshInFlight === rotation) this.refreshInFlight = undefined;
      });
      this.refreshInFlight = rotation;
    }
    return this.refreshInFlight;
  }
}
