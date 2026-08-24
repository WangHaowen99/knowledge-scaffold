import {
  checkFindingFingerprint,
  type CheckFinding,
} from '../workflow/package-check.js';

export type RepairExecutionStatus =
  | 'pending'
  | 'running'
  | 'waiting_user'
  | 'validating'
  | 'finished'
  | 'failed';

export type RepairQualityOutcome =
  | 'unresolved'
  | 'resolved'
  | 'improved'
  | 'regressed'
  | 'stale'
  | 'rejected'
  | 'unknown_legacy';

export interface RepairQualityGate {
  passed: boolean;
  outcome: Exclude<RepairQualityOutcome, 'stale' | 'rejected' | 'unknown_legacy'>;
  targetFingerprints: string[];
  resolvedFingerprints: string[];
  remainingFingerprints: string[];
  introducedFingerprints: string[];
  unexpectedPaths: string[];
  validationFailures: string[];
  beforeBlockingCount: number;
  afterBlockingCount: number;
  reasons: string[];
}

export interface RepairQualityGateInput {
  before: CheckFinding[];
  after: CheckFinding[];
  targetFingerprints?: string[];
  changedPaths?: string[];
  allowedPaths?: string[];
  validationFailures?: string[];
}

export function repairFindingFingerprint(finding: CheckFinding): string {
  const stored = String(finding.fingerprint ?? '').trim();
  return stored || checkFindingFingerprint(finding);
}

export function blockingFindingFingerprints(findings: CheckFinding[]): string[] {
  return [...new Set(findings
    .filter((finding) => finding.severity === '错误')
    .map(repairFindingFingerprint))].sort();
}

function pathAllowed(path: string, allowedPaths: string[]): boolean {
  return allowedPaths.some((allowed) => {
    if (allowed.endsWith('/**')) return path.startsWith(allowed.slice(0, -2));
    return path === allowed;
  });
}

/**
 * 修复质量只比较阻断发现的稳定 fingerprint。展示文案、临时 finding id 和严重级别
 * 之外的改写不会把同一问题误算成新问题。
 */
export function evaluateRepairQualityGate(input: RepairQualityGateInput): RepairQualityGate {
  const before = blockingFindingFingerprints(input.before);
  const after = blockingFindingFingerprints(input.after);
  const beforeSet = new Set(before);
  const afterSet = new Set(after);
  const requestedTargets = (input.targetFingerprints ?? [])
    .map((value) => String(value).trim())
    .filter(Boolean);
  const targets = [...new Set(requestedTargets.length ? requestedTargets : before)].sort();
  const resolved = before.filter((fingerprint) => !afterSet.has(fingerprint));
  const remaining = after.filter((fingerprint) => beforeSet.has(fingerprint));
  const introduced = after.filter((fingerprint) => !beforeSet.has(fingerprint));
  const unresolvedTargets = targets.filter((fingerprint) => afterSet.has(fingerprint));
  const changedPaths = [...new Set((input.changedPaths ?? []).map(String).filter(Boolean))].sort();
  const allowedPaths = [...new Set((input.allowedPaths ?? []).map(String).filter(Boolean))].sort();
  const unexpectedPaths = allowedPaths.length
    ? changedPaths.filter((path) => !pathAllowed(path, allowedPaths))
    : [];
  const validationFailures = [...new Set((input.validationFailures ?? [])
    .map(String).map((value) => value.trim()).filter(Boolean))];
  const passed = unresolvedTargets.length === 0
    && introduced.length === 0
    && unexpectedPaths.length === 0
    && validationFailures.length === 0;
  const outcome: RepairQualityGate['outcome'] = validationFailures.length > 0
    ? 'unresolved'
    : passed
      ? after.length === 0 ? 'resolved' : 'improved'
      : introduced.length > 0 || after.length > before.length ? 'regressed' : 'unresolved';
  const reasons = [
    unresolvedTargets.length
      ? `本轮目标问题仍有 ${unresolvedTargets.length} 项未解决` : '',
    introduced.length
      ? `候选引入了 ${introduced.length} 项新的必须修复问题` : '',
    unexpectedPaths.length
      ? `候选修改了 ${unexpectedPaths.length} 个计划外文件` : '',
    validationFailures.length
      ? `候选复检未完整执行：${validationFailures.join('；')}` : '',
  ].filter(Boolean);

  return {
    passed,
    outcome,
    targetFingerprints: targets,
    resolvedFingerprints: resolved,
    remainingFingerprints: remaining,
    introducedFingerprints: introduced,
    unexpectedPaths,
    validationFailures,
    beforeBlockingCount: before.length,
    afterBlockingCount: after.length,
    reasons,
  };
}
