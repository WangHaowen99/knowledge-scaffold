import { resolve } from 'node:path';

// 服务配置：从环境变量读取（.env 由部署方提供，dev 下 tsx 不自动加载 .env，
// 用 `node --env-file=.env` 或 shell export；见 README）。
function env(key: string, fallback = ''): string {
  return process.env[key] ?? fallback;
}

export const config = {
  port: Number(env('PORT', '5100')),
  dataDir: resolve(env('DATA_DIR', './data')),

  llm: {
    baseUrl: env('MODELING_LLM_BASE_URL'),
    model: env('MODELING_LLM_MODEL'),
    apiKey: env('MODELING_LLM_API_KEY'),
  },
  taskLlm: {
    baseUrl: env('MODELING_TASK_LLM_BASE_URL'),
    model: env('MODELING_TASK_LLM_MODEL'),
    apiKey: env('MODELING_TASK_LLM_API_KEY'),
  },

  auth: {
    // 独立 auth-server 地址（OAuth2 /token + JWKS + /register）；容器内用服务名如 http://auth-server:5056
    issuerUrl: env('AUTH_ISSUER_URL', 'http://localhost:5056'),
    // 验签期望的 iss claim：容器内外地址可能不同（auth-server 默认签 http://auth-server:5056），
    // 与访问地址不一致时用 AUTH_JWT_ISSUER 单独指定
    jwtIssuer: env('AUTH_JWT_ISSUER') || env('AUTH_ISSUER_URL', 'http://localhost:5056'),
    jwtAudience: env('AUTH_JWT_AUDIENCE', 'meta-wise'),
  },

  consumerApiUrl: env('CONSUMER_API_URL', 'http://localhost:5055'),
  evalDbUrl: env('EVAL_DB_URL'),
};

export type AppConfig = typeof config;
