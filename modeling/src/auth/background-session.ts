import { EvaluationAuthSession } from '../evaluation/auth-session.js';

const DEFAULT_MAX_IDLE_MS = 24 * 60 * 60 * 1_000;

interface StoredSession {
  session: EvaluationAuthSession;
  updatedAt: number;
}

/** Keeps renewable user credentials in Modeling Core memory, never in Agent or the database. */
export class BackgroundAuthSessionRegistry {
  private readonly sessions = new Map<string, StoredSession>();

  constructor(
    private readonly now: () => number = Date.now,
    private readonly maxIdleMs = DEFAULT_MAX_IDLE_MS,
  ) {}

  remember(username: string, accessToken: string, refreshToken?: string): EvaluationAuthSession {
    const key = username.trim();
    const existing = this.sessions.get(key);
    if (existing) {
      existing.session.updateCredentials(accessToken, refreshToken);
      existing.updatedAt = this.now();
      return existing.session;
    }
    const session = new EvaluationAuthSession({ accessToken, refreshToken });
    this.sessions.set(key, { session, updatedAt: this.now() });
    return session;
  }

  get(username: string): EvaluationAuthSession | undefined {
    const key = username.trim();
    const stored = this.sessions.get(key);
    if (!stored) return undefined;
    if (this.now() - stored.updatedAt > this.maxIdleMs) {
      this.sessions.delete(key);
      return undefined;
    }
    return stored.session;
  }

  forget(username: string): void {
    this.sessions.delete(username.trim());
  }

  clear(): void {
    this.sessions.clear();
  }
}

export const backgroundAuthSessions = new BackgroundAuthSessionRegistry();
