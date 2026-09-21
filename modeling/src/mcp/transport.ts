import { randomBytes } from 'node:crypto';

import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

import { AgentCoreError } from '../agent/core-service.js';
import { config } from '../config.js';
import { canWriteProject, getProject } from '../projects/repo.js';
import { normalizeHttpError, sendHttpError } from '../http-error.js';
import {
  MODELING_MCP_SERVER_INFO,
  ModelingMcpError,
  modelingMcpTransportEnabled,
  type ModelingMcpHttpTransport,
  type ModelingMcpToolset,
  verifyProjectMcpToken,
} from './service.js';
import { ModelingMcpToolError, publicMcpToolError } from './tool-error.js';
import { mapMcpArtifactError } from './artifact-error.js';
import { executeModelingMcpTool, toolDefinitionByName, toolDefinitionsFor } from './toolset.js';

type JsonRpcId = string | number | null;

interface JsonRpcRequestPayload {
  jsonrpc: '2.0';
  id?: JsonRpcId;
  method: string;
  params: Record<string, unknown>;
}

type ProtocolMode = 'modern' | 'legacy' | 'compatibility';

interface ProtocolNegotiation {
  version: string;
  mode: ProtocolMode;
  source: 'body' | 'header' | 'default';
}

const JSONRPC_INVALID_REQUEST = -32600;
const JSONRPC_METHOD_NOT_FOUND = -32601;
const JSONRPC_INVALID_PARAMS = -32602;
const JSONRPC_INTERNAL_ERROR = -32603;
const JSONRPC_RESOURCE_NOT_FOUND = -32004;
const JSONRPC_HEADER_MISMATCH = -32020;
const JSONRPC_AUTH_ERROR = -32001;
const SSE_SESSION_NOT_FOUND = 'Session not found';
const SSE_HEARTBEAT_MS = 15_000;
const SERVER_INFO_META_KEY = 'io.modelcontextprotocol/serverInfo';
const CLIENT_CAPABILITIES_META_KEY = 'io.modelcontextprotocol/clientCapabilities';
const LEGACY_PROTOCOL_VERSION = '2025-11-25';

function isMcpTransportPath(url: string): boolean {
  return /\/(?:mcp|sse)(?:\/|[?]|$)/u.test(String(url ?? ''));
}

function rpcCodeForHttpStatus(statusCode: number): number {
  if (statusCode === 401 || statusCode === 403) return JSONRPC_AUTH_ERROR;
  if (statusCode === 404) return JSONRPC_RESOURCE_NOT_FOUND;
  if (statusCode >= 500) return JSONRPC_INTERNAL_ERROR;
  return JSONRPC_INVALID_PARAMS;
}

interface SseSessionRecord {
  sessionId: string;
  endpointId: string;
  toolset: ModelingMcpToolset;
  tokenId: string;
  projectId: string;
  username: string;
  clientIp: string;
  createdAt: number;
  lastActivityAt: number;
  send: (event: 'endpoint' | 'message', data: string) => void;
  close: () => void;
}

