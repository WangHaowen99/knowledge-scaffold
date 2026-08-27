import { randomBytes } from 'node:crypto';

import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

import { AgentCoreError } from '../agent/core-service.js';
import { config } from '../config.js';
import { canWriteProject, getProject } from '../projects/repo.js';
import {
  MODELING_MCP_SERVER_INFO,
  ModelingMcpError,
  modelingMcpTransportEnabled,
  type ModelingMcpHttpTransport,
  type ModelingMcpToolset,
  verifyProjectMcpToken,
} from './service.js';
import { executeModelingMcpTool, toolDefinitionByName, toolDefinitionsFor } from './toolset.js';

type JsonRpcId = string | number | null;

interface JsonRpcRequestPayload {
  jsonrpc: '2.0';
  id?: JsonRpcId;
  method: string;
  params: Record<string, unknown>;
}

type ProtocolMode = 'modern' | 'legacy';

const JSONRPC_INVALID_REQUEST = -32600;
const JSONRPC_METHOD_NOT_FOUND = -32601;
const JSONRPC_INVALID_PARAMS = -32602;
const JSONRPC_INTERNAL_ERROR = -32603;
const JSONRPC_HEADER_MISMATCH = -32020;
const JSONRPC_AUTH_ERROR = -32001;
const SSE_SESSION_NOT_FOUND = 'Session not found';
const MAX_SSE_SESSIONS = 10_000;
const SSE_HEARTBEAT_MS = 15_000;

interface SseSessionRecord {
  sessionId: string;
  endpointId: string;
  toolset: ModelingMcpToolset;
  send: (event: 'endpoint' | 'message', data: string) => void;
}

