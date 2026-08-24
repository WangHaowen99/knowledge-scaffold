import {
  classifyRepairFinding,
  ControlledRepairAttemptStore,
  RepairQualityGateRejectedError,
  repairFindingFromPayload,
} from '../ai-construction/controlled-repair.js';
import {
  blockingFindingFingerprints,
  repairFindingFingerprint,
} from '../ai-construction/repair-quality.js';
import type { CheckFinding, PackageCheckResult } from '../workflow/package-check.js';
import {
  blockingPendingInteractions,
  isStaleRevisionBoundInteraction,
} from './construction-completion.js';
import type { AuthoringInteraction, JsonObject } from './types.js';
import { AuthoringState } from './state.js';

export interface PackageRepairQueueItem {
  interactionId: string;
  title: string;
  status: 'current' | 'queued';
  order: number;
  purpose: string;
  location: 'assistant' | 'scenario';
  disposition?: string;
  dependsOn: string[];
}

export interface PackageRepairState {
  revision: string;
  blockingCount: number;
  validationIncomplete: boolean;
  currentStep: {
    id: 'package_repair';
    status: 'ready' | 'running' | 'waiting_user' | 'completed' | 'failed';
  };
  attempt: ReturnType<ControlledRepairAttemptStore['latestForProject']> | null;
  findings: Array<{
    id: string;
    fingerprint: string;
    category: string;
    target: string;
    message: string;
    disposition: string;
    authority: string;
    reason: string;
    code?: string;
    facts?: Record<string, unknown>;
    impact?: string;
  }>;
  pendingQueue: PackageRepairQueueItem[];
  nextActions: JsonObject[];
}

function queueMetadata(interaction: AuthoringInteraction): {
  order: number;
  dependsOn: string[];
  disposition?: string;
} {
  const queue = interaction.payload.queue;
  const record = queue && typeof queue === 'object' && !Array.isArray(queue)
    ? queue as JsonObject : {};
  const order = Number(record.order);
  const dependsOn = Array.isArray(record.dependsOn)
    ? record.dependsOn.map(String).filter(Boolean) : [];
  const disposition = String(interaction.payload.disposition ?? '').trim();
  return {
    order: Number.isFinite(order) ? Math.max(0, Math.trunc(order)) : Number.MAX_SAFE_INTEGER,
    dependsOn,
    ...(disposition ? { disposition } : {}),
  };
}

function cancelStaleRepairInteractions(
  projectId: string,
  revision: string,
  state: AuthoringState,
  attempts: ControlledRepairAttemptStore,
): void {
  const stale = state.snapshot(projectId).pendingInteractions.filter((interaction) => (
    isStaleRevisionBoundInteraction(interaction, revision)
      && ['controlled_repair', 'controlled_repair_expert'].includes(
        String(interaction.payload.purpose ?? ''),
      )
  ));
  for (const interaction of stale) {
    const attemptId = String(interaction.payload.repairAttemptId ?? '').trim();
    if (attemptId) {
      try { attempts.markStale(attemptId, '知识包版本已变化，旧修复待办已失效'); } catch { /* 历史审计可能已结束 */ }
    }
    try {
      state.apply({
        kind: 'update_run',
        projectId,
        runId: interaction.runId,
        status: 'cancelled',
        result: { purpose: 'controlled_repair', stale: true, currentRevision: revision },
      });
    } catch { /* 已结束的运行无需重复关闭 */ }
  }
}

function interactionFindingFingerprints(interaction: AuthoringInteraction): string[] {
  const stored = Array.isArray(interaction.payload.findingFingerprints)
    ? interaction.payload.findingFingerprints.map(String).filter(Boolean)
    : [];
  if (stored.length) return stored;
  try {
    return [repairFindingFingerprint(repairFindingFromPayload(interaction.payload.finding))];
  } catch {
    return [];
  }
}

function closeResolvedRepairInteractions(input: {
  projectId: string;
  revision: string;
  findings: CheckFinding[];
  state: AuthoringState;
  attempts: ControlledRepairAttemptStore;
}): void {
  const blocking = new Set(blockingFindingFingerprints(input.findings));
  const resolved = input.state.snapshot(input.projectId).pendingInteractions.filter((interaction) => {
    if (interaction.baseRevision && interaction.baseRevision !== input.revision) return false;
    if (!['controlled_repair', 'controlled_repair_expert'].includes(
      String(interaction.payload.purpose ?? ''),
    )) return false;
    const targets = interactionFindingFingerprints(interaction);
    return targets.length > 0 && targets.every((fingerprint) => !blocking.has(fingerprint));
  });
  for (const interaction of resolved) {
    const attemptId = String(interaction.payload.repairAttemptId ?? '').trim();
    if (attemptId) {
      try {
        const attempt = input.attempts.get(attemptId);
        input.attempts.complete(attemptId, input.revision, input.findings, [
          ...attempt.actions,
          { type: 'resolved_by_reconciliation', revision: input.revision },
        ]);
      } catch (error) {
        // 新增了其他阻断问题时 complete 会记录 regressed 后抛错；旧目标题卡仍须关闭，
        // 新问题会由当前 check 重新进入处理入口。
        if (!(error instanceof RepairQualityGateRejectedError)) {
          try { input.attempts.stop(attemptId, '当前复检已不再发现原问题'); } catch { /* 已结束 */ }
        }
      }
    }
    try {
      input.state.apply({
        kind: 'update_run',
        projectId: input.projectId,
        runId: interaction.runId,
        status: 'cancelled',
        result: {
          purpose: 'controlled_repair',
          resolvedByCheck: true,
          revision: input.revision,
        },
      });
    } catch { /* 已结束的运行无需重复关闭 */ }
  }
}

