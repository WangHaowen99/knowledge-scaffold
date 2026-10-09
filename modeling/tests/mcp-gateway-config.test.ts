import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const gatewayTemplate = readFileSync(
  new URL('../deploy/mcp-gateway/nginx.conf.template', import.meta.url),
  'utf8',
);

function gatewayLocationPattern(): RegExp {
  const match = gatewayTemplate.match(/location\s+~\s+"([^"]+)"\s*\{/u);
  assert.ok(match, 'MCP Gateway 必须包含正则 location');
  return new RegExp(match[1], 'u');
}

test('MCP Gateway 代理 Streamable HTTP、SSE 和 SSE messages 路径', () => {
  const pattern = gatewayLocationPattern();
  const endpointId = '0123456789abcdef';
  const supportedPaths = [
    `/${endpointId}/read/mcp`,
    `/${endpointId}/change/mcp`,
    `/${endpointId}/read/sse`,
    `/${endpointId}/change/sse`,
    `/${endpointId}/read/sse/messages`,
    `/${endpointId}/read/sse/messages/`,
    `/${endpointId}/change/sse/messages`,
    `/${endpointId}/change/sse/messages/`,
  ];

  for (const path of supportedPaths) assert.match(path, pattern);
  assert.match(
    new URL(`https://mcp.example.com/${endpointId}/read/sse/messages/?session_id=synthetic`).pathname,
    pattern,
  );
});

test('MCP Gateway 继续拒绝非法 endpoint、toolset 和额外路径', () => {
  const pattern = gatewayLocationPattern();
  const rejectedPaths = [
    '/0123456789abcde/read/mcp',
    '/0123456789abcdef/chat/mcp',
    '/0123456789abcdef/read/sse/messages/extra',
    '/0123456789abcdef/read/tools/list',
  ];

  for (const path of rejectedPaths) assert.doesNotMatch(path, pattern);
});
