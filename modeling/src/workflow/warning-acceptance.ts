import { createHash } from 'node:crypto';

import { getDb, nowIso } from '../db/index.js';
import { getCheck, type CheckFinding } from './package-check.js';

export type WarningAcceptancePhase = 'validation' | 'release';
export type WarningAcceptanceRole = 'project_owner' | 'modeler' | 'domain_expert' | 'validator';

export interface PackageWarningReviewItem {
  fingerprint: string;
  finding: CheckFinding;
  accepted: boolean;
  impactNote?: string;
  acceptedBy?: string;
  acceptedRole?: string;
  acceptedAt?: string;
  releaseConfirmed: boolean;
  releaseNote?: string;
  releaseConfirmedBy?: string;
  releaseConfirmedAt?: string;
}

export interface PackageWarningReview {
  revision: string;
  items: PackageWarningReviewItem[];
  warningTotal: number;
  pendingValidation: number;
  pendingReleaseConfirmation: number;
  releaseBlocking: number;
  readyForValidation: boolean;
  readyForRelease: boolean;
  knownLimitations: Array<{
    fingerprint: string;
    category: string;
    target: string;
    message: string;
    impactNote: string;
    releaseNote: string;
  }>;
}

function stableFinding(finding: CheckFinding) {
  return {
    source: finding.source,
    severity: finding.severity,
    category: finding.category,
    target: finding.target,
    message: finding.message,
    releaseBlocking: finding.releaseBlocking === true,
  };
}

export function warningFingerprint(finding: CheckFinding): string {
  return createHash('sha256').update(JSON.stringify(stableFinding(finding))).digest('hex');
}

function acceptanceRows(projectId: string, revision: string): Map<string, any> {
  const rows = getDb().prepare(`SELECT * FROM package_warning_acceptances
    WHERE project_id = ? AND revision_oid = ?`).all(projectId, revision) as any[];
  return new Map(rows.map((row) => [row.finding_fingerprint, row]));
}

export function getPackageWarningReview(projectId: string, revision: string): PackageWarningReview {
  const check = getCheck(projectId, revision);
  const warnings = check?.findings.filter((finding) => finding.severity === '警告') ?? [];
  const rows = acceptanceRows(projectId, revision);
  const items = warnings.map((finding) => {
    const fingerprint = warningFingerprint(finding);
    const row = rows.get(fingerprint);
    return {
      fingerprint,
      finding,
      accepted: Boolean(row),
      ...(row?.impact_note ? { impactNote: row.impact_note } : {}),
      ...(row?.accepted_by ? { acceptedBy: row.accepted_by } : {}),
      ...(row?.accepted_role ? { acceptedRole: row.accepted_role } : {}),
      ...(row?.accepted_at ? { acceptedAt: row.accepted_at } : {}),
      releaseConfirmed: Boolean(row?.release_confirmed_at),
      ...(row?.release_note ? { releaseNote: row.release_note } : {}),
      ...(row?.release_confirmed_by ? { releaseConfirmedBy: row.release_confirmed_by } : {}),
      ...(row?.release_confirmed_at ? { releaseConfirmedAt: row.release_confirmed_at } : {}),
    } satisfies PackageWarningReviewItem;
  });
  const releaseBlocking = items.filter((item) => item.finding.releaseBlocking === true).length;
  const pendingValidation = items.filter((item) => !item.accepted).length;
  const pendingReleaseConfirmation = items.filter((item) => (
    item.finding.releaseBlocking !== true && (!item.accepted || !item.releaseConfirmed)
  )).length;
  const knownLimitations = items.filter((item) => (
    item.accepted && item.releaseConfirmed && item.finding.releaseBlocking !== true
  )).map((item) => ({
    fingerprint: item.fingerprint,
    category: item.finding.category,
    target: item.finding.target,
    message: item.finding.message,
    impactNote: item.impactNote ?? '',
    releaseNote: item.releaseNote ?? '',
  }));
  return {
    revision,
    items,
    warningTotal: items.length,
    pendingValidation,
    pendingReleaseConfirmation,
    releaseBlocking,
    readyForValidation: pendingValidation === 0,
    readyForRelease: pendingValidation === 0
      && pendingReleaseConfirmation === 0
      && releaseBlocking === 0,
    knownLimitations,
  };
}