export function registerProjectMcpTransportRoutes(app: FastifyInstance): void {
  app.get('/:mcpEndpointId/:toolset/mcp', async (request, reply) => {
    if (!modelingMcpTransportEnabled('streamable-http')) {
      return reply.code(404).send({ error: '当前未启用 Streamable HTTP transport' });
    }
    reply.header('Allow', 'POST');
    return reply.code(405).send({ error: '当前只支持 POST Streamable HTTP 调用' });
  });

  app.delete('/:mcpEndpointId/:toolset/mcp', async (request, reply) => {
    if (!modelingMcpTransportEnabled('streamable-http')) {
      return reply.code(404).send({ error: '当前未启用 Streamable HTTP transport' });
    }
    reply.header('Allow', 'POST');
    return reply.code(405).send({ error: '当前未启用会话删除接口' });
  });

  app.post('/:mcpEndpointId/:toolset/mcp', async (request, reply) => {
    if (!modelingMcpTransportEnabled('streamable-http')) {
      return sendRpcError(reply, null, 404, JSONRPC_METHOD_NOT_FOUND, '当前未启用 Streamable HTTP transport');
    }
    const rpc = parseJsonRpcPayload(request.body);
    if ('error' in rpc) {
      return sendRpcError(reply, null, rpc.error.statusCode, rpc.error.jsonrpcCode, rpc.error.message);
    }

    const toolset = parseToolset((request.params as any).toolset);
    if (!toolset) {
      return sendRpcError(reply, rpc.id ?? null, 404, JSONRPC_INVALID_PARAMS, '未知 MCP toolset');
    }

    try {
      const protocol = negotiateProtocol(rpc, request.headers);
      if (protocol.mode !== 'legacy') validateTransportHeaders(request, rpc, protocol.mode);
      const verified = await verifyProjectMcpToken({
        bearerToken: bearerToken(request.headers.authorization),
        endpointId: String((request.params as any).mcpEndpointId ?? ''),
        minimumAccess: toolset === 'change' ? 'write' : 'read',
      });
      const result = await dispatchMcpRequest({
        rpc,
        protocolMode: protocol.mode,
        protocolVersion: protocol.version,
        toolset,
        verified,
      });
      if (result === undefined) return reply.code(202).send();
      if (protocol.source !== 'default') {
        reply.header('MCP-Protocol-Version', protocol.version);
      }
      maybeSetToolListHeaders(reply, rpc, protocol.mode);
      return reply.code(200).send(rpcResultEnvelope(rpc.id ?? null, result));
    } catch (error) {
      if (error instanceof ModelingMcpError) {
        return sendNormalizedMcpRpcError(reply, rpc.id ?? null, error);
      }
      if (error instanceof RpcProtocolError) {
        return sendRpcError(reply, rpc.id ?? null, error.statusCode, error.jsonrpcCode, error.message, error.data);
      }
      const artifactError = tryMapMcpArtifactError(error, 'load');
      if (artifactError) {
        return sendMcpArtifactRpcError(reply, rpc.id ?? null, artifactError, error);
      }
      request.log.error({ err: error }, 'MCP Streamable HTTP 请求执行失败');
      return sendRpcError(
        reply,
        rpc.id ?? null,
        500,
        JSONRPC_INTERNAL_ERROR,
        'MCP 服务内部错误',
        { code: 'MCP_INTERNAL_ERROR' },
      );
    }
  });

  app.get('/:mcpEndpointId/:toolset/sse', async (request, reply) => {
    if (!modelingMcpTransportEnabled('sse')) {
      return reply.code(404).send({ error: '当前未启用 SSE transport' });
    }
    const toolset = parseToolset((request.params as any).toolset);
    if (!toolset) {
      return reply.code(404).send({ error: '未知 MCP toolset' });
    }
    try {
      const verified = await verifyProjectMcpToken({
        bearerToken: bearerToken(request.headers.authorization),
        endpointId: String((request.params as any).mcpEndpointId ?? ''),
        minimumAccess: toolset === 'change' ? 'write' : 'read',
      });
      (request as FastifyRequest & { mcpPrincipal?: {
        tokenId: string; projectId: string; username: string; clientIp: string;
      } }).mcpPrincipal = {
        tokenId: verified.tokenId,
        projectId: verified.projectId,
        username: verified.username,
        clientIp: request.ip,
      };
    } catch (error) {
      if (error instanceof ModelingMcpError) {
        return sendNormalizedMcpRpcError(reply, null, error);
      }
      throw error;
    }

    const endpointId = String((request.params as any).mcpEndpointId ?? '').trim();
    reply.hijack();
    const raw = reply.raw;
    raw.statusCode = 200;
    raw.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
    raw.setHeader('Cache-Control', 'no-cache, no-transform');
    raw.setHeader('Connection', 'keep-alive');
    raw.setHeader('X-Accel-Buffering', 'no');

    let sessionId = '';
    let released = false;
    let heartbeat: ReturnType<typeof setInterval> | null = null;
    const release = () => {
      if (released) return;
      released = true;
      if (heartbeat) clearInterval(heartbeat);
      if (sessionId) sseSessions.release(sessionId);
      try {
        if (!raw.writableEnded) raw.end();
      } catch {
        // ignore close failures
      }
    };
    const send = (event: 'endpoint' | 'message', data: string) => {
      if (released || raw.destroyed || raw.writableEnded) return;
      raw.write(formatSseEvent(event, data));
    };

    try {
      const principal = (request as FastifyRequest & {
        mcpPrincipal?: { tokenId: string; projectId: string; username: string; clientIp: string };
      }).mcpPrincipal;
      if (!principal) throw new ModelingMcpError('MCP_AUTH_REQUIRED', '缺少 MCP 会话身份', 401);
      sessionId = sseSessions.create(endpointId, toolset, principal, send, release).sessionId;
    } catch (error) {
      const normalized = normalizeHttpError(error, {
        code: 'MCP_INTERNAL_ERROR',
        message: 'MCP 服务内部错误',
      });
      raw.statusCode = normalized.statusCode;
      raw.setHeader('Content-Type', 'application/json; charset=utf-8');
      if (normalized.statusCode >= 500) request.log.error({ err: error }, 'MCP SSE 会话创建失败');
      raw.end(JSON.stringify(
        rpcErrorEnvelope(
          null,
          rpcCodeForHttpStatus(normalized.statusCode),
          normalized.message,
          { code: normalized.code },
        ),
      ));
      return;
    }

    raw.flushHeaders?.();
    send('endpoint', `/${endpointId}/${toolset}/sse/messages/?session_id=${sessionId}`);
    heartbeat = setInterval(() => {
      if (!released && !raw.destroyed && !raw.writableEnded) raw.write(': keep-alive\n\n');
    }, SSE_HEARTBEAT_MS);
    const closePromise = new Promise<void>((resolve) => {
      request.raw.once('close', () => resolve());
      request.raw.once('aborted', () => resolve());
    });
    request.raw.once('close', release);
    request.raw.once('aborted', release);
    await closePromise;
  });

  const sseMessageHandler = async (request: FastifyRequest, reply: FastifyReply) => {
    if (!modelingMcpTransportEnabled('sse')) {
      return sendRpcError(reply, null, 404, JSONRPC_METHOD_NOT_FOUND, '当前未启用 SSE transport');
    }
    const rpc = parseJsonRpcPayload(request.body);
    if ('error' in rpc) {
      return sendRpcError(reply, null, rpc.error.statusCode, rpc.error.jsonrpcCode, rpc.error.message);
    }

    const toolset = parseToolset((request.params as any).toolset);
    if (!toolset) {
      return sendRpcError(reply, rpc.id ?? null, 404, JSONRPC_INVALID_PARAMS, '未知 MCP toolset');
    }
    const endpointId = String((request.params as any).mcpEndpointId ?? '').trim();
    const sessionId = sseSessionIdFromQuery(request.query);
    const session = sseSessions.resolve(sessionId, endpointId, toolset);
    if (!session) {
      return sendRpcError(reply, null, 404, JSONRPC_INVALID_REQUEST, SSE_SESSION_NOT_FOUND);
    }

    try {
      const protocol = negotiateProtocol(rpc, request.headers);
      if (protocol.mode !== 'legacy') validateTransportHeaders(request, rpc, protocol.mode);
      const verified = await verifyProjectMcpToken({
        bearerToken: bearerToken(request.headers.authorization),
        endpointId,
        minimumAccess: toolset === 'change' ? 'write' : 'read',
      });
      if (session.tokenId !== verified.tokenId || session.username !== verified.username) {
        throw new ModelingMcpError('MCP_SESSION_PRINCIPAL_MISMATCH', 'SSE session 与当前 Token 不匹配', 403);
      }
      const result = await dispatchMcpRequest({
        rpc,
        protocolMode: protocol.mode,
        protocolVersion: protocol.version,
        toolset,
        verified,
      });
      if (result !== undefined) {
        session.send('message', JSON.stringify(rpcResultEnvelope(rpc.id ?? null, result)));
      }
      if (protocol.source !== 'default') {
        reply.header('MCP-Protocol-Version', protocol.version);
      }
      return reply.code(202).send();
    } catch (error) {
      if (error instanceof ModelingMcpError) {
        return sendNormalizedMcpRpcError(reply, rpc.id ?? null, error);
      }
      if (error instanceof RpcProtocolError) {
        return sendRpcError(reply, rpc.id ?? null, error.statusCode, error.jsonrpcCode, error.message, error.data);
      }
      const artifactError = tryMapMcpArtifactError(error, 'load');
      if (artifactError) {
        return sendMcpArtifactRpcError(reply, rpc.id ?? null, artifactError, error);
      }
      request.log.error({ err: error }, 'MCP SSE 请求执行失败');
      return sendRpcError(
        reply,
        rpc.id ?? null,
        500,
        JSONRPC_INTERNAL_ERROR,
        'MCP 服务内部错误',
        { code: 'MCP_INTERNAL_ERROR' },
      );
    }
  };
  app.post('/:mcpEndpointId/:toolset/sse/messages', sseMessageHandler);
  app.post('/:mcpEndpointId/:toolset/sse/messages/', sseMessageHandler);
}

