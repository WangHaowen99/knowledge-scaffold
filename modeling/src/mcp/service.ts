import { randomBytes, randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import { errors as joseErrors, jwtVerify, SignJWT } from 'jose';

import { config } from '../config.js';
import { getDb, nowIso } from '../db/index.js';
import { canReadProject, canWriteProject, getProject } from '../projects/repo.js';

export type ModelingMcpToolset = 'read' | 'change';
export type ModelingMcpTransportMode = 'both' | 'streamable-http' | 'sse';
export type ModelingMcpHttpTransport = 'streamable-http' | 'sse';

export type ModelingMcpScope =
  | 'ontology.read'
  | 'ontology.change.validate'
  | 'ontology.change.apply';

export const MODELING_MCP_SERVER_INFO = {
  name: 'knowledge-scaffold-modeling-mcp',
  version: '0.2.0',
} as const;

export interface ProjectMcpEndpoint {
  projectId: string;
  endpointId: string;
  createdAt: string;
  updatedAt: string;
}

export interface ProjectMcpTokenSummary {
  id: string;
  projectId: string;
  name: string;
  subjectUsername: string;
  issuedBy: string;
  scopes: ModelingMcpScope[];
  createdAt: string;
  expiresAt: string;
  lastUsedAt: string | null;
  revokedAt: string | null;
  revokedBy: string | null;
}

export interface ProjectMcpTokenIssueResult {
  endpoint: ProjectMcpEndpoint;
  token: string;
  tokenSummary: ProjectMcpTokenSummary;
}

export interface VerifiedProjectMcpToken {
  endpoint: ProjectMcpEndpoint;
  tokenId: string;
  projectId: string;
  username: string;
  scopes: ModelingMcpScope[];
}

export interface ProjectMcpRuntimeStatus {
  enabled: boolean;
  configured: true;
  running: boolean;
  detail: string;
  project_id: string;
  endpoint_id: string;
  transport: ModelingMcpTransportMode;
  server_name: string;
  server_version: string;
  protocol_versions: string[];
  base_url: string;
  scope_catalog: Array<{
    scope: ModelingMcpScope;
    description: string;
  }>;
  token_policy: {
    min_ttl_seconds: number;
    default_ttl_seconds: number;
    max_ttl_seconds: number;
  };
  urls: Array<{
    toolset: ModelingMcpToolset;
    transport: ModelingMcpHttpTransport;
    url: string;
  }>;
  toolsets: Array<{
    name: ModelingMcpToolset;
    required_scopes: ModelingMcpScope[];
    tools: string[];
  }>;
}

interface ProjectMcpEndpointRow {
  project_id: string;
  endpoint_id: string;
  created_at: string;
  updated_at: string;
}

interface ProjectMcpTokenRow {
  id: string;
  project_id: string;
  token_name: string;
  subject_username: string;
  issued_by: string;
  scopes_json: string;
  expires_at: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
}

interface ModelingMcpRuntimeRow {
  instance_id: string;
  status: 'running' | 'stopped';
  started_at: string;
  heartbeat_at: string;
  detail: string | null;
}

const ENDPOINT_ID_PATTERN = /^[0-9a-f]{16}$/u;
const VALID_SCOPES: readonly ModelingMcpScope[] = [
  'ontology.read',
  'ontology.change.validate',
  'ontology.change.apply',
] as const;
const DEFAULT_SCOPES: readonly ModelingMcpScope[] = ['ontology.read'];
const MCP_RUNTIME_STALE_MS = 30_000;
const MCP_TOKEN_SECRET_MIN_CHARS = 32;
const TOOLSET_SCOPE_REQUIREMENTS: Record<ModelingMcpToolset, readonly ModelingMcpScope[]> = {
  read: ['ontology.read'],
  change: ['ontology.change.validate', 'ontology.change.apply'],
};

let signingSecretCache: Uint8Array | null = null;

export class ModelingMcpError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly statusCode = 400,
  ) {
    super(message);
    this.name = 'ModelingMcpError';
  }
}

