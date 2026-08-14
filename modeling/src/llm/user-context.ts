import { AsyncLocalStorage } from 'node:async_hooks';

interface LlmUserStore {
  username?: string;
}

const userStorage = new AsyncLocalStorage<LlmUserStore>();

/**
 * Returns the authenticated user associated with the current request/task.
 * CLI scripts and unit tests intentionally return undefined and use env defaults.
 */
export function currentLlmUser(): string | undefined {
  const username = userStorage.getStore()?.username?.trim();
  return username || undefined;
}

/** Run work with an explicit user scope (useful for workers and tests). */
export function runWithLlmUser<T>(username: string, work: () => T): T {
  const normalized = username.trim();
  if (!normalized) throw new Error('LLM 配置用户不能为空');
  return userStorage.run({ username: normalized }, work);
}

/** Give each Fastify request its own context before authentication runs. */
export function runWithLlmRequest<T>(work: () => T): T {
  return userStorage.run({}, work);
}

/** Attach the verified user to the current request context. */
export function enterLlmUser(username: string): void {
  const normalized = username.trim();
  if (!normalized) throw new Error('认证用户不能为空');
  const store = userStorage.getStore();
  if (store) store.username = normalized;
  else userStorage.enterWith({ username: normalized });
}