/**
 * MCP 进程独立于主 modeling Fastify 实例运行，不能依赖主应用安装的
 * setErrorHandler。覆盖 transport 路径上尚未被路由捕获的异常，保证它们
 * 仍然是 JSON-RPC 响应而不是 Fastify 默认的 HTML/"Internal Server Error"。
 * 非 transport 路径（例如健康检查）复用普通 HTTP 脱敏规则。
 */
export function installMcpHttpErrorHandler(app: FastifyInstance): void {
  app.setErrorHandler((error, request, reply) => {
    if (reply.sent) return reply;
    if (!isMcpTransportPath(request.url)) {
      return sendHttpError(reply, error, {
        logMessage: `${request.method} ${request.url} MCP 服务请求执行失败`,
      });
    }

    const normalized = normalizeHttpError(error, {
      code: 'MCP_INTERNAL_ERROR',
      message: 'MCP 服务内部错误',
      logMessage: `${request.method} ${request.url} MCP transport 请求执行失败`,
    });
    if (normalized.statusCode >= 500) {
      request.log.error({ err: error }, 'MCP transport 未捕获异常');
    }
    return reply.code(normalized.statusCode).send(rpcErrorEnvelope(
      null,
      rpcCodeForHttpStatus(normalized.statusCode),
      normalized.message,
      { code: normalized.code },
    ));
  });
}

