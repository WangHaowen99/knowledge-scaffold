// rea-sidecar 入口：读环境变量，起 HTTP 服务（默认 8130）。说明见 README.md。
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createApp } from './src/app.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const env = process.env;
const mb = (name, fallback) => Number(env[name] || fallback) * 1024 * 1024;
const int = (name, fallback) => Number(env[name] || fallback);

const reaBin = env.REA_SIDECAR_REA_BIN || join(here, 'node_modules', 'rea-agents', 'scripts', 'rea.mjs');
let reaVersion = null;
try {
  // 版本取 REA 自己的 package.json，不起进程
  reaVersion = JSON.parse(readFileSync(join(dirname(reaBin), '..', 'package.json'), 'utf8')).version ?? null;
} catch {
  reaVersion = null;
}
const ownVersion = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8')).version;

const config = {
  scratch: env.REA_SIDECAR_SCRATCH || '/scratch',
  reaBin,
  reaVersion,
  provider: `rea-static@${ownVersion}`,
  runnerPath: join(here, 'src', 'runner.mjs'),
  runnerHeapMb: int('REA_SIDECAR_RUNNER_HEAP_MB', 1536),
  maxQueued: int('REA_SIDECAR_MAX_QUEUED', 4),
  maxBytes: mb('REA_SIDECAR_MAX_BYTES_MB', 2048),
  maxOutputBytes: mb('REA_SIDECAR_MAX_OUTPUT_MB', 512),
  maxEntries: int('REA_SIDECAR_MAX_ENTRIES', 20000),
  maxInventory: int('REA_SIDECAR_MAX_INVENTORY', 20000),
  maxAssemblies: int('REA_SIDECAR_MAX_ASSEMBLIES', 20),
  maxPrograms: int('REA_SIDECAR_MAX_PROGRAMS', 3),
  maxPyc: int('REA_SIDECAR_MAX_PYC', 50),
  ttlMs: int('REA_SIDECAR_TTL_S', 6 * 3600) * 1000,
};

const { server, manager } = createApp(config);
await manager.cleanupScratch();
server.listen(int('REA_SIDECAR_PORT', 8130), env.REA_SIDECAR_HOST || '0.0.0.0', () => {
  process.stdout.write(`rea-sidecar 监听 ${server.address().port}，REA ${reaVersion ?? '未安装'}\n`);
});
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    manager.shutdown();
    server.close();
    process.exit(0);
  });
}
