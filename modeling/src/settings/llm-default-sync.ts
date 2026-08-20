import { getLlmDefaultSettings, getPublicLlmDefaultSettings } from './llm-defaults-repo.js';
import {
  clearNewUserLlmConfiguration,
  initializeUserLlmDefaultSnapshots,
  replaceUserLlmPurposeWithDefaultSnapshot,
  replaceUserLlmWithDefaultSnapshots,
} from './llm-default-snapshots-repo.js';
import { LlmConfigEncryptionKeyError } from './llm-secret-codec.js';
import type { LlmPurpose } from './llm-defaults-repo.js';

export class LlmDefaultSyncError extends Error {
  constructor(message: string, readonly statusCode: 409 | 503) {
    super(message);
    this.name = 'LlmDefaultSyncError';
  }
}

export function isPlatformLlmDefaultAvailable(purpose: LlmPurpose = 'main'): boolean {
  return Boolean(
    getPublicLlmDefaultSettings(purpose)
      ?? (purpose === 'task' ? getPublicLlmDefaultSettings('main') : undefined),
  );
}

function missingDefaultMessage(purpose: LlmPurpose): string {
  return purpose === 'main'
    ? '管理员尚未配置平台默认主模型'
    : '管理员尚未配置平台默认后台任务模型';
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

function currentPlatformDefault(purpose: LlmPurpose) {
  try {
    const settings = getLlmDefaultSettings(purpose)
      ?? (purpose === 'task' ? getLlmDefaultSettings('main') : undefined);
    if (!settings) throw new LlmDefaultSyncError(missingDefaultMessage(purpose), 409);
    return settings;
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
export function syncUserLlmToPlatformDefault(username: string, purpose?: LlmPurpose): void {
  if (purpose) {
    replaceUserLlmPurposeWithDefaultSnapshot(username, purpose, currentPlatformDefault(purpose));
    return;
  }
  replaceUserLlmWithDefaultSnapshots(username, currentPlatformDefaults(true)!);
}