async function dispatchMcpRequest(input: {
  rpc: JsonRpcRequestPayload;
  protocolMode: ProtocolMode;
  protocolVersion: string;
  toolset: ModelingMcpToolset;
  verified: Awaited<ReturnType<typeof verifyProjectMcpToken>>;
}): Promise<Record<string, unknown> | undefined> {
  const { rpc, protocolMode, protocolVersion, toolset, verified } = input;
  if (rpc.method === 'notifications/initialized') return undefined;
  if (rpc.method === 'ping') return {};

  if (rpc.method === 'initialize') {
      return {
        protocolVersion,
        capabilities: { tools: { listChanged: false } },
        serverInfo: MODELING_MCP_SERVER_INFO,
        instructions: `当前 endpoint 只暴露 ${toolset} toolset，请通过 tools/list 查看当前 token 可用工具。`,
      };
  }

  if (rpc.method === 'server/discover') {
    return {
      resultType: 'complete',
      supportedVersions: config.mcp.supportedProtocolVersions,
      capabilities: { tools: { listChanged: false } },
      instructions: `当前 endpoint 只暴露 ${toolset} toolset，请通过 tools/list 查看当前 token 可用工具。`,
      ttlMs: 300_000,
      cacheScope: 'private',
      _meta: { [SERVER_INFO_META_KEY]: MODELING_MCP_SERVER_INFO },
    };
  }

  if (rpc.method === 'tools/list') {
    const tools = toolDefinitionsFor(toolset, verified.scopes).map((tool) => ({
      name: tool.name,
      title: tool.title,
      description: tool.description,
      inputSchema: tool.inputSchema,
      ...(tool.outputSchema ? { outputSchema: tool.outputSchema } : {}),
      annotations: {
        readOnlyHint: tool.toolset === 'read',
      },
    }));
    if (protocolMode !== 'legacy') {
      return {
        resultType: 'complete',
        tools,
        ttlMs: 300_000,
        cacheScope: 'private',
        _meta: { [SERVER_INFO_META_KEY]: MODELING_MCP_SERVER_INFO },
      };
    }
    return { tools };
  }

  if (rpc.method === 'tools/call') {
    const toolName = String(rpc.params.name ?? '').trim();
    if (!toolName) throw new RpcProtocolError('缺少 tools/call.name', 400, JSONRPC_INVALID_PARAMS);
    const tool = toolDefinitionByName(toolName);
    if (!tool || tool.toolset !== toolset) {
      throw new RpcProtocolError(`当前 endpoint 不支持工具 ${toolName}`, 404, JSONRPC_INVALID_PARAMS);
    }
    if (!verified.scopes.includes(tool.requiredScope)) {
      throw new ModelingMcpError('MCP_SCOPE_FORBIDDEN', `当前 Token 不具备 ${toolName} 所需权限`, 403);
    }
    const project = getProject(verified.projectId);
    if (!project) throw new ModelingMcpError('MCP_PROJECT_NOT_FOUND', '项目不存在', 404);
    if (toolset === 'change' && !canWriteProject(project.id, verified.username)) {
      throw new ModelingMcpError('MCP_PROJECT_FORBIDDEN', '当前用户已无权修改该项目', 403);
    }
    const argumentsValue = rpc.params.arguments;
    if (argumentsValue !== undefined && (!argumentsValue || typeof argumentsValue !== 'object' || Array.isArray(argumentsValue))) {
      throw new RpcProtocolError('tools/call.arguments 必须是对象', 400, JSONRPC_INVALID_PARAMS);
    }
    try {
      const payload = await executeModelingMcpTool({
        toolName,
        project,
        username: verified.username,
        tokenId: verified.tokenId,
        argumentsValue: (argumentsValue as Record<string, unknown> | undefined) ?? {},
      });
      return protocolMode !== 'legacy'
        ? modernToolResult(payload)
        : legacyToolResult(payload);
    } catch (error) {
      if (error instanceof AgentCoreError || error instanceof ModelingMcpToolError) {
        return protocolMode !== 'legacy'
          ? modernToolErrorResult(error)
          : legacyToolErrorResult(error);
      }
      const artifactError = tryMapMcpArtifactError(error, 'load');
      if (artifactError) {
        return protocolMode !== 'legacy'
          ? modernToolErrorResult(artifactError)
          : legacyToolErrorResult(artifactError);
      }
      throw error;
    }
  }

  throw new RpcProtocolError(`不支持的方法 ${rpc.method}`, 404, JSONRPC_METHOD_NOT_FOUND);
}

