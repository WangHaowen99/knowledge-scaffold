// 发布检查、已发布版本、求解验证卡对齐 develop（ADR-0098）：文字与判定照 develop 的 PublishCheckDialog、
// PublishedVersionsDialog、lib/packagePublish.js、SolverModelPanel；mock 的建模阶段与 HB-05。全部为合成数据。
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  EVALUATION_HINT, RESOURCES_NOTE, assistantPrompt, checkSummary, checkTitle, degradeState, fileTarget, hardBlockers, lastPublishText,
  needsSolverAcknowledgement, proceedState, publishedText, stageNote, tableGroups, versionMeta, versionsTitle, writebackText,
} from '../src/pages/publish/publishCheckModel.js';
import { needsValidation, validationText } from '../src/pages/scripts/solverValidationModel.js';

const clean = {
  gates: [{ id: 'data_binding', blocking: false, degradable: true }],
  binding: { issues: [], objects: 3, bound_objects: 3 },
  solver: { enabled: false }, structure: { blocking: 0, findings: [] }, stages: { ready: true, missing_stages: [] },
};

test('建模阶段：缺哪几个就照 develop 的句子提示，发布按钮置灰', () => {
  assert.equal(stageNote(clean), '');
  const missing = { ...clean, stages: { ready: false, missing_stages: [{ key: 'operation', title: '操作提取' }, { key: 'validate', title: '质量检查' }] } };
  assert.equal(stageNote(missing), '建模未完成，请先完成：操作提取、质量检查');
  assert.deepEqual(proceedState(missing, false), { label: '发布', disabled: true, title: '建模未完成，请先完成：操作提取、质量检查' });
  assert.deepEqual(proceedState(clean, false), { label: '发布', disabled: false, title: '' });
});

test('发布按钮的三种说法与降级规则（develop 页脚）', () => {
  const blocking = { ...clean, binding: { issues: [{ blocking: true }], objects: 3, bound_objects: 2 } };
  assert.equal(proceedState(blocking).label, '发布（需先修复）');
  const solver = { ...clean, solver: { enabled: true, blocking: 0, requires_validation_after_publish: true } };
  assert.equal(needsSolverAcknowledgement(solver), true);
  assert.equal(proceedState(solver, false).label, '发布（需先确认）');
  assert.equal(proceedState(solver, true).disabled, false);
  assert.equal(degradeState(solver, false).disabled, true);
  // 结构自检挡发布，降级也不能绕过
  const structure = { ...blocking, structure: { blocking: 2 }, gates: [{ id: 'package_structure', title: '知识包结构', blocking: true, degradable: false }] };
  assert.equal(hardBlockers(structure), 2);
  assert.deepEqual(degradeState(structure, true), { disabled: true, title: '降级只放宽数据绑定，求解契约与知识包结构问题须先修正' });
});

test('检查卡的标题、摘要与上次发布', () => {
  assert.equal(checkTitle(clean), '发布知识包 · 数据绑定检查');
  assert.equal(checkTitle({ solver: { enabled: true } }), '发布知识包 · 发布检查');
  assert.equal(checkSummary({ binding: { objects: 5, bound_objects: 3, issues: [{ blocking: true }, { blocking: false }] } }), '对象 5 个，已绑定数据表 3 个；阻断项 1 个');
  const text = lastPublishText({ last_publish: { at: '2026-11-01T08:00:00Z', by: 'operator_a', degraded: true, unbound_count: 2 } });
  assert.match(text, /^上次发布：.+ · operator_a · 降级发布（2 个对象未绑定）$/);
  assert.equal(lastPublishText({ last_publish: null }), '');
});

