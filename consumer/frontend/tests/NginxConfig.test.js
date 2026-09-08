import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const nginxConfig = readFileSync(
  fileURLToPath(new URL('../nginx.conf', import.meta.url)),
  'utf8',
);

test('frontend proxy resolves the Docker backend alias dynamically', () => {
  assert.match(nginxConfig, /resolver 127\.0\.0\.11 valid=10s ipv6=off;/);
  assert.match(nginxConfig, /resolver_timeout 5s;/);
  assert.match(
    nginxConfig,
    /set \$backend_upstream "__NGINX_BACKEND_UPSTREAM__";/,
  );

  const dynamicProxyCount = (nginxConfig.match(/proxy_pass \$backend_upstream;/g) || []).length;
  assert.equal(dynamicProxyCount, 4);
  assert.doesNotMatch(nginxConfig, /proxy_pass __NGINX_BACKEND_UPSTREAM__/);
});