function maybeSetToolListHeaders(reply: FastifyReply, rpc: JsonRpcRequestPayload, protocolMode: ProtocolMode): void {
  if (protocolMode !== 'legacy' && rpc.method === 'tools/list') {
    reply.header('Cache-Control', 'private, max-age=300');
  }
}

function rpcResultEnvelope(id: JsonRpcId, result: Record<string, unknown>) {
  return { jsonrpc: '2.0', id, result };
}

function rpcErrorEnvelope(id: JsonRpcId, code: number, message: string, data?: Record<string, unknown>) {
  return { jsonrpc: '2.0', id, error: { code, message, ...(data ? { data } : {}) } };
}

function parseJsonRpcPayload(body: unknown):
  | JsonRpcRequestPayload
  | { error: RpcProtocolError } {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { error: new RpcProtocolError('请求体必须是单个 JSON-RPC 对象', 400, JSONRPC_INVALID_REQUEST) };
  }
  const payload = body as Record<string, unknown>;
  if (payload.jsonrpc !== '2.0') {
    return { error: new RpcProtocolError('jsonrpc 必须为 2.0', 400, JSONRPC_INVALID_REQUEST) };
  }
  if (typeof payload.method !== 'string' || !payload.method.trim()) {
    return { error: new RpcProtocolError('method 必填', 400, JSONRPC_INVALID_REQUEST) };
  }
  const params = payload.params;
  if (params !== undefined && (!params || typeof params !== 'object' || Array.isArray(params))) {
    return { error: new RpcProtocolError('params 必须是对象', 400, JSONRPC_INVALID_PARAMS) };
  }
  return {
    jsonrpc: '2.0',
    ...(Object.hasOwn(payload, 'id') ? { id: payload.id as JsonRpcId } : {}),
    method: payload.method.trim(),
    params: (params as Record<string, unknown> | undefined) ?? {},
  };
}

function negotiateProtocol(rpc: JsonRpcRequestPayload, headers: Record<string, unknown>): ProtocolNegotiation {
  const headerVersion = headerValue(headers['mcp-protocol-version']);
  const bodyVersion = rpcBodyProtocolVersion(rpc);
  if (headerVersion && bodyVersion && headerVersion !== bodyVersion) {
    throw protocolVersionMismatch(headerVersion, bodyVersion);
  }
  const requestedVersion = bodyVersion || headerVersion;
  const version = requestedVersion || config.mcp.supportedProtocolVersions[0];
  if (requestedVersion) assertProtocolVersionSupported(requestedVersion);
  const mode = !requestedVersion || rpc.method === 'initialize' || version === LEGACY_PROTOCOL_VERSION
    ? 'legacy'
    : version === config.mcp.supportedProtocolVersions[0] ? 'modern' : 'compatibility';
  return {
    version,
    mode,
    source: bodyVersion ? 'body' : headerVersion ? 'header' : 'default',
  };
}

