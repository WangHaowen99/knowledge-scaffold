import { getLlmDefaultSettings, getPublicLlmDefaultSettings } from './llm-defaults-repo.js';
import {
  clearNewUserLlmConfiguration,
  initializeUserLlmDefaultSnapshots,
  replaceUserLlmWithDefaultSnapshots,
} from './llm-default-snapshots-repo.js';
import { LlmConfigEncryptionKeyError } from './llm-secret-codec.js';

export class LlmDefaultSyncError extends Error {
  constructor(message: string, readonly statusCode: 409 | 503) {
    super(message);
    this.name = 'LlmDefaultSyncError';
  }
}

export function isPlatformLlmDefaultAvailable(): boolean {
  return Boolean(getPublicLlmDefaultSettings('main'));
}

function currentPlatformDefaults(required: boolean) {
  try {
    const main = getLlmDefaultSettings('main');
    if (!main && required) {
      throw new LlmDefaultSyncError('管理员尚未配置平台默认 LLM', 409);
    }
    if (!main) return undefined;
    return { main, task: getLlmDefaultSettings('task') };
  } catch (error) {
    if (error instanceof LlmConfigEncryptionKeyError) {
      throw new LlmDefaultSyncError(`平台默认 LLM 暂不可用：${error.message}`, 503);
    }
    throw error;
  }
}

/** 注册成功是新账号边界：先丢弃同名旧数据，再固化当时模板；没有模板时使用部署配置。 */
export function initializeNewUserLlmDefaults(username: string): boolean {
  // 即使模板密钥损坏，成功注册的新账号也绝不能读取同名旧账号遗留的连接凭据。
  clearNewUserLlmConfiguration(username);
  const defaults = currentPlatformDefaults(false);
  if (!defaults) return false;
  return initializeUserLlmDefaultSnapshots(username, defaults);
}

/** 用当前模板原子替换发起用户的 LLM 配置；之后 admin 修改不会追写该快照。 */
export function syncUserLlmToPlatformDefault(username: string): void {
  replaceUserLlmWithDefaultSnapshots(username, currentPlatformDefaults(true)!);
}
