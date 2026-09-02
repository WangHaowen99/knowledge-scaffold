import { createHash } from 'node:crypto';

import { getDb, nowIso } from '../db/index.js';

export interface ScriptCandidateChange {
  path: string;
  kind: 'write' | 'delete';
  content: string | Uint8Array | null;
}

export interface ScriptTestReceiptInput {
  projectId: string;
  runId: string;
  scriptPath: string;
  source: string | Uint8Array;
  total: number;
  passed: number;
  failed: number;
  sandbox: 'jiuwenbox';
}

export function scriptTestSourceHash(source: string | Uint8Array): string {
  const text = typeof source === 'string' ? source : Buffer.from(source).toString('utf8');
  const canonical = `${text.replace(/\r\n?/gu, '\n').replace(/\n+$/u, '')}\n`;
  return `sha256:${createHash('sha256').update('agent-script-test-v1\0').update(canonical).digest('hex')}`;
}

export function recordScriptTestReceipt(input: ScriptTestReceiptInput): void {
  if (input.total < 1 || input.passed !== input.total || input.failed !== 0) {
    throw new Error(`脚本测试回执只能记录全量通过结果：${input.scriptPath}`);
  }
  getDb().prepare(`INSERT INTO agent_script_test_receipts (
    project_id, run_id, script_path, source_hash, total, passed, failed, sandbox, created_at
  ) VALUES (?,?,?,?,?,?,?,?,?)
  ON CONFLICT(project_id, run_id, script_path, source_hash) DO UPDATE SET
    total = excluded.total,
    passed = excluded.passed,
    failed = excluded.failed,
    sandbox = excluded.sandbox,
    created_at = excluded.created_at`)
    .run(
      input.projectId,
      input.runId,
      input.scriptPath,
      scriptTestSourceHash(input.source),
      input.total,
      input.passed,
      input.failed,
      input.sandbox,
      nowIso(),
    );
}

export function untestedScriptPaths(
  projectId: string,
  runId: string,
  changes: ScriptCandidateChange[],
  db = getDb(),
): string[] {
  const publicPath = (path: string) => path.replace(/^package\//u, '');
  const scriptWrites = changes.filter((change) => (
    change.kind === 'write' && publicPath(change.path).startsWith('scripts/')
  ));
  if (!scriptWrites.length) return [];
  const executableWrites = scriptWrites.filter((change) => publicPath(change.path).endsWith('.py'));
  if (!executableWrites.length) return scriptWrites.map((change) => publicPath(change.path));
  return executableWrites.filter((change) => {
    const path = publicPath(change.path);
    const sourceHash = scriptTestSourceHash(change.content ?? '');
    return !db.prepare(`SELECT 1 FROM agent_script_test_receipts
      WHERE project_id = ? AND run_id = ? AND script_path = ? AND source_hash = ?`)
      .get(projectId, runId, path, sourceHash);
  }).map((change) => publicPath(change.path));
}