export function ensureProjectMcpEndpoint(projectId: string): ProjectMcpEndpoint {
  const current = projectMcpEndpoint(projectId);
  if (current) return current;
  const now = nowIso();
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    const existing = projectMcpEndpoint(projectId);
    if (existing) {
      db.exec('COMMIT');
      return existing;
    }
    while (true) {
      const endpointId = randomBytes(8).toString('hex');
      if (!ENDPOINT_ID_PATTERN.test(endpointId)) continue;
      try {
        db.prepare(`INSERT INTO project_mcp_endpoints
          (project_id, endpoint_id, created_at, updated_at)
          VALUES (?,?,?,?)`)
          .run(projectId, endpointId, now, now);
        db.exec('COMMIT');
        return {
          projectId,
          endpointId,
          createdAt: now,
          updatedAt: now,
        };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (!message.includes('UNIQUE constraint failed')) throw error;
      }
    }
  } catch (error) {
    if (db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
}

export function projectMcpEndpoint(projectId: string): ProjectMcpEndpoint | null {
  const row = getDb().prepare(`SELECT project_id, endpoint_id, created_at, updated_at
    FROM project_mcp_endpoints WHERE project_id = ?`).get(projectId) as ProjectMcpEndpointRow | undefined;
  return row ? endpointFromRow(row) : null;
}

export function projectMcpEndpointById(endpointId: string): ProjectMcpEndpoint | null {
  const normalized = String(endpointId ?? '').trim();
  if (!ENDPOINT_ID_PATTERN.test(normalized)) return null;
  const row = getDb().prepare(`SELECT project_id, endpoint_id, created_at, updated_at
    FROM project_mcp_endpoints WHERE endpoint_id = ?`).get(normalized) as ProjectMcpEndpointRow | undefined;
  return row ? endpointFromRow(row) : null;
}

export async function issueProjectMcpToken(input: {
  projectId: string;
  name: string;
  subjectUsername: string;
  issuedBy: string;
  scopes?: readonly string[] | null;
  ttlSeconds?: number;
}): Promise<ProjectMcpTokenIssueResult> {
  const project = getProject(input.projectId);
  if (!project) throw new ModelingMcpError('MCP_PROJECT_NOT_FOUND', '项目不存在', 404);
  const endpoint = ensureProjectMcpEndpoint(project.id);
  const name = normalizeTokenName(input.name);
  const scopes = normalizeScopes(input.scopes);
  const ttlSeconds = normalizeTokenTtlSeconds(input.ttlSeconds);
  const createdAt = nowIso();
  const expiresAt = new Date(Date.parse(createdAt) + ttlSeconds * 1_000).toISOString();
  const tokenId = `mcp_${randomUUID().replaceAll('-', '')}`;

  const token = await new SignJWT({
    project_id: project.id,
    scopes,
  })
    .setProtectedHeader({ alg: 'HS256', typ: 'JWT' })
    .setIssuer(config.mcp.tokenIssuer)
    .setAudience(config.mcp.tokenAudience)
    .setSubject(input.subjectUsername)
    .setJti(tokenId)
    .setIssuedAt()
    .setExpirationTime(Math.floor(Date.parse(expiresAt) / 1_000))
    .sign(signingSecret());

  getDb().prepare(`INSERT INTO project_mcp_tokens (
    id, project_id, token_name, subject_username, issued_by, scopes_json,
    expires_at, created_at, last_used_at, revoked_at, revoked_by
  ) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL)`)
    .run(
      tokenId,
      project.id,
      name,
      input.subjectUsername,
      input.issuedBy,
      JSON.stringify(scopes),
      expiresAt,
      createdAt,
    );

  return {
    endpoint,
    token,
    tokenSummary: {
      id: tokenId,
      projectId: project.id,
      name,
      subjectUsername: input.subjectUsername,
      issuedBy: input.issuedBy,
      scopes,
      createdAt,
      expiresAt,
      lastUsedAt: null,
      revokedAt: null,
      revokedBy: null,
    },
  };
}

export function listProjectMcpTokens(projectId: string, options: { includeRevoked?: boolean } = {}): ProjectMcpTokenSummary[] {
  const rows = getDb().prepare(`SELECT
      id, project_id, token_name, subject_username, issued_by, scopes_json,
      expires_at, created_at, last_used_at, revoked_at, revoked_by
    FROM project_mcp_tokens
    WHERE project_id = ?
      ${options.includeRevoked ? '' : 'AND revoked_at IS NULL'}
    ORDER BY created_at DESC`).all(projectId) as unknown as ProjectMcpTokenRow[];
  return rows.map(tokenFromRow);
}

export function revokeProjectMcpToken(projectId: string, tokenId: string, username: string): boolean {
  const result = getDb().prepare(`UPDATE project_mcp_tokens
    SET revoked_at = ?, revoked_by = ?
    WHERE project_id = ? AND id = ? AND revoked_at IS NULL`)
    .run(nowIso(), username, projectId, tokenId);
  return Number(result.changes ?? 0) === 1;
}

export async function verifyProjectMcpToken(input: {
  bearerToken: string;
  endpointId: string;
  minimumAccess: 'read' | 'write';
}): Promise<VerifiedProjectMcpToken> {
  const endpoint = projectMcpEndpointById(input.endpointId);
  if (!endpoint) {
    throw new ModelingMcpError('MCP_ENDPOINT_NOT_FOUND', 'MCP endpoint 不存在', 404);
  }
  if (!input.bearerToken) {
    throw new ModelingMcpError('MCP_AUTH_REQUIRED', '缺少 Bearer Token', 401);
  }

  let payload: Record<string, unknown>;
  try {
    const verified = await jwtVerify(input.bearerToken, signingSecret(), {
      issuer: config.mcp.tokenIssuer,
      audience: config.mcp.tokenAudience,
    });
    payload = verified.payload as Record<string, unknown>;
  } catch (error) {
    if (error instanceof joseErrors.JWTExpired) {
      throw new ModelingMcpError('MCP_TOKEN_EXPIRED', 'MCP Token 已过期', 401);
    }
    throw new ModelingMcpError('MCP_TOKEN_INVALID', 'MCP Token 无效或已过期', 401);
  }

  const tokenId = String(payload.jti ?? '').trim();
  const projectId = String(payload.project_id ?? '').trim();
  const username = String(payload.sub ?? '').trim();
  if (!tokenId || !projectId || !username) {
    throw new ModelingMcpError('MCP_TOKEN_INVALID', 'MCP Token 缺少必要字段', 401);
  }
  if (projectId !== endpoint.projectId) {
    throw new ModelingMcpError('MCP_SCOPE_MISMATCH', 'MCP Token 与目标项目不匹配', 403);
  }

  const row = getDb().prepare(`SELECT
      id, project_id, token_name, subject_username, issued_by, scopes_json,
      expires_at, created_at, last_used_at, revoked_at, revoked_by
    FROM project_mcp_tokens WHERE id = ?`).get(tokenId) as ProjectMcpTokenRow | undefined;
  if (!row || row.project_id !== projectId || row.subject_username !== username) {
    throw new ModelingMcpError('MCP_TOKEN_UNKNOWN', 'MCP Token 未登记或已失效', 401);
  }
  if (row.revoked_at) {
    throw new ModelingMcpError('MCP_TOKEN_REVOKED', 'MCP Token 已吊销', 401);
  }
  if (Date.parse(row.expires_at) <= Date.now()) {
    throw new ModelingMcpError('MCP_TOKEN_EXPIRED', 'MCP Token 已过期', 401);
  }

  const scopes = tokenFromRow(row).scopes;
  const requiredAcl = input.minimumAccess === 'write'
    ? canWriteProject(projectId, username)
    : canReadProject(projectId, username);
  if (!requiredAcl) {
    throw new ModelingMcpError('MCP_PROJECT_FORBIDDEN', '当前用户已无权访问该项目', 403);
  }
  if (input.minimumAccess === 'read' && !scopes.includes('ontology.read')) {
    throw new ModelingMcpError('MCP_SCOPE_FORBIDDEN', 'MCP Token 不具备只读权限', 403);
  }
  if (input.minimumAccess === 'write'
    && !scopes.some((scope) => TOOLSET_SCOPE_REQUIREMENTS.change.includes(scope))) {
    throw new ModelingMcpError('MCP_SCOPE_FORBIDDEN', 'MCP Token 不具备变更权限', 403);
  }

  getDb().prepare('UPDATE project_mcp_tokens SET last_used_at = ? WHERE id = ?').run(nowIso(), tokenId);
  return {
    endpoint,
    tokenId,
    projectId,
    username,
    scopes,
  };
}

export function projectMcpRuntimeStatus(projectId: string, toolsByToolset: Record<ModelingMcpToolset, string[]>): ProjectMcpRuntimeStatus {
  const project = getProject(projectId);
  if (!project) throw new ModelingMcpError('MCP_PROJECT_NOT_FOUND', '项目不存在', 404);
  const endpoint = ensureProjectMcpEndpoint(project.id);
  const baseUrl = modelingMcpPublicBaseUrl();
  const transports = enabledModelingMcpHttpTransports();
  const runtime = modelingMcpRuntimeState();
  return {
    enabled: runtime.running,
    configured: true,
    running: runtime.running,
    detail: runtime.detail,
    project_id: project.id,
    endpoint_id: endpoint.endpointId,
    transport: config.mcp.transport,
    server_name: MODELING_MCP_SERVER_INFO.name,
    server_version: MODELING_MCP_SERVER_INFO.version,
    protocol_versions: [config.mcp.protocolVersion, config.mcp.legacyProtocolVersion],
    base_url: baseUrl,
    scope_catalog: VALID_SCOPES.map((scope) => ({
      scope,
      description: describeScope(scope),
    })),
    token_policy: {
      min_ttl_seconds: 300,
      default_ttl_seconds: config.mcp.defaultTokenTtlSeconds,
      max_ttl_seconds: config.mcp.maxTokenTtlSeconds,
    },
    urls: transports.flatMap((transport) => (
      (['read', 'change'] as ModelingMcpToolset[]).map((toolset) => ({
        toolset,
        transport,
        url: `${baseUrl}/${endpoint.endpointId}/${toolset}/${transport === 'sse' ? 'sse' : 'mcp'}`,
      }))
    )),
    toolsets: (['read', 'change'] as ModelingMcpToolset[]).map((toolset) => ({
      name: toolset,
      required_scopes: [...TOOLSET_SCOPE_REQUIREMENTS[toolset]],
      tools: toolsByToolset[toolset] ?? [],
    })),
  };
}

export function enabledModelingMcpHttpTransports(): ModelingMcpHttpTransport[] {
  if (config.mcp.transport === 'both') return ['streamable-http', 'sse'];
  return [config.mcp.transport];
}

export function modelingMcpTransportEnabled(transport: ModelingMcpHttpTransport): boolean {
  return enabledModelingMcpHttpTransports().includes(transport);
}

export function modelingMcpPublicBaseUrl(): string {
  if (config.mcp.publicBaseUrl) return config.mcp.publicBaseUrl;
  return `http://127.0.0.1:${config.mcp.port}`;
}

function tokenFromRow(row: ProjectMcpTokenRow): ProjectMcpTokenSummary {
  return {
    id: row.id,
    projectId: row.project_id,
    name: row.token_name,
    subjectUsername: row.subject_username,
    issuedBy: row.issued_by,
    scopes: normalizeScopes(parseScopesJson(row.scopes_json)),
    createdAt: row.created_at,
    expiresAt: row.expires_at,
    lastUsedAt: row.last_used_at ?? null,
    revokedAt: row.revoked_at ?? null,
    revokedBy: row.revoked_by ?? null,
  };
}

function endpointFromRow(row: ProjectMcpEndpointRow): ProjectMcpEndpoint {
  return {
    projectId: row.project_id,
    endpointId: row.endpoint_id,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function parseScopesJson(raw: string): string[] {
  try {
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function normalizeScopes(scopes: readonly string[] | null | undefined): ModelingMcpScope[] {
  if (Array.isArray(scopes) && scopes.length === 0) {
    throw new ModelingMcpError('MCP_SCOPE_INVALID', '至少选择一个 scope', 400);
  }
  const normalized = (Array.isArray(scopes) ? scopes : DEFAULT_SCOPES)
    .map((item) => String(item ?? '').trim())
    .filter(Boolean);
  const unique = [...new Set(normalized)];
  if (!unique.length) throw new ModelingMcpError('MCP_SCOPE_INVALID', '至少选择一个 scope', 400);
  const unknown = unique.filter((scope) => !VALID_SCOPES.includes(scope as ModelingMcpScope));
  if (unknown.length) {
    throw new ModelingMcpError('MCP_SCOPE_INVALID', `不支持的 scope: ${unknown.join(', ')}`, 400);
  }
  return unique as ModelingMcpScope[];
}

function normalizeTokenName(value: string): string {
  const name = String(value ?? '').trim();
  if (!name) throw new ModelingMcpError('MCP_TOKEN_NAME_REQUIRED', 'token 名称必填', 400);
  if (name.length > 80) throw new ModelingMcpError('MCP_TOKEN_NAME_TOO_LONG', 'token 名称最多 80 个字符', 400);
  return name;
}

function normalizeTokenTtlSeconds(value: number | undefined): number {
  if (value === undefined) return config.mcp.defaultTokenTtlSeconds;
  const ttl = Number(value);
  if (!Number.isSafeInteger(ttl) || ttl < 300) {
    throw new ModelingMcpError('MCP_TOKEN_TTL_INVALID', 'expires_in_seconds 至少为 300 秒', 400);
  }
  if (ttl > config.mcp.maxTokenTtlSeconds) {
    throw new ModelingMcpError(
      'MCP_TOKEN_TTL_INVALID',
      `expires_in_seconds 不能超过 ${config.mcp.maxTokenTtlSeconds} 秒`,
      400,
    );
  }
  return ttl;
}

function describeScope(scope: ModelingMcpScope): string {
  if (scope === 'ontology.read') return '读取当前项目的本体资源。';
  if (scope === 'ontology.change.validate') return '同步预校验本体资源修改，不写入。';
  return '执行本体资源写操作，并在校验通过后直接生成正式 revision。';
}

function signingSecret(): Uint8Array {
  if (signingSecretCache) return signingSecretCache;
  const envSecret = String(config.mcp.tokenSecret ?? '').trim();
  if (envSecret) {
    if (envSecret.length < MCP_TOKEN_SECRET_MIN_CHARS) {
      throw new ModelingMcpError(
        'MCP_TOKEN_SECRET_INVALID',
        `MODELING_MCP_TOKEN_SECRET 至少需要 ${MCP_TOKEN_SECRET_MIN_CHARS} 个字符`,
        500,
      );
    }
    signingSecretCache = new TextEncoder().encode(envSecret);
    return signingSecretCache;
  }
  const secretPath = join(config.dataDir, 'mcp-token-secret.txt');
  mkdirSync(config.dataDir, { recursive: true });
  try {
    const current = readFileSync(secretPath, 'utf8').trim();
    if (current) {
      signingSecretCache = new TextEncoder().encode(current);
      return signingSecretCache;
    }
  } catch { /* 首次生成时忽略 */ }

  const generated = randomBytes(32).toString('hex');
  try {
    writeFileSync(secretPath, `${generated}\n`, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    signingSecretCache = new TextEncoder().encode(generated);
    return signingSecretCache;
  } catch {
    const current = readFileSync(secretPath, 'utf8').trim();
    signingSecretCache = new TextEncoder().encode(current);
    return signingSecretCache;
  }
}

export function markModelingMcpRuntimeRunning(instanceId: string, detail = ''): void {
  const now = nowIso();
  getDb().prepare(`INSERT INTO modeling_mcp_runtime
    (singleton_id, instance_id, status, started_at, heartbeat_at, detail)
    VALUES (1, ?, 'running', ?, ?, ?)
    ON CONFLICT(singleton_id) DO UPDATE SET
      instance_id = excluded.instance_id,
      status = 'running',
      started_at = CASE
        WHEN modeling_mcp_runtime.instance_id = excluded.instance_id
          THEN modeling_mcp_runtime.started_at
        ELSE excluded.started_at
      END,
      heartbeat_at = excluded.heartbeat_at,
      detail = excluded.detail`)
    .run(instanceId, now, now, detail || null);
}

export function markModelingMcpRuntimeStopped(instanceId: string, detail = ''): void {
  getDb().prepare(`UPDATE modeling_mcp_runtime
    SET status = 'stopped', heartbeat_at = ?, detail = ?
    WHERE singleton_id = 1 AND instance_id = ?`)
    .run(nowIso(), detail || null, instanceId);
}

function modelingMcpRuntimeState(): { running: boolean; detail: string } {
  const row = getDb().prepare(`SELECT instance_id, status, started_at, heartbeat_at, detail
    FROM modeling_mcp_runtime WHERE singleton_id = 1`).get() as ModelingMcpRuntimeRow | undefined;
  if (!row) return { running: false, detail: 'MCP 服务尚未上报运行状态' };
  if (row.status !== 'running') return { running: false, detail: row.detail || 'MCP 服务已停止' };
  const heartbeat = Date.parse(row.heartbeat_at);
  if (!Number.isFinite(heartbeat) || Date.now() - heartbeat > MCP_RUNTIME_STALE_MS) {
    return { running: false, detail: 'MCP 服务心跳已过期' };
  }
  return { running: true, detail: row.detail || 'MCP 服务运行中' };
}
