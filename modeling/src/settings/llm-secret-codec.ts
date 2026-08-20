import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { config } from '../config.js';

const FORMAT_PREFIX = 'enc:v1';
const IV_BYTES = 12;
export const MANAGED_LLM_CONFIG_ENCRYPTION_KEY_FILE = '.llm-config-encryption-key';

export class LlmConfigEncryptionKeyError extends Error {
  constructor(message = '平台 LLM API Key 加密密钥不可用') {
    super(message);
    this.name = 'LlmConfigEncryptionKeyError';
  }
}

function hasErrorCode(error: unknown, code: string): boolean {
  return error instanceof Error
    && 'code' in error
    && (error as NodeJS.ErrnoException).code === code;
}

function validateMasterKey(masterKey: string, source: string): string {
  if (Buffer.byteLength(masterKey, 'utf8') < 32) {
    throw new LlmConfigEncryptionKeyError(`${source}必须至少包含 32 字节`);
  }
  return masterKey;
}

function managedKeyPath(): string {
  return join(config.dataDir, MANAGED_LLM_CONFIG_ENCRYPTION_KEY_FILE);
}

function readManagedMasterKey(): string | undefined {
  try {
    return readFileSync(managedKeyPath(), 'utf8').trim();
  } catch (error) {
    if (hasErrorCode(error, 'ENOENT')) return undefined;
    throw new LlmConfigEncryptionKeyError('无法读取 DATA_DIR 中自动管理的 LLM 加密密钥');
  }
}

function managedMasterKey(createIfMissing: boolean): string {
  const existing = readManagedMasterKey();
  if (existing !== undefined) return validateMasterKey(existing, 'DATA_DIR 自动管理的 LLM 加密密钥');
  if (!createIfMissing) {
    throw new LlmConfigEncryptionKeyError('DATA_DIR 中自动管理的 LLM 加密密钥缺失，无法解密平台 LLM API Key');
  }

  try {
    mkdirSync(config.dataDir, { recursive: true, mode: 0o700 });
  } catch {
    throw new LlmConfigEncryptionKeyError('无法创建 DATA_DIR 以保存自动管理的 LLM 加密密钥');
  }

  const generated = randomBytes(32).toString('base64url');
  try {
    writeFileSync(managedKeyPath(), generated, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
    return generated;
  } catch (error) {
    if (hasErrorCode(error, 'EEXIST')) {
      const concurrentlyCreated = readManagedMasterKey();
      if (concurrentlyCreated !== undefined) {
        return validateMasterKey(concurrentlyCreated, 'DATA_DIR 自动管理的 LLM 加密密钥');
      }
    }
    throw new LlmConfigEncryptionKeyError('无法写入 DATA_DIR 自动管理的 LLM 加密密钥');
  }
}

export function llmConfigEncryptionKeyMode(): 'environment' | 'managed' {
  return config.llmConfigEncryptionKey.trim() ? 'environment' : 'managed';
}

function encryptionKey(createManagedKey: boolean): Buffer {
  const configured = config.llmConfigEncryptionKey;
  const masterKey = configured.trim()
    ? validateMasterKey(configured, 'MODELING_LLM_CONFIG_ENCRYPTION_KEY ')
    : managedMasterKey(createManagedKey);
  return createHash('sha256').update(masterKey, 'utf8').digest();
}

function additionalData(purpose: string): Buffer {
  return Buffer.from(`modeling.llm_default_settings.${purpose}.api_key`, 'utf8');
}

/** 使用 AES-256-GCM 加密平台默认 Key，密文格式包含版本以便后续轮换算法。 */
export function encryptLlmDefaultApiKey(apiKey: string, purpose: string): string | null {
  if (!apiKey) return null;
  const iv = randomBytes(IV_BYTES);
  const cipher = createCipheriv('aes-256-gcm', encryptionKey(true), iv);
  cipher.setAAD(additionalData(purpose));
  const ciphertext = Buffer.concat([cipher.update(apiKey, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return [FORMAT_PREFIX, iv.toString('base64url'), tag.toString('base64url'), ciphertext.toString('base64url')].join(':');
}

export function decryptLlmDefaultApiKey(ciphertext: string | null, purpose: string): string {
  if (!ciphertext) return '';
  const parts = ciphertext.split(':');
  if (parts.length !== 5 || `${parts[0]}:${parts[1]}` !== FORMAT_PREFIX) {
    throw new LlmConfigEncryptionKeyError('平台 LLM API Key 密文格式无效');
  }
  try {
    const decipher = createDecipheriv('aes-256-gcm', encryptionKey(false), Buffer.from(parts[2], 'base64url'));
    decipher.setAAD(additionalData(purpose));
    decipher.setAuthTag(Buffer.from(parts[3], 'base64url'));
    return Buffer.concat([
      decipher.update(Buffer.from(parts[4], 'base64url')),
      decipher.final(),
    ]).toString('utf8');
  } catch (error) {
    if (error instanceof LlmConfigEncryptionKeyError) throw error;
    throw new LlmConfigEncryptionKeyError('无法解密平台 LLM API Key，请检查部署加密密钥是否与写入时一致');
  }
}
