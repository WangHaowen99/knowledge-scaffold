import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

import { getDb, nowIso } from '../db/index.js';
import type { AuthUser } from '../types.js';

const CREDENTIAL_TTL_MS = 24 * 60 * 60 * 1000;

function fingerprint(value: string): string {
  return createHash('sha256').update(value).digest('hex');
}

export function issueAgentRunCredential(projectId: string, runId: string, owner: string): string {
  const credential = `arc_${randomBytes(32).toString('base64url')}`;
  const expiresAt = new Date(Date.now() + CREDENTIAL_TTL_MS).toISOString();
  const result = getDb().prepare(`UPDATE agent_candidate_workspaces
    SET credential_hash = ?, credential_expires_at = ?, updated_at = ?
    WHERE project_id = ? AND run_id = ? AND owner = ?`)
    .run(fingerprint(credential), expiresAt, nowIso(), projectId, runId, owner);
  if (!Number(result.changes)) throw new Error('Agent 候选工作区不存在，无法签发运行凭据');
  return credential;
}

export function authenticateAgentRunCredential(
  projectId: string,
  runId: string,
  credential: string,
): AuthUser | null {
  if (!projectId || !runId || !credential) return null;
  const row = getDb().prepare(`SELECT owner, credential_hash, credential_expires_at, lease_expires_at
    FROM agent_candidate_workspaces WHERE project_id = ? AND run_id = ?`)
    .get(projectId, runId) as {
      owner: string;
      credential_hash: string | null;
      credential_expires_at: string | null;
      lease_expires_at: string;
    } | undefined;
  if (!row?.credential_hash || !row.credential_expires_at) return null;
  const now = Date.now();
  if (Date.parse(row.credential_expires_at) <= now || Date.parse(row.lease_expires_at) <= now) return null;
  const expected = Buffer.from(row.credential_hash, 'hex');
  const actual = Buffer.from(fingerprint(credential), 'hex');
  if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) return null;
  getDb().prepare(`UPDATE agent_candidate_workspaces
    SET credential_expires_at = ?, updated_at = ? WHERE project_id = ? AND run_id = ?`)
    .run(new Date(now + CREDENTIAL_TTL_MS).toISOString(), nowIso(), projectId, runId);
  return { username: row.owner, role: 'modeler' };
}
