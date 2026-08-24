import { getPackageArtifactStore } from '../src/artifacts/runtime.js';
import { assessHistoricalRepair } from '../src/ai-construction/repair-history-migration.js';
import { reconcilePackageRepairState } from '../src/authoring/package-repair-reconciliation.js';
import { getAuthoringState } from '../src/authoring/state.js';
import { getDb } from '../src/db/index.js';
import type { CheckFinding, PackageCheckResult } from '../src/workflow/package-check.js';
import { getCheck } from '../src/workflow/package-check.js';

function findings(value: unknown): CheckFinding[] | null {
  try {
    const parsed = JSON.parse(String(value ?? ''));
    return Array.isArray(parsed) ? parsed as CheckFinding[] : null;
  } catch {
    return null;
  }
}

const apply = process.argv.includes('--apply');
const db = getDb();
const packages = getPackageArtifactStore();
const state = getAuthoringState();
const attempts = db.prepare(`SELECT id, project_id, status, output_revision_oid,
    quality_outcome, findings_before_json, findings_after_json
  FROM package_repair_attempts
  WHERE quality_outcome = 'unknown_legacy'
  ORDER BY created_at`).all() as any[];
const updateAttempt = db.prepare(`UPDATE package_repair_attempts SET
    quality_outcome = ?, target_fingerprints_json = ?, resolved_fingerprints_json = ?,
    introduced_fingerprints_json = ?, before_blocking_count = ?, after_blocking_count = ?,
    quality_gate_json = ?
  WHERE id = ? AND quality_outcome = 'unknown_legacy'`);
const historical: Array<Record<string, unknown>> = [];

for (const row of attempts) {
  const before = findings(row.findings_before_json);
  let after = findings(row.findings_after_json);
  if ((!after || after.length === 0) && row.output_revision_oid) {
    const check = db.prepare(`SELECT findings_json FROM package_check_revisions
      WHERE project_id = ? AND artifact_revision_oid = ?`).get(
      row.project_id,
      row.output_revision_oid,
    ) as { findings_json: string } | undefined;
    if (check) after = findings(check.findings_json);
  }
  const assessment = assessHistoricalRepair(before, row.status === 'completed' ? after : null);
  historical.push({ id: row.id, projectId: row.project_id, ...assessment });
  if (apply) {
    updateAttempt.run(
      assessment.qualityOutcome,
      JSON.stringify(assessment.targetFingerprints),
      JSON.stringify(assessment.resolvedFingerprints),
      JSON.stringify(assessment.introducedFingerprints),
      assessment.beforeBlockingCount,
      assessment.afterBlockingCount,
      JSON.stringify({ source: 'historical_backfill', assessedAt: new Date().toISOString() }),
      row.id,
    );
  }
}

const projects = db.prepare('SELECT id, name FROM projects ORDER BY created_at').all() as Array<{
  id: string; name: string;
}>;
const currentErrors: Array<Record<string, unknown>> = [];
for (const project of projects) {
  const current = await packages.current(project.id).catch(() => null);
  if (!current) continue;
  const check = getCheck(project.id) as PackageCheckResult | null;
  const currentCheck = check?.revision === current.revision.oid ? check : null;
  const errorCount = currentCheck?.findings.filter((item) => item.severity === '错误').length ?? 0;
  const validationIncomplete = currentCheck?.findings.some((item) => (
    item.category === '语义校验执行失败'
  )) ?? false;
  if (!errorCount && !validationIncomplete) continue;
  const dryPending = state.snapshot(project.id).pendingInteractions.filter((interaction) => (
    (!interaction.baseRevision || interaction.baseRevision === current.revision.oid)
      && ['controlled_repair', 'controlled_repair_expert'].includes(
        String(interaction.payload.purpose ?? ''),
      )
  ));
  const repair = apply
    ? reconcilePackageRepairState({
      projectId: project.id,
      revision: current.revision.oid,
      check: currentCheck,
      state,
    })
    : {
      pendingQueue: dryPending,
      nextActions: validationIncomplete
        ? [{ id: 'run_package_check', label: '重新执行完整复检' }, { id: 'open_model_settings', label: '检查模型配置' }]
        : dryPending.length
        ? [{ id: 'open_interaction', label: '处理当前待办' }]
        : [{ id: 'repair_knowledge_structure', label: '逐项处理校验错误' }],
    };
  currentErrors.push({
    projectId: project.id,
    projectName: project.name,
    revision: current.revision.oid,
    errorCount,
    validationIncomplete,
    pendingCount: repair.pendingQueue.length,
    actionable: repair.pendingQueue.length > 0 || repair.nextActions.length > 0,
    needsRepairEntry: repair.pendingQueue.length === 0,
    nextActions: repair.nextActions,
  });
}

const outcomeCounts = Object.fromEntries([...new Set(historical.map((item) => (
  String(item.qualityOutcome)
)))].sort().map((outcome) => [
  outcome,
  historical.filter((item) => item.qualityOutcome === outcome).length,
]));
console.log(JSON.stringify({
  mode: apply ? 'apply' : 'dry-run',
  assessedAttempts: historical.length,
  outcomeCounts,
  currentErrorProjects: currentErrors.length,
  projectsWithoutAction: currentErrors.filter((item) => !item.actionable).length,
  historical,
  currentErrors,
}, null, 2));