test('改表下拉两组、打开文档的去处、交给助手的口径', () => {
  const groups = tableGroups({ candidates: [{ table: 'T_ORDER', matched: 2 }] }, ['T_ORDER', 'T_STORE']);
  assert.deepEqual(groups, [
    { label: '推荐', options: [{ value: 'T_ORDER', label: 'T_ORDER（2 列对得上）' }] },
    { label: '全部数据表', options: [{ value: 'T_STORE', label: 'T_STORE' }] },
  ]);
  assert.deepEqual(fileTarget('ontology/object/订单.md'), { page: 'studio', query: { entry: 'object:订单' } });
  assert.deepEqual(fileTarget('ontology/logic/创建订单.md'), { page: 'studio', query: { entry: 'operation:创建订单' } });
  assert.deepEqual(fileTarget('ontology/scripts/route_eval.py'), { page: 'scripts' });
  assert.equal(fileTarget('ontology/solver/plan_route/model.yaml'), null);
  const prompt = assistantPrompt([{ message: '「客户」声明的表 T_CLIENT 不存在' }]);
  assert.match(prompt, /^发布前数据绑定检查发现以下对象的「# 数据来源」与项目数据表对不上，请逐个修正：\n- 「客户」声明的表 T_CLIENT 不存在\n修正口径：/);
  assert.ok(!prompt.endsWith(' '));
});

test('发布成功的提示（develop usePackagePublish）', () => {
  assert.deepEqual(publishedText({ version: '0.1.3' }), { tone: 'success', text: '知识包已发布' });
  assert.deepEqual(publishedText({ version: '0.1.3' }, { degrade: true }), { tone: 'success', text: '已降级发布，未通过检查的对象不绑定数据表' });
  assert.deepEqual(publishedText({ version: '0.1.3' }, { acknowledged: true }), { tone: 'success', text: '知识包 0.1.3 已发布，求解能力待验证', evaluate: true });
  assert.deepEqual(publishedText({ version: '0.1.3', local_record_pending: true }), { tone: 'warning', text: '知识包已发布，本地发布记录待恢复' });
});

test('已发布版本：标题、版本下面一行、提示与末行文字', () => {
  assert.equal(versionsTitle({ releases: [{ name: '门店补货' }] }, '别名'), '已发布版本 — 门店补货');
  assert.equal(versionsTitle({ releases: [] }, '门店补货'), '已发布版本 — 门店补货');
  assert.equal(versionsTitle({ releases: [] }, ''), '已发布版本');
  assert.match(versionMeta({ created_at: '2026-11-01T08:00:00Z', source_service: 'modeling' }), / · modeling$/);
  assert.equal(EVALUATION_HINT, '打开优化建议执行评测优化，修改并发布新版本后状态自动变为“已处理”。');
  assert.equal(RESOURCES_NOTE, 'Hub 仅保存明确共享的最新资料集，不与历史包版本绑定；本地资料不会自动上传。');
});

test('求解验证卡：只在启用了求解操作时出现，五种状态文字照 develop', () => {
  assert.equal(needsValidation({ enabled: false }), false);
  assert.equal(needsValidation({ enabled: true, operations: [{ enabled: false }] }), false);
  assert.equal(needsValidation({ enabled: true, operations: [{ enabled: true }] }), true);
  const unpublished = { state: 'unpublished' };
  assert.equal(validationText({ status: null, dirty: false, current: unpublished }), '正在读取求解验证状态。');
  assert.equal(validationText({ status: {}, dirty: true, current: unpublished }), '有未保存的修改：保存并发布新版本后，再对新版本发起求解验证。');
  assert.equal(validationText({ status: { configured: false }, dirty: false, current: unpublished }), '评测服务不可用或未配置，暂时读不到求解验证状态。');
  assert.equal(validationText({ status: {}, dirty: false, current: { state: 'passed', version: '0.1.2' } }), '当前修订已在 0.1.2 通过求解验证。');
  assert.equal(validationText({ status: {}, dirty: false, current: { state: 'published' } }), '当前修订已发布，尚未通过求解验证：在评测页面对该版本发起求解验证。');
  assert.equal(validationText({ status: {}, dirty: false, current: unpublished }), '当前修改尚未发布和验证：发布新版本后，在「已发布版本（知识包评测）」中发起求解验证。');
});

test('评测优化后发布的回写提示（develop usePackagePublish）', () => {
  const done = { version: '0.1.4', hub: { release_id: 'r4' }, evaluation_writeback: { status: 'done', release_id: 'r4', version: '0.1.4', base_version: '0.1.3' } };
  assert.deepEqual(writebackText(done), { tone: 'success', text: '已回写评测服务：新版本 0.1.4 处理了 0.1.3 的优化建议' });
  assert.deepEqual(writebackText({ evaluation_writeback: { status: 'pending' } }), { tone: 'warning', text: '新版本的处理结果暂未回写评测服务，打开已发布版本时会自动重试' });
  assert.equal(writebackText({ version: '0.1.4' }), null);
});