export function reconcilePackageRepairState(input: {
  projectId: string;
  revision: string;
  check: PackageCheckResult | null;
  state: AuthoringState;
  attempts?: ControlledRepairAttemptStore;
}): PackageRepairState {
  const attempts = input.attempts ?? new ControlledRepairAttemptStore();
  cancelStaleRepairInteractions(input.projectId, input.revision, input.state, attempts);
  const validationIncomplete = false;
  if (input.check?.revision === input.revision) {
    closeResolvedRepairInteractions({
      projectId: input.projectId,
      revision: input.revision,
      findings: input.check.findings,
      state: input.state,
      attempts,
    });
  }
  const snapshot = input.state.snapshot(input.projectId);
  const pending = blockingPendingInteractions(snapshot.pendingInteractions, input.revision);
  const interactionById = new Map(snapshot.pendingInteractions.map((item) => [item.id, item]));
  const queue = pending.map((item, index) => {
    const metadata = queueMetadata(interactionById.get(item.id)!);
    return {
      interactionId: item.id,
      title: item.title,
      status: index === 0 ? 'current' as const : 'queued' as const,
      order: metadata.order,
      purpose: item.purpose,
      location: item.location,
      ...(metadata.disposition ? { disposition: metadata.disposition } : {}),
      dependsOn: metadata.dependsOn,
    };
  }).sort((left, right) => left.order - right.order);
  queue.forEach((item, index) => { item.status = index === 0 ? 'current' : 'queued'; });

  const checkCurrent = input.check?.revision === input.revision ? input.check : null;
  const blocking = checkCurrent?.findings.filter((finding) => finding.severity === '错误') ?? [];
  const repairRelevant = blocking;
  const findings = repairRelevant.map((finding) => {
    const classified = classifyRepairFinding(finding);
    return {
      id: finding.id,
      fingerprint: repairFindingFingerprint(finding),
      category: finding.category,
      target: finding.target,
      message: finding.userMessage ?? finding.message,
      disposition: classified.disposition,
      authority: classified.authority,
      reason: classified.reason,
      ...(finding.code ? { code: finding.code } : {}),
      ...(finding.facts ? { facts: finding.facts } : {}),
      ...(finding.impact ? { impact: finding.impact } : {}),
    };
  });
  const latestAttempt = attempts.latestForProject(input.projectId);
  const attempt = latestAttempt && [
    latestAttempt.rootRevision,
    latestAttempt.inputRevision,
    latestAttempt.outputRevision,
  ].includes(input.revision)
    ? latestAttempt
    : null;
  const activeRepair = snapshot.activeRuns.some((run) => (
    ['queued', 'running'].includes(run.status)
      && ['controlled_repair', 'controlled_repair_apply'].includes(String(run.state.purpose ?? ''))
  ));
  const currentStepStatus: PackageRepairState['currentStep']['status'] = blocking.length === 0
    ? 'completed'
    : activeRepair ? 'running'
      : queue.some((item) => ['controlled_repair', 'controlled_repair_expert'].includes(item.purpose))
        ? 'waiting_user'
        : attempt?.executionStatus === 'failed' ? 'failed' : 'ready';
  const nextActions: JsonObject[] = blocking.length === 0
    ? [{ id: 'confirm_knowledge', label: '确认知识结构', style: 'primary' }]
    : queue.length > 0
      ? [{
        id: 'open_interaction', label: '处理当前待办', style: 'primary',
        interactionId: queue[0].interactionId,
      }]
      : [
        { id: 'repair_knowledge_structure', label: '逐项处理校验错误', style: 'primary' },
        { id: 'review_knowledge_files', label: '人工修改知识文件', style: 'secondary' },
        { id: 'defer_validation_errors', label: '暂缓并记录理由', style: 'secondary' },
      ];
  return {
    revision: input.revision,
    blockingCount: blocking.length,
    validationIncomplete,
    currentStep: { id: 'package_repair', status: currentStepStatus },
    attempt,
    findings,
    pendingQueue: queue,
    nextActions,
  };
}
