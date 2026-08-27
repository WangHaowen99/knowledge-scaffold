import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { parse } from 'yaml';

const modelingRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

for (const relativePath of [
  'deploy/docker-compose.release.yml',
  '../deploy/docker-compose.modeling.yml',
]) {
  test(`${relativePath} 为 Linux 容器提供 Consumer 宿主机网关`, () => {
    const compose = parse(readFileSync(resolve(modelingRoot, relativePath), 'utf8')) as {
      services?: { modeling?: { extra_hosts?: string[] } };
    };
    assert.ok(
      compose.services?.modeling?.extra_hosts?.includes('host.docker.internal:host-gateway'),
    );
  });
}