function validateTransportHeaders(
  request: FastifyRequest,
  rpc: JsonRpcRequestPayload,
  protocolMode: Exclude<ProtocolMode, 'legacy'>,
): void {
  const meta = rpc.params._meta;
  const clientCapabilities = meta && typeof meta === 'object' && !Array.isArray(meta)
    ? (meta as Record<string, unknown>)[CLIENT_CAPABILITIES_META_KEY]
    : undefined;
  if (protocolMode === 'modern'
    && (!clientCapabilities || typeof clientCapabilities !== 'object' || Array.isArray(clientCapabilities))) {
    throw new RpcProtocolError(
      `请求体 _meta.${CLIENT_CAPABILITIES_META_KEY} 必须是对象`,
      400,
      JSONRPC_INVALID_PARAMS,
    );
  }

  const methodHeader = decodeHeaderSentinel(headerValue(request.headers['mcp-method']));
  if (methodHeader && methodHeader !== rpc.method) {
    throw new RpcProtocolError('Mcp-Method 请求头与 JSON-RPC method 不一致', 400, JSONRPC_HEADER_MISMATCH);
  }
  if (rpc.method === 'tools/call') {
    const toolNameHeader = decodeHeaderSentinel(headerValue(request.headers['mcp-name']));
    const toolName = String(rpc.params.name ?? '').trim();
    if (toolNameHeader && toolNameHeader !== toolName) {
      throw new RpcProtocolError('Mcp-Name 请求头与 tools/call.name 不一致', 400, JSONRPC_HEADER_MISMATCH);
    }
  }
}

function modernToolResult(payload: Record<string, unknown>): Record<string, unknown> {
  return {
    resultType: 'complete',
    content: [{
      type: 'text',
      text: toolText(payload),
    }],
    structuredContent: payload,
    _meta: { [SERVER_INFO_META_KEY]: MODELING_MCP_SERVER_INFO },
  };
}

function legacyToolResult(payload: Record<string, unknown>): Record<string, unknown> {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify(payload, null, 2),
    }],
    structuredContent: payload,
    isError: false,
  };
}

function modernToolErrorResult(error: AgentCoreError | ModelingMcpToolError): Record<string, unknown> {
  const publicError = toolErrorPayload(error);
  return {
    resultType: 'complete',
    content: [{
      type: 'text',
      text: publicError.message,
    }],
    structuredContent: {
      code: publicError.code,
      error: publicError.message,
      next: publicError.next,
      details: publicError.details,
    },
    isError: true,
    _meta: { [SERVER_INFO_META_KEY]: MODELING_MCP_SERVER_INFO },
  };
}

function legacyToolErrorResult(error: AgentCoreError | ModelingMcpToolError): Record<string, unknown> {
  const publicError = toolErrorPayload(error);
  const structuredContent = {
    code: publicError.code,
    error: publicError.message,
    next: publicError.next,
    details: publicError.details,
  };
  return {
    content: [{
      type: 'text',
      text: JSON.stringify(structuredContent, null, 2),
    }],
    structuredContent,
    isError: true,
  };
}

function toolText(payload: Record<string, unknown>): string {
  const display = payload.display;
  if (display && typeof display === 'object' && !Array.isArray(display)) {
    const text = String((display as Record<string, unknown>).text ?? '').trim();
    if (text) return text;
  }
  return JSON.stringify(payload, null, 2);
}

function rpcBodyProtocolVersion(rpc: JsonRpcRequestPayload): string {
  const standardVersion = String(rpc.params.protocolVersion ?? '').trim();
  if (standardVersion) return standardVersion;
  const meta = rpc.params._meta;
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return '';
  return String((meta as Record<string, unknown>)['io.modelcontextprotocol/protocolVersion'] ?? '').trim();
}

function isProtocolVersion(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/u.test(value);
}

function assertProtocolVersionSupported(version: string): void {
  if (!isProtocolVersion(version)) throw invalidProtocolVersion(version);
  if (config.mcp.supportedProtocolVersions.includes(version)) return;
  if (config.mcp.protocolCompatibility) return;
  throw unsupportedProtocolVersion(version);
}

function invalidProtocolVersion(version: string): RpcProtocolError {
  return new RpcProtocolError(
    'MCP 协议版本格式无效',
    400,
    JSONRPC_HEADER_MISMATCH,
    {
      code: 'MCP_PROTOCOL_VERSION_INVALID',
      requested_version: version,
      supported_versions: config.mcp.supportedProtocolVersions,
    },
  );
}

function unsupportedProtocolVersion(version: string): RpcProtocolError {
  return new RpcProtocolError(
    '不支持的 MCP 协议版本',
    400,
    JSONRPC_HEADER_MISMATCH,
    {
      code: 'MCP_PROTOCOL_VERSION_UNSUPPORTED',
      requested_version: version,
      supported_versions: config.mcp.supportedProtocolVersions,
    },
  );
}

