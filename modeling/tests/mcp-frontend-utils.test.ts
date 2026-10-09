import assert from 'node:assert/strict';
import test from 'node:test';

import { copyMcpText } from '../frontend/src/mcpClipboard.js';
import { buildModelingMcpClientConfigs } from '../frontend/src/mcpClientConfig.js';

test('MCP clipboard 优先使用 Clipboard API', async () => {
  let copied = '';
  const result = await copyMcpText('token-value', {
    navigatorRef: { clipboard: { writeText: async (value: string) => { copied = value; } } },
    documentRef: undefined,
  });

  assert.deepEqual(result, { ok: true, error: '' });
  assert.equal(copied, 'token-value');
});

test('MCP clipboard 在 Clipboard API 被拒绝时降级到选择复制', async () => {
  let appended = false;
  let removed = false;
  let selected = false;
  const textarea: any = {
    value: '',
    style: {},
    setAttribute() {},
    focus() {},
    select() { selected = true; },
    parentNode: { removeChild() { removed = true; } },
  };
  const result = await copyMcpText('fallback-value', {
    navigatorRef: { clipboard: { writeText: async () => { throw new Error('denied'); } } },
    documentRef: {
      createElement: () => textarea,
      body: { appendChild() { appended = true; } },
      execCommand: (command: string) => command === 'copy',
    },
  });

  assert.deepEqual(result, { ok: true, error: '' });
  assert.equal(textarea.value, 'fallback-value');
  assert.equal(appended, true);
  assert.equal(selected, true);
  assert.equal(removed, true);
});

test('MCP clipboard 在浏览器不支持复制时返回可操作提示', async () => {
  const result = await copyMcpText('value', { navigatorRef: {}, documentRef: {} });
  assert.deepEqual(result, { ok: false, error: '浏览器不支持自动复制，请手工选择内容' });
});

const runtime = {
  urls: [
    { toolset: 'read', transport: 'sse', url: 'https://mcp.example/read/sse' },
    { toolset: 'read', transport: 'streamable-http', url: 'https://mcp.example/read/mcp' },
    { toolset: 'change', transport: 'streamable-http', url: 'https://mcp.example/change/mcp' },
  ],
};

test('MCP 客户端配置按完整 Token scope 生成 Read、Change 和组合 JSON', () => {
  const configs = buildModelingMcpClientConfigs(runtime, 'secret-token', [
    'ontology.read',
    'ontology.change.validate',
    'ontology.change.apply',
  ]);

  const read = JSON.parse(configs.read);
  const change = JSON.parse(configs.change);
  const combined = JSON.parse(configs.combined);
  assert.equal(read.mcpServers['modeling-read'].url, 'https://mcp.example/read/mcp');
  assert.equal(read.mcpServers['modeling-read'].headers.Authorization, 'Bearer secret-token');
  assert.equal(change.mcpServers['modeling-change'].url, 'https://mcp.example/change/mcp');
  assert.deepEqual(Object.keys(combined.mcpServers), ['modeling-read', 'modeling-change']);
});

test('MCP Read-only Token 只生成 Read 配置', () => {
  const configs = buildModelingMcpClientConfigs(runtime, 'read-token', ['ontology.read']);

  assert.match(configs.read, /Bearer read-token/u);
  assert.equal(configs.change, '');
  assert.equal(configs.combined, '');
});

for (const scope of ['ontology.change.validate', 'ontology.change.apply']) {
  test(`MCP ${scope} Token 只生成 Change 配置`, () => {
    const configs = buildModelingMcpClientConfigs(runtime, 'change-token', [scope]);

    assert.equal(configs.read, '');
    assert.match(configs.change, /Bearer change-token/u);
    assert.equal(configs.combined, '');
  });
}

test('MCP 空 scope Token 不生成客户端配置', () => {
  const configs = buildModelingMcpClientConfigs(runtime, 'empty-token', []);

  assert.deepEqual(configs, { read: '', change: '', combined: '' });
});

test('MCP 客户端配置刷新后使用 Token 占位符并展示全部模板', () => {
  const configs = buildModelingMcpClientConfigs(runtime);

  assert.match(configs.read, /Bearer <MCP_TOKEN>/u);
  assert.match(configs.change, /Bearer <MCP_TOKEN>/u);
  assert.notEqual(configs.combined, '');
});
