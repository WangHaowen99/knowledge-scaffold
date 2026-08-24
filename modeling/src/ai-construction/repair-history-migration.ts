import type { CheckFinding } from '../workflow/package-check.js';
import {
  blockingFindingFingerprints,
  type RepairQualityOutcome,
} from './repair-quality.js';

export interface HistoricalRepairAssessment {
  qualityOutcome: RepairQualityOutcome;
  targetFingerprints: string[];
  resolvedFingerprints: string[];
  introducedFingerprints: string[];
  beforeBlockingCount: number;
  afterBlockingCount: number | null;
}

/**
 * 旧记录没有“本轮目标”字段，只能按 before/after 的完整阻断集合保守回算。
 * 缺任一侧事实时保留 unknown_legacy，绝不根据 completed 状态猜测成功。
 */
export function assessHistoricalRepair(
  before: CheckFinding[] | null,
  after: CheckFinding[] | null,
): HistoricalRepairAssessment {
  const beforeFingerprints = before ? blockingFindingFingerprints(before) : [];
  if (!before || !after) {
    return {
      qualityOutcome: 'unknown_legacy',
      targetFingerprints: beforeFingerprints,
      resolvedFingerprints: [],
      introducedFingerprints: [],
      beforeBlockingCount: beforeFingerprints.length,
      afterBlockingCount: null,
    };
  }
  const afterFingerprints = blockingFindingFingerprints(after);
  const beforeSet = new Set(beforeFingerprints);
  const afterSet = new Set(afterFingerprints);
  const resolved = beforeFingerprints.filter((fingerprint) => !afterSet.has(fingerprint));
  const introduced = afterFingerprints.filter((fingerprint) => !beforeSet.has(fingerprint));
  const qualityOutcome: RepairQualityOutcome = afterFingerprints.length === 0
    ? 'resolved'
    : introduced.length > 0 || afterFingerprints.length > beforeFingerprints.length
      ? 'regressed'
      : resolved.length > 0 ? 'improved' : 'unresolved';
  return {
    qualityOutcome,
    targetFingerprints: beforeFingerprints,
    resolvedFingerprints: resolved,
    introducedFingerprints: introduced,
    beforeBlockingCount: beforeFingerprints.length,
    afterBlockingCount: afterFingerprints.length,
  };
}
