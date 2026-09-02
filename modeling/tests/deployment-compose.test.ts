import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { parse } from 'yaml';

const modelingRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const composePaths = [
  'deploy/docker-compose.release.yml',
  '../deploy/docker-compose.modeling.yml',
];

interface ComposeService {
  extra_hosts?: string[];
  ports?: string[];
  environment?: string[];
}

interface ModelingCompose {
  services?: Record<string, ComposeService>;
}

function readCompose(relativePath: string): ModelingCompose {
  return parse(readFileSync(resolve(modelingRoot, relativePath), 'utf8')) as ModelingCompose;
}

for (const relativePath of composePaths) {
  test(`${relativePath} 为 Linux 容器提供 Consumer 宿主机网关`, () => {
    const compose = readCompose(relativePath);
    assert.ok(
      compose.services?.modeling?.extra_hosts?.includes('host.docker.internal:host-gateway'),
    );
  });

  test(`${relativePath} 由 modeling 单进程同时提供 API 与 MCP`, () => {
    const compose = readCompose(relativePath);
    const modeling = compose.services?.modeling;
    assert.ok(modeling);
    assert.equal(compose.services?.['modeling-mcp'], undefined);
    assert.ok(modeling.ports?.some((value) => value.includes(':5100')));
    assert.ok(modeling.ports?.some((value) => value.includes(':5188')));

    const environment = new Map((modeling.environment ?? []).map((entry) => {
      const separator = entry.indexOf('=');
      return [entry.slice(0, separator), entry.slice(separator + 1)];
    }));
    assert.equal(environment.get('MODELING_MCP_HOST'), '0.0.0.0');
    assert.equal(environment.get('MODELING_MCP_PORT'), '5188');
    for (const name of [
      'MODELING_MCP_MAX_SSE_SESSIONS',
      'MODELING_MCP_MAX_SSE_SESSIONS_PER_TOKEN',
      'MODELING_MCP_MAX_SSE_SESSIONS_PER_PROJECT',
      'MODELING_MCP_MAX_SSE_SESSIONS_PER_USER',
      'MODELING_MCP_MAX_SSE_SESSIONS_PER_IP',
      'MODELING_MCP_SSE_MAX_LIFETIME_SECONDS',
      'MODELING_MCP_SSE_IDLE_TIMEOUT_SECONDS',
    ]) {
      assert.ok(environment.has(name), `${relativePath} 缺少 ${name}`);
    }
  });
}

test('Modeling 启动入口在同一 Node 进程启动 MCP，且不保留独立启动脚本', () => {
  const packageJson = JSON.parse(readFileSync(resolve(modelingRoot, 'package.json'), 'utf8')) as {
    scripts?: Record<string, string>;
  };
  assert.equal(packageJson.scripts?.['mcp:dev'], undefined);
  assert.equal(packageJson.scripts?.['mcp:start'], undefined);
  assert.equal(packageJson.scripts?.start, 'node dist/server.js');

  const serverSource = readFileSync(resolve(modelingRoot, 'src/server.ts'), 'utf8');
  const mcpServerSource = readFileSync(resolve(modelingRoot, 'src/mcp-server.ts'), 'utf8');
  assert.match(serverSource, /startModelingMcpServer\(\)/);
  assert.match(mcpServerSource, /export async function startModelingMcpServer/);
  assert.doesNotMatch(mcpServerSource, /process\.once\(['"]SIG(?:TERM|INT)/);
  assert.doesNotMatch(mcpServerSource, /main\(\)\.catch/);
});

test('Modeling 标准部署会清理旧版独立 MCP 容器', () => {
  const stackScript = readFileSync(resolve(modelingRoot, '../deploy/deploy-stack.sh'), 'utf8');
  const modelingDeployScript = readFileSync(resolve(modelingRoot, 'deploy/deploy.sh'), 'utf8');
  assert.match(stackScript, /modeling_compose[^\n]*up -d --build --remove-orphans/);
  assert.equal((modelingDeployScript.match(/up -d(?: --build)? --remove-orphans/g) ?? []).length, 2);
});
