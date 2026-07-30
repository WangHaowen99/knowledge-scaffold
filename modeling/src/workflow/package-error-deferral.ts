import { createHash } from 'node:crypto';

import { getDb, nowIso } from '../db/index.js';
import { getCheck, type CheckFinding } from './package-check.js';

export type PackageErrorDeferralRole = 'project_owner' | 'modeler' | 'domain_expert' | 'validator';

export interface PackageErrorDeferral {
  projectId: string;
  revision: string;
  errorCount: number;
  findings: CheckFinding[];
  reason: string;
  actor: string;
  actorRole: PackageErrorDeferralRole;
  deferredAt: string;
  valid: boolean;
}

function findingFingerprint(finding: CheckFinding): string {
  return createHash('sha256').update(JSON.stringify({
    source: finding.source,
    severity: finding.severity,
    category: finding.category,
    target: finding.target,
    message: finding.message,
  })).digest('hex');
}

function fingerprints(findings: CheckFinding[]): string[] {
  return findings.map(findingFingerprint).sort();
}

export function getPackageErrorDeferral(
  projectId: string,
  revision: string,
): PackageErrorDeferral | null {
  const row = getDb().prepare(`SELECT * FROM package_error_deferrals
    WHERE project_id = ? AND revision_oid = ?`).get(projectId, revision) as any;
  if (!row) return null;
  const check = getCheck(projectId);
  const currentFindings = check?.revision === revision
    ? check.findings.filter((finding) => finding.severity === '错误')
    : [];
  const recordedFingerprints = JSON.parse(row.error_fingerprints_json) as string[];
  return {
    projectId: row.project_id,
    revision: row.revision_oid,
    errorCount: Number(row.error_count),
    findings: JSON.parse(row.findings_json) as CheckFinding[],
    reason: row.reason,
    actor: row.deferred_by,
    actorRole: row.deferred_role,
    deferredAt: row.deferred_at,
    valid: currentFindings.length > 0
      && JSON.stringify(fingerprints(currentFindings))
        === JSON.stringify([...recordedFingerprints].sort()),
  };
}

export function deferPackageErrors(input: {
  projectId: string;
  revision: string;
  reason: string;
  actor: string;
  actorRole: PackageErrorDeferralRole;
}): PackageErrorDeferral {
  const reason = input.reason.trim();
  if (reason.length < 4) throw new Error('请说明暂缓阻断错误的原因');
  if (reason.length > 2_000) throw new Error('暂缓原因过长');
  const check = getCheck(input.projectId);
  if (!check || check.revision !== input.revision) {
    throw new Error('知识包检查已过期，请重新检查后再决定是否暂缓');
  }
  const findings = check.findings.filter((finding) => finding.severity === '错误');
  if (!findings.length) throw new Error('当前知识包没有需要暂缓的阻断错误');
  const deferredAt = nowIso();
  getDb().prepare(`INSERT INTO package_error_deferrals (
    project_id, revision_oid, error_count, error_fingerprints_json, findings_json, reason,
    deferred_by, deferred_role, deferred_at
  ) VALUES (?,?,?,?,?,?,?,?,?)
  ON CONFLICT(project_id, revision_oid) DO UPDATE SET
    error_count = excluded.error_count,
    error_fingerprints_json = excluded.error_fingerprints_json,
    findings_json = excluded.findings_json,
    reason = excluded.reason,
    deferred_by = excluded.deferred_by,
    deferred_role = excluded.deferred_role,
    deferred_at = excluded.deferred_at`).run(
    input.projectId,
    input.revision,
    findings.length,
    JSON.stringify(fingerprints(findings)),
    JSON.stringify(findings),
    reason,
    input.actor,
    input.actorRole,
    deferredAt,
  );
  return getPackageErrorDeferral(input.projectId, input.revision)!;
}