function protocolVersionMismatch(headerVersion: string, bodyVersion: string): RpcProtocolError {
  return new RpcProtocolError(
    'MCP 协议版本请求头与请求体不一致',
    400,
    JSONRPC_HEADER_MISMATCH,
    {
      code: 'MCP_PROTOCOL_VERSION_MISMATCH',
      header_version: headerVersion,
      body_version: bodyVersion,
      supported_versions: config.mcp.supportedProtocolVersions,
    },
  );
}

function parseToolset(value: unknown): ModelingMcpToolset | null {
  const normalized = String(value ?? '').trim();
  if (normalized === 'read' || normalized === 'change') return normalized;
  return null;
}

function headerValue(value: unknown): string {
  if (Array.isArray(value)) return String(value[0] ?? '').trim();
  return String(value ?? '').trim();
}

function bearerToken(authorization: unknown): string {
  const header = String(authorization ?? '').trim();
  return header.startsWith('Bearer ') ? header.slice(7).trim() : '';
}

function decodeHeaderSentinel(value: string): string {
  const normalized = value.trim();
  const match = normalized.match(/^=\?utf-8\?b\?([a-z0-9+/=]+)\?=$/iu);
  if (!match) return normalized;
  try {
    return Buffer.from(match[1], 'base64').toString('utf8').trim();
  } catch {
    return normalized;
  }
}

function sseSessionIdFromQuery(query: unknown): string {
  if (!query || typeof query !== 'object' || Array.isArray(query)) return '';
  const value = (query as Record<string, unknown>).session_id;
  if (Array.isArray(value)) return String(value[0] ?? '').trim();
  return String(value ?? '').trim();
}

function formatSseEvent(event: 'endpoint' | 'message', data: string): string {
  return `event: ${event}\ndata: ${String(data).replace(/\r?\n/gu, '\ndata: ')}\n\n`;
}

function sendRpcError(
  reply: FastifyReply,
  id: JsonRpcId,
  statusCode: number,
  code: number,
  message: string,
  data?: Record<string, unknown>,
): FastifyReply {
  return reply.code(statusCode).send(rpcErrorEnvelope(id, code, message, data));
}

function sendNormalizedMcpRpcError(
  reply: FastifyReply,
  id: JsonRpcId,
  error: unknown,
): FastifyReply {
  const normalized = normalizeHttpError(error, {
    code: 'MCP_INTERNAL_ERROR',
    message: 'MCP 服务内部错误',
    includeReason: false,
  });
  if (normalized.statusCode >= 500) reply.log.error({ err: error }, 'MCP 请求执行失败');
  return sendRpcError(
    reply,
    id,
    normalized.statusCode,
    rpcCodeForHttpStatus(normalized.statusCode),
    normalized.message,
    { code: normalized.code },
  );
}

function sendMcpArtifactRpcError(
  reply: FastifyReply,
  id: JsonRpcId,
  error: ModelingMcpToolError,
  cause: unknown,
): FastifyReply {
  const publicError = publicMcpToolError(error);
  if (error.statusCode >= 500) reply.log.error({ err: cause }, 'MCP 工件请求执行失败');
  return sendRpcError(
    reply,
    id,
    error.statusCode,
    rpcCodeForHttpStatus(error.statusCode),
    publicError.message,
    {
      code: publicError.code,
      ...(error.statusCode < 500 ? { details: publicError.details } : {}),
    },
  );
}

function toolErrorPayload(error: AgentCoreError | ModelingMcpToolError) {
  if (error instanceof ModelingMcpToolError) return publicMcpToolError(error);
  if (error.statusCode >= 500) {
    return {
      code: error.code,
      message: 'MCP 工具暂时不可用，请稍后重试',
      next: [],
      details: {},
    };
  }
  return {
    code: error.code,
    message: error.message,
    next: [...error.next],
    details: safePublicDetails(error.details),
  };
}

function tryMapMcpArtifactError(
  error: unknown,
  phase: 'input' | 'load' | 'validate' | 'persist',
): ModelingMcpToolError | null {
  try {
    return mapMcpArtifactError(error, { phase });
  } catch {
    return null;
  }
}

function safePublicDetails(value: unknown, depth = 0): any {
  if (depth > 8) return '<redacted>';
  if (Array.isArray(value)) return value.slice(0, 200).map((item) => safePublicDetails(item, depth + 1));
  if (value && typeof value === 'object') {
    const output: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (/^(?:technical|stack|sql|token|authorization|secret)$/iu.test(key)) continue;
      output[key] = safePublicDetails(item, depth + 1);
    }
    return output;
  }
  if (typeof value === 'string'
    && (/(?:^|\s)[A-Za-z]:\\/u.test(value) || /\/(?:home|var|data|run)\//u.test(value))) {
    return '<redacted>';
  }
  return value;
}

