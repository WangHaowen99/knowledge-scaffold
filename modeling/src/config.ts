import { resolve } from 'node:path';

// 服务配置：从环境变量读取（.env 由部署方提供，dev 下 tsx 不自动加载 .env，
// 用 `node --env-file=.env` 或 shell export；见 README）。
function env(key: string, fallback = ''): string {
  return process.env[key] ?? fallback;
}

export const DEFAULT_LLM_MAX_TOKENS = 128_000;
export const DEFAULT_LLM_CONTEXT_WINDOW = 1_000_000;

function positiveIntegerEnv(key: string, fallback: number): number {
  const value = Number(env(key, String(fallback)));
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function normalizeMcpTransportEnv(key: string, fallback: 'both' | 'streamable-http' | 'sse'): 'both' | 'streamable-http' | 'sse' {
  const value = env(key, fallback).trim().toLowerCase();
  if (value === 'http') return 'streamable-http';
  if (value === 'both' || value === 'streamable-http' || value === 'sse') return value;
  return fallback;
}

const evaluationLlmModel = env('MODELING_EVALUATION_LLM_MODEL');

export const config = {
  port: Number(env('PORT', '5100')),
  dataDir: resolve(env('DATA_DIR', './data')),

  llm: {
    baseUrl: env('MODELING_LLM_BASE_URL'),
    model: env('MODELING_LLM_MODEL'),
    apiKey: env('MODELING_LLM_API_KEY'),
    // maxTokens 是输出上限；contextWindow 是输入+输出总上下文窗口，二者独立配置。
    maxTokens: positiveIntegerEnv('MODELING_LLM_MAX_TOKENS', DEFAULT_LLM_MAX_TOKENS),
    contextWindow: positiveIntegerEnv('MODELING_LLM_CONTEXT_WINDOW', DEFAULT_LLM_CONTEXT_WINDOW),
  },
  taskLlm: {
    baseUrl: env('MODELING_TASK_LLM_BASE_URL'),
    model: env('MODELING_TASK_LLM_MODEL'),
    apiKey: env('MODELING_TASK_LLM_API_KEY'),
  },
  // 显式部署密钥；为空时由 codec 在 DATA_DIR 自动生成持久化密钥，不得写入数据库或返回前端。
  llmConfigEncryptionKey: env('MODELING_LLM_CONFIG_ENCRYPTION_KEY'),

  auth: {
    // 独立 auth-server 地址（OAuth2 /token + JWKS + /register）；容器内用服务名如 http://auth-server:5056
    issuerUrl: env('AUTH_ISSUER_URL', 'http://localhost:5056'),
    // 验签期望的 iss claim：容器内外地址可能不同（auth-server 默认签 http://auth-server:5056），
    // 与访问地址不一致时用 AUTH_JWT_ISSUER 单独指定
    jwtIssuer: env('AUTH_JWT_ISSUER') || env('AUTH_ISSUER_URL', 'http://localhost:5056'),
    jwtAudience: env('AUTH_JWT_AUDIENCE', 'meta-wise'),
  },

  consumerApiUrl: env('CONSUMER_API_URL', 'http://localhost:5055'),
  evaluationModelProfileRef: env('MODELING_EVALUATION_MODEL_PROFILE_REF'),
  evaluationManagedModelProfile: evaluationLlmModel ? {
    provider: env('MODELING_EVALUATION_LLM_PROVIDER', 'openai_compatible'),
    baseUrl: env('MODELING_EVALUATION_LLM_BASE_URL') || env('MODELING_LLM_BASE_URL'),
    model: evaluationLlmModel,
    apiKey: env('MODELING_EVALUATION_LLM_API_KEY') || env('MODELING_LLM_API_KEY'),
    timeout: positiveIntegerEnv('MODELING_EVALUATION_LLM_TIMEOUT', 600),
    maxTokens: positiveIntegerEnv('MODELING_EVALUATION_LLM_MAX_TOKENS', 8192),
    temperature: 0.2,
  } : undefined,
  agentBaseUrl: env('MODELING_AGENT_BASE_URL', 'http://localhost:5110').replace(/\/+$/, ''),
  jiuwenboxSocketPath: env('JIUWENBOX_SOCKET_PATH', '/run/jiuwenbox/jiuwenbox.sock'),
  modelingServiceToken: env('MODELING_SERVICE_TOKEN'),
  evalDbUrl: env('EVAL_DB_URL'),
  evalDbAdminUrl: env('EVAL_DB_ADMIN_URL'),
  evalBinding: {
    provider: env('MODELING_EVAL_BINDING_PROVIDER', 'nl2sql_template'),
    url: env('MODELING_EVAL_BINDING_URL'),
    token: env('MODELING_EVAL_BINDING_TOKEN'),
    scene: env('MODELING_EVAL_BINDING_SCENE'),
  },
  mcp: {
    host: env('MODELING_MCP_HOST', '0.0.0.0'),
    port: Number(env('MODELING_MCP_PORT', '5188')),
    transport: normalizeMcpTransportEnv('MODELING_MCP_TRANSPORT', 'both'),
    publicBaseUrl: env('MODELING_MCP_PUBLIC_BASE_URL').replace(/\/+$/, ''),
    tokenSecret: env('MODELING_MCP_TOKEN_SECRET'),
    tokenIssuer: env('MODELING_MCP_TOKEN_ISSUER', 'knowledge-scaffold-modeling'),
    tokenAudience: env('MODELING_MCP_TOKEN_AUDIENCE', 'knowledge-scaffold-modeling-mcp'),
    defaultTokenTtlSeconds: positiveIntegerEnv('MODELING_MCP_TOKEN_TTL_SECONDS', 86_400),
    maxTokenTtlSeconds: positiveIntegerEnv('MODELING_MCP_TOKEN_MAX_TTL_SECONDS', 2_592_000),
    protocolVersion: env('MODELING_MCP_PROTOCOL_VERSION', '2026-07-28'),
    legacyProtocolVersion: env('MODELING_MCP_LEGACY_PROTOCOL_VERSION', '2025-11-25'),
  },
};

export type AppConfig = typeof config;
