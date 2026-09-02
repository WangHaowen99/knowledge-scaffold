import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-acceptance-'));
process.env.MODELING_ACCEPTANCE_MODE = 'true';

const {
  acceptanceAgentToolAfter,
  acceptanceAgentToolBefore,
  armAcceptanceFault,
  closeAllAcceptanceFaults,
  closeAcceptanceFault,
  listAcceptanceFaults,
  takeAcceptanceFault,
} = await import('../src/acceptance/faults.js');
const { ACCEPTANCE_SCENARIOS, createAcceptanceScenario } = await import('../src/acceptance/scenarios.js');
const { acceptanceProjectScenario } = await import('../src/acceptance/projects.js');
const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');
const { listDocuments } = await import('../src/documents/repo.js');
const { getProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
const { listTestcases } = await import('../src/workflow/m3-repo.js');
const { pendingAgentProjectEvents } = await import('../src/agent/project-event-outbox.js');

test('十个浏览器验收场景都能建立独立的正式知识包基线', async () => {
  const created = [];
  for (const [scenarioId] of ACCEPTANCE_SCENARIOS) {
    const result = await createAcceptanceScenario('browser_acceptance_user', scenarioId);
    created.push(result);
    const current = await getPackageArtifactStore().current(result.project.id);
    assert.ok(current, `${scenarioId} 缺少正式工件`);
    assert.match(current!.spec.name, /浏览器验收/u);
    assert.match(result.project.background, /不包含真实业务数据/u);
    assert.equal(acceptanceProjectScenario(result.project.id, 'browser_acceptance_user'), scenarioId);
    assert.equal(acceptanceProjectScenario(result.project.id, 'another_synthetic_user'), '');
    const importEvent = pendingAgentProjectEvents(result.project.id).find((event) => 'baselineRevision' in event);
    assert.ok(importEvent, `${scenarioId} 缺少 Agent 基线接续事件`);
  }
  assert.equal(new Set(created.map((item) => item.project.id)).size, ACCEPTANCE_SCENARIOS.length);

  const generation = created.find((item) => item.scenarioId === 'SCN-GENERATION')!;
  assert.equal(listDocuments(generation.project.id).length, 3);
  const long = created.find((item) => item.scenarioId === 'SCN-LONG')!;
  assert.equal(listDocuments(long.project.id).length, 4);
  assert.ok(listDocuments(long.project.id).every((item) => (item.textContent?.length ?? 0) > 5_000));
  const evaluation = created.find((item) => item.scenarioId === 'SCN-EVALUATION')!;
  assert.equal(listTestcases(evaluation.project.id).filter((item) => item.source === 'manual').length, 3);
  assert.equal(getProjectDataSource(evaluation.project.id)?.mode, 'none');
});

test('SCN-BINDING 提供九张 Schema Sheet 和一个待移除派生对象', async () => {
  const result = await createAcceptanceScenario('binding_contract_user', 'SCN-BINDING');
  const current = await getPackageArtifactStore().readPackageFiles(result.project.id);
  const object = current.spec.objects.find((item) => item.key === 'legacy_derived_summary');

  assert.ok(object);
  assert.equal(object.sourceTable, undefined);
  assert.deepEqual(object.sourceRefs ?? [], []);
  assert.ok(object.fields.every((field) => field.sourceColumn === undefined));
  assert.equal(current.files['mappings/object-data-mappings.json'], undefined);

  const documents = listDocuments(result.project.id);
  assert.equal(documents.length, 1);
  assert.equal(documents[0]?.filename, 'synthetic_schema_workbook.xlsx');
  assert.equal(documents[0]?.purpose, 'schema');
  assert.equal(documents[0]?.tables.length, 9);
  assert.equal(documents[0]?.tables[8]?.rowCount, 0);
});

test('故障一次启用只消费一次，同运行粘性不会增加浏览器消费次数', () => {
  armAcceptanceFault('fault_owner', 'F09');
  const first = takeAcceptanceFault('fault_owner', 'F09', 'run-one', { stickyRun: true });
  const second = takeAcceptanceFault('fault_owner', 'F09', 'run-one', { stickyRun: true });
  const otherRun = takeAcceptanceFault('fault_owner', 'F09', 'run-two', { stickyRun: true });
  assert.equal(first?.firstEffect, true);
  assert.equal(second?.firstEffect, false);
  assert.equal(second?.consumedCount, 1);
  assert.equal(second?.effectCount, 2);
  assert.equal(otherRun, null);
  const visible = listAcceptanceFaults('fault_owner').find((item) => item.id === 'F09');
  assert.equal(visible?.status, 'consumed');
  assert.equal(visible?.consumedCount, 1);
  assert.equal(closeAcceptanceFault('fault_owner', 'F09').status, 'closed');
});

test('关闭全部故障会隔离前一条浏览器用例留下的未消费故障', () => {
  armAcceptanceFault('isolated_owner', 'F16');
  armAcceptanceFault('isolated_owner', 'F18');
  const closed = closeAllAcceptanceFaults('isolated_owner');
  assert.ok(closed.every((item) => item.status !== 'armed'));
  assert.equal(takeAcceptanceFault('isolated_owner', 'F16', 'later-run'), null);
  assert.equal(takeAcceptanceFault('isolated_owner', 'F18', 'later-file'), null);
});

test('Change Set 故障分别覆盖提交前零写入和提交后回执恢复边界', () => {
  armAcceptanceFault('change_owner', 'F03');
  const before = acceptanceAgentToolBefore('change_owner', 'run-change', 'stage_change_set');
  assert.equal(before?.statusCode, 503);
  assert.equal(before?.payload.code, 'CHANGE_SET_TEMPORARILY_UNAVAILABLE');
  assert.equal(acceptanceAgentToolBefore('change_owner', 'run-change', 'stage_change_set'), null);

  armAcceptanceFault('change_owner', 'F04');
  const after = acceptanceAgentToolAfter('change_owner', 'run-change', 'stage_change_set');
  assert.equal(after?.statusCode, 503);
  assert.equal(after?.payload.code, 'CHANGE_SET_RESULT_UNKNOWN');
  assert.equal(after?.payload.committed, true);
  assert.equal(acceptanceAgentToolAfter('change_owner', 'run-change', 'stage_change_set'), null);
});

test('相同校验指纹重试一次后确定性停止，脚本故障返回逐例失败', () => {
  armAcceptanceFault('validation_owner', 'F09');
  const first = acceptanceAgentToolBefore('validation_owner', 'run-validation', 'validate_change_set');
  const second = acceptanceAgentToolBefore('validation_owner', 'run-validation', 'validate_change_set');
  assert.equal(first?.payload.retryable, true);
  assert.equal(second?.payload.code, 'VALIDATION_RETRY_EXHAUSTED');
  assert.equal(second?.payload.retryable, false);

  armAcceptanceFault('validation_owner', 'F12');
  const script = acceptanceAgentToolBefore('validation_owner', 'run-script', 'run_script_test');
  assert.equal(script?.statusCode, 200);
  assert.equal(script?.payload.failed, 2);
  assert.equal(Array.isArray(script?.payload.results), true);
});

test('Schema 对象同步结构化转向只注入一次并携带稳定恢复动作', () => {
  armAcceptanceFault('binding_owner', 'F19');
  const first = acceptanceAgentToolBefore('binding_owner', 'run-binding', 'sync_schema_objects');
  assert.equal(first?.payload.requiredAction, 'redirect_tool');
  assert.equal(first?.payload.requiredTool, 'sync_schema_objects');
  assert.equal((first?.payload.recoveryAction as any)?.id, 'resume_schema_object_sync');
  assert.equal(acceptanceAgentToolBefore(
    'binding_owner', 'run-binding', 'sync_schema_objects',
  ), null);
});