class RpcProtocolError extends Error {
  constructor(
    message: string,
    public readonly statusCode: number,
    public readonly jsonrpcCode: number,
    public readonly data?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'RpcProtocolError';
  }
}

export interface SseSessionPolicy {
  maxSessions: number;
  maxSessionsPerToken: number;
  maxSessionsPerProject: number;
  maxSessionsPerUser: number;
  maxSessionsPerIp: number;
  maxLifetimeMs: number;
  idleTimeoutMs: number;
}

export class SseSessionRegistry {
  private readonly sessions = new Map<string, SseSessionRecord>();

  constructor(
    private readonly policy: SseSessionPolicy,
    private readonly now: () => number = Date.now,
    startCleanupTimer = true,
  ) {
    if (startCleanupTimer) setInterval(() => this.releaseExpired(), SSE_HEARTBEAT_MS).unref();
  }

  create(
    endpointId: string,
    toolset: ModelingMcpToolset,
    principal: { tokenId: string; projectId: string; username: string; clientIp: string },
    send: SseSessionRecord['send'],
    close: SseSessionRecord['close'],
  ): SseSessionRecord {
    if (this.sessions.size >= this.policy.maxSessions) {
      throw new ModelingMcpError('MCP_SESSION_CAPACITY_REACHED', 'SSE 会话容量已满', 503);
    }
    const existing = [...this.sessions.values()];
    if (existing.filter((item) => item.tokenId === principal.tokenId).length >= this.policy.maxSessionsPerToken) {
      throw new ModelingMcpError('MCP_SESSION_TOKEN_LIMIT_REACHED', '当前 Token 的 SSE 会话数已达上限', 429);
    }
    if (existing.filter((item) => item.projectId === principal.projectId).length >= this.policy.maxSessionsPerProject) {
      throw new ModelingMcpError('MCP_SESSION_PROJECT_LIMIT_REACHED', '当前项目的 SSE 会话数已达上限', 429);
    }
    if (existing.filter((item) => item.username === principal.username).length >= this.policy.maxSessionsPerUser) {
      throw new ModelingMcpError('MCP_SESSION_USER_LIMIT_REACHED', '当前用户的 SSE 会话数已达上限', 429);
    }
    if (existing.filter((item) => item.clientIp === principal.clientIp).length >= this.policy.maxSessionsPerIp) {
      throw new ModelingMcpError('MCP_SESSION_IP_LIMIT_REACHED', '当前来源地址的 SSE 会话数已达上限', 429);
    }
    while (true) {
      const sessionId = randomBytes(16).toString('hex');
      if (this.sessions.has(sessionId)) continue;
      const now = this.now();
      const session = { sessionId, endpointId, toolset, ...principal, send, close, createdAt: now, lastActivityAt: now };
      this.sessions.set(sessionId, session);
      return session;
    }
  }

  resolve(sessionId: string, endpointId: string, toolset: ModelingMcpToolset): SseSessionRecord | null {
    const normalized = String(sessionId ?? '').trim();
    if (!normalized) return null;
    const session = this.sessions.get(normalized);
    if (!session) return null;
    if (session.endpointId !== endpointId || session.toolset !== toolset) return null;
    session.lastActivityAt = this.now();
    return session;
  }

  release(sessionId: string): void {
    const normalized = String(sessionId ?? '').trim();
    if (!normalized) return;
    this.sessions.delete(normalized);
  }

  releaseExpired(): void {
    const now = this.now();
    for (const session of [...this.sessions.values()]) {
      if (now - session.createdAt < this.policy.maxLifetimeMs
        && now - session.lastActivityAt < this.policy.idleTimeoutMs) continue;
      session.close();
      this.sessions.delete(session.sessionId);
    }
  }
}

const sseSessions = new SseSessionRegistry({
  maxSessions: config.mcp.maxSseSessions,
  maxSessionsPerToken: config.mcp.maxSseSessionsPerToken,
  maxSessionsPerProject: config.mcp.maxSseSessionsPerProject,
  maxSessionsPerUser: config.mcp.maxSseSessionsPerUser,
  maxSessionsPerIp: config.mcp.maxSseSessionsPerIp,
  maxLifetimeMs: config.mcp.sseMaxLifetimeSeconds * 1_000,
  idleTimeoutMs: config.mcp.sseIdleTimeoutSeconds * 1_000,
});