export function acceptPackageWarnings(input: {
  projectId: string;
  revision: string;
  fingerprints: string[];
  note: string;
  actor: string;
  actorRole: WarningAcceptanceRole;
  phase: WarningAcceptancePhase;
}): PackageWarningReview {
  const note = input.note.trim();
  if (note.length < 4) throw new Error('请填写警告影响和接受理由');
  if (note.length > 2_000) throw new Error('警告接受说明过长');
  const check = getCheck(input.projectId, input.revision);
  if (!check) {
    throw new Error('知识包检查已过期，请重新检查后再确认警告');
  }
  const review = getPackageWarningReview(input.projectId, input.revision);
  const requested = new Set(input.fingerprints);
  const selected = review.items.filter((item) => requested.has(item.fingerprint));
  if (!selected.length || selected.length !== requested.size) throw new Error('请选择当前修订中的警告');
  if (input.phase === 'release') {
    if (selected.some((item) => item.finding.releaseBlocking === true)) {
      throw new Error('发布阻断警告必须修复，不能作为已知限制接受');
    }
    if (selected.some((item) => !item.accepted)) throw new Error('请先在验证阶段接受警告影响');
  }
  const db = getDb();
  const timestamp = nowIso();
  if (input.phase === 'validation') {
    const statement = db.prepare(`INSERT INTO package_warning_acceptances (
      project_id, revision_oid, finding_fingerprint, finding_json, impact_note,
      accepted_by, accepted_role, accepted_at
    ) VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(project_id, revision_oid, finding_fingerprint) DO UPDATE SET
      finding_json = excluded.finding_json,
      impact_note = excluded.impact_note,
      accepted_by = excluded.accepted_by,
      accepted_role = excluded.accepted_role,
      accepted_at = excluded.accepted_at,
      release_note = NULL,
      release_confirmed_by = NULL,
      release_confirmed_role = NULL,
      release_confirmed_at = NULL`);
    for (const item of selected) statement.run(
      input.projectId,
      input.revision,
      item.fingerprint,
      JSON.stringify(item.finding),
      note,
      input.actor,
      input.actorRole,
      timestamp,
    );
  } else {
    const statement = db.prepare(`UPDATE package_warning_acceptances SET
      release_note = ?, release_confirmed_by = ?, release_confirmed_role = ?, release_confirmed_at = ?
      WHERE project_id = ? AND revision_oid = ? AND finding_fingerprint = ?`);
    for (const item of selected) statement.run(
      note,
      input.actor,
      input.actorRole,
      timestamp,
      input.projectId,
      input.revision,
      item.fingerprint,
    );
  }
  return getPackageWarningReview(input.projectId, input.revision);
}

export function saveReleaseKnownLimitations(input: {
  projectId: string;
  version: string;
  revision: string;
  knownLimitations: PackageWarningReview['knownLimitations'];
  actor: string;
}): void {
  getDb().prepare(`INSERT INTO package_release_records (
    project_id, version, revision_oid, known_limitations_json, released_by, released_at
  ) VALUES (?,?,?,?,?,?)
  ON CONFLICT(project_id, version) DO UPDATE SET
    revision_oid = excluded.revision_oid,
    known_limitations_json = excluded.known_limitations_json,
    released_by = excluded.released_by,
    released_at = excluded.released_at`).run(
      input.projectId,
      input.version,
      input.revision,
      JSON.stringify(input.knownLimitations),
      input.actor,
      nowIso(),
    );
}

export function getReleaseKnownLimitations(
  projectId: string,
  version: string,
): PackageWarningReview['knownLimitations'] {
  const row = getDb().prepare(`SELECT known_limitations_json FROM package_release_records
    WHERE project_id = ? AND version = ?`).get(projectId, version) as any;
  return row ? JSON.parse(row.known_limitations_json) : [];
}