export function registerProjectMcpTransportRoutes(app: FastifyInstance): void {
  app.get('/:mcpEndpointId/:toolset/mcp', async (_request, reply) => {
    if (!modelingMcpTransportEnabled('streamable-http')) {
      return reply.code(404).send({ error: '当前未启用 Streamable HTTP transport' });
    }
    reply.header('Allow', 'POST');
    return reply.code(405).send({ error: '当前只支持 POST Streamable HTTP 调用' });
  });

  app.delete('/:mcpEndpointId/:toolset/mcp', async (_request, reply) => {
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

    const protocolMode = detectProtocolMode(rpc, request.headers);
    try {
      if (protocolMode === 'modern') validateModernTransportHeaders(request, rpc);
      const verified = await verifyProjectMcpToken({
        bearerToken: bearerToken(request.headers.authorization),
        endpointId: String((request.params as any).mcpEndpointId ?? ''),
        minimumAccess: toolset === 'change' ? 'write' : 'read',
      });
      const result = await dispatchMcpRequest({ rpc, protocolMode, toolset, verified });
      if (result === undefined) return reply.code(202).send();
      if (protocolMode === 'modern') {
        reply.header('MCP-Protocol-Version', config.mcp.protocolVersion);
      }
      maybeSetToolListHeaders(reply, rpc, protocolMode);
      return reply.code(200).send(rpcResultEnvelope(rpc.id ?? null, result));
    } catch (error) {
      if (error instanceof ModelingMcpError) {
        return sendRpcError(reply, rpc.id ?? null, error.statusCode, JSONRPC_AUTH_ERROR, error.message);
      }
      if (error instanceof RpcProtocolError) {
        return sendRpcError(reply, rpc.id ?? null, error.statusCode, error.jsonrpcCode, error.message);
      }
      return sendRpcError(
        reply,
        rpc.id ?? null,
        500,
        JSONRPC_INTERNAL_ERROR,
        error instanceof Error ? error.message : String(error),
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
      await verifyProjectMcpToken({
        bearerToken: bearerToken(request.headers.authorization),
        endpointId: String((request.params as any).mcpEndpointId ?? ''),
        minimumAccess: toolset === 'change' ? 'write' : 'read',
      });
    } catch (error) {
      if (error instanceof ModelingMcpError) {
        return reply.code(error.statusCode).send({
          jsonrpc: '2.0',
          id: null,
          error: { code: JSONRPC_AUTH_ERROR, message: error.message },
        });
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
      sessionId = sseSessions.create(endpointId, toolset, send).sessionId;
    } catch (error) {
      raw.statusCode = error instanceof ModelingMcpError ? error.statusCode : 500;
      raw.setHeader('Content-Type', 'application/json; charset=utf-8');
      raw.end(JSON.stringify(
        rpcErrorEnvelope(
          null,
          error instanceof ModelingMcpError ? JSONRPC_INTERNAL_ERROR : JSONRPC_INTERNAL_ERROR,
          error instanceof Error ? error.message : String(error),
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

    const protocolMode = detectProtocolMode(rpc, request.headers);
    try {
      if (protocolMode === 'modern') validateModernTransportHeaders(request, rpc);
      const verified = await verifyProjectMcpToken({
        bearerToken: bearerToken(request.headers.authorization),
        endpointId,
        minimumAccess: toolset === 'change' ? 'write' : 'read',
      });
      const result = await dispatchMcpRequest({ rpc, protocolMode, toolset, verified });
      if (result !== undefined) {
        session.send('message', JSON.stringify(rpcResultEnvelope(rpc.id ?? null, result)));
      }
      if (protocolMode === 'modern') {
        reply.header('MCP-Protocol-Version', config.mcp.protocolVersion);
      }
      return reply.code(202).send();
    } catch (error) {
      if (error instanceof ModelingMcpError) {
        return sendRpcError(reply, rpc.id ?? null, error.statusCode, JSONRPC_AUTH_ERROR, error.message);
      }
      if (error instanceof RpcProtocolError) {
        return sendRpcError(reply, rpc.id ?? null, error.statusCode, error.jsonrpcCode, error.message);
      }
      return sendRpcError(
        reply,
        rpc.id ?? null,
        500,
        JSONRPC_INTERNAL_ERROR,
        error instanceof Error ? error.message : String(error),
      );
    }
  };
  app.post('/:mcpEndpointId/:toolset/sse/messages', sseMessageHandler);
  app.post('/:mcpEndpointId/:toolset/sse/messages/', sseMessageHandler);
}

async function dispatchMcpRequest(input: {
  rpc: JsonRpcRequestPayload;
  protocolMode: ProtocolMode;
  toolset: ModelingMcpToolset;
  verified: Awaited<ReturnType<typeof verifyProjectMcpToken>>;
}): Promise<Record<string, unknown> | undefined> {
  const { rpc, protocolMode, toolset, verified } = input;
  if (rpc.method === 'notifications/initialized') return undefined;
  if (rpc.method === 'ping') return {};

  if (rpc.method === 'initialize') {
      return {
        protocolVersion: config.mcp.legacyProtocolVersion,
        capabilities: { tools: { listChanged: false } },
        serverInfo: MODELING_MCP_SERVER_INFO,
        instructions: `当前 endpoint 只暴露 ${toolset} toolset，请通过 tools/list 查看当前 token 可用工具。`,
      };
  }

  if (rpc.method === 'server/discover') {
    return {
      supportedVersions: [config.mcp.protocolVersion, config.mcp.legacyProtocolVersion],
      capabilities: { tools: { listChanged: false } },
      instructions: `当前 endpoint 只暴露 ${toolset} toolset，请通过 tools/list 查看当前 token 可用工具。`,
      _meta: { serverInfo: MODELING_MCP_SERVER_INFO },
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
    if (protocolMode === 'modern') {
      return {
        resultType: 'complete',
        tools,
        ttlMs: 300_000,
        cacheScope: 'private',
        _meta: { serverInfo: MODELING_MCP_SERVER_INFO },
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
      return protocolMode === 'modern'
        ? modernToolResult(payload)
        : legacyToolResult(payload);
    } catch (error) {
      if (error instanceof AgentCoreError) {
        return protocolMode === 'modern'
          ? modernToolErrorResult(error)
          : legacyToolErrorResult(error);
      }
      throw error;
    }
  }

  throw new RpcProtocolError(`不支持的方法 ${rpc.method}`, 404, JSONRPC_METHOD_NOT_FOUND);
}

function maybeSetToolListHeaders(reply: FastifyReply, rpc: JsonRpcRequestPayload, protocolMode: ProtocolMode): void {
  if (protocolMode === 'modern' && rpc.method === 'tools/list') {
    reply.header('Cache-Control', 'private, max-age=300');
  }
}

function rpcResultEnvelope(id: JsonRpcId, result: Record<string, unknown>) {
  return { jsonrpc: '2.0', id, result };
}

function rpcErrorEnvelope(id: JsonRpcId, code: number, message: string) {
  return { jsonrpc: '2.0', id, error: { code, message } };
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

function detectProtocolMode(rpc: JsonRpcRequestPayload, headers: Record<string, unknown>): ProtocolMode {
  const headerVersion = headerValue(headers['mcp-protocol-version']);
  const metaVersion = rpcMetaProtocolVersion(rpc);
  if (rpc.method === 'initialize') return 'legacy';
  if (!headerVersion && !metaVersion) return 'legacy';
  return 'modern';
}

function validateModernTransportHeaders(request: FastifyRequest, rpc: JsonRpcRequestPayload): void {
  const protocolVersion = headerValue(request.headers['mcp-protocol-version']);
  if (!protocolVersion) {
    throw new RpcProtocolError('缺少 MCP-Protocol-Version 请求头', 400, JSONRPC_HEADER_MISMATCH);
  }
  if (protocolVersion !== config.mcp.protocolVersion) {
    throw new RpcProtocolError(`当前仅支持协议版本 ${config.mcp.protocolVersion}`, 400, JSONRPC_HEADER_MISMATCH);
  }
  const rpcVersion = rpcMetaProtocolVersion(rpc);
  if (rpcVersion !== config.mcp.protocolVersion) {
    throw new RpcProtocolError('请求体 _meta 协议版本与服务端不匹配', 400, JSONRPC_HEADER_MISMATCH);
  }

  const methodHeader = decodeHeaderSentinel(headerValue(request.headers['mcp-method']));
  if (!methodHeader || methodHeader !== rpc.method) {
    throw new RpcProtocolError('Mcp-Method 请求头与 JSON-RPC method 不一致', 400, JSONRPC_HEADER_MISMATCH);
  }
  if (rpc.method === 'tools/call') {
    const toolNameHeader = decodeHeaderSentinel(headerValue(request.headers['mcp-name']));
    const toolName = String(rpc.params.name ?? '').trim();
    if (!toolNameHeader || toolNameHeader !== toolName) {
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
    _meta: { serverInfo: MODELING_MCP_SERVER_INFO },
  };
}

function legacyToolResult(payload: Record<string, unknown>): Record<string, unknown> {
  return {
    content: [{
      type: 'text',
      text: toolText(payload),
    }],
    structuredContent: payload,
    isError: false,
  };
}

function modernToolErrorResult(error: AgentCoreError): Record<string, unknown> {
  return {
    resultType: 'complete',
    content: [{
      type: 'text',
      text: error.message,
    }],
    structuredContent: {
      code: error.code,
      error: error.message,
      next: error.next,
      details: error.details,
    },
    isError: true,
    _meta: { serverInfo: MODELING_MCP_SERVER_INFO },
  };
}

function legacyToolErrorResult(error: AgentCoreError): Record<string, unknown> {
  return {
    content: [{
      type: 'text',
      text: error.message,
    }],
    structuredContent: {
      code: error.code,
      error: error.message,
      next: error.next,
      details: error.details,
    },
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

function rpcMetaProtocolVersion(rpc: JsonRpcRequestPayload): string {
  const meta = rpc.params._meta;
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return '';
  return String((meta as Record<string, unknown>)['io.modelcontextprotocol/protocolVersion'] ?? '').trim();
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
): FastifyReply {
  return reply.code(statusCode).send(rpcErrorEnvelope(id, code, message));
}

class RpcProtocolError extends Error {
  constructor(
    message: string,
    public readonly statusCode: number,
    public readonly jsonrpcCode: number,
  ) {
    super(message);
    this.name = 'RpcProtocolError';
  }
}

class SseSessionRegistry {
  private readonly sessions = new Map<string, SseSessionRecord>();

  create(
    endpointId: string,
    toolset: ModelingMcpToolset,
    send: SseSessionRecord['send'],
  ): SseSessionRecord {
    if (this.sessions.size >= MAX_SSE_SESSIONS) {
      throw new ModelingMcpError('MCP_SESSION_CAPACITY_REACHED', 'SSE 会话容量已满', 503);
    }
    while (true) {
      const sessionId = randomBytes(16).toString('hex');
      if (this.sessions.has(sessionId)) continue;
      const session = { sessionId, endpointId, toolset, send };
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
    return session;
  }

  release(sessionId: string): void {
    const normalized = String(sessionId ?? '').trim();
    if (!normalized) return;
    this.sessions.delete(normalized);
  }
}

const sseSessions = new SseSessionRegistry();
