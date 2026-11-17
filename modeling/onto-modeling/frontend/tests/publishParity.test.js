// 发布检查、已发布版本、求解验证卡对齐 develop（ADR-0098）：文字与判定照 develop 的 PublishCheckDialog、
// PublishedVersionsDialog、lib/packagePublish.js、SolverModelPanel；mock 的建模阶段与 HB-05。全部为合成数据。
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  EVALUATION_HINT, assistantPrompt, degradeState, fileTarget, hardBlockers, iterationOptions, localReleaseRows,
  needsSolverAcknowledgement, proceedState, publishedText, releaseRows, releaseSource, stageNote, structurePrompt, structureRows,
  tableGroups, writebackText,
} from '../src/pages/publish/publishCheckModel.js';

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

test('渲染时才发现的结构问题（关系写法）只在门禁里，也挡发布、不能降级', () => {
  const render = { ...clean, gates: [...clean.gates, { id: 'package_structure', title: '知识包结构', blocking: true, degradable: false }] };
  assert.equal(hardBlockers(render), 1);
  assert.equal(proceedState(render).disabled, true);
  assert.equal(proceedState(render).label, '发布（需先修复）');
  assert.equal(degradeState(render).disabled, true);
});

test('结构问题按对象列（路径只给打开和助手），交给助手带文件与行号', () => {
  const findings = [
    { code: 'relation_structure_invalid', message: '关系端点「合成对象」不存在', path: 'ontology/object/订单.md', line: 12, target: { name: '订单' } },
    { code: 'package_render_failed', message: '导出失败' },
  ];
  assert.deepEqual(structureRows(findings).map((r) => [r.name, r.message, r.path, r.line]), [
    ['订单', '关系端点「合成对象」不存在', 'ontology/object/订单.md', 12],
    ['', '导出失败', '', null],
  ]);
  assert.equal(structurePrompt(findings), '发布前的知识包结构检查没通过，请逐条修正：\n'
    + '- ontology/object/订单.md 第 12 行：关系端点「合成对象」不存在\n- 导出失败\n修完运行规则检查，确认没有新的结构问题。');
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

test('已发布版本一张表：Hub 版本挂上本地记录（按 release_id，其次版本号），Hub 读不到时退回本地记录；本服务不写来源', () => {
  const hub = [{ release_id: 'r2', version: '0.1.2' }, { release_id: 'r1', version: '0.1.1' }, { release_id: 'rx', version: '0.1.0' }];
  const local = [{ version: '0.1.2', hub: { release_id: 'r2' } }, { version: '0.1.1' }];
  assert.deepEqual(releaseRows(hub, local).map((r) => r.local?.version || null), ['0.1.2', '0.1.1', null]);
  assert.deepEqual(localReleaseRows(local).map((r) => [r.release_id, r.version]), [['r2', '0.1.2'], ['', '0.1.1']]);
  assert.equal(releaseSource({ source_service: 'modeling' }), '');
  assert.equal(releaseSource({ source_service: 'evaluation' }), 'evaluation');
  assert.deepEqual(iterationOptions([{ commit: 'c2', version_label: 'v0.2', current: true }, { commit: 'c1', version_label: 'v0.1' }]),
    [{ value: 'c2', label: 'v0.2（当前）' }, { value: 'c1', label: 'v0.1' }]);
  assert.equal(EVALUATION_HINT, '按优化建议修改并发布新版本后，状态自动变为“已处理”。');
});

test('评测优化后发布的回写提示（develop usePackagePublish）', () => {
  const done = { version: '0.1.4', hub: { release_id: 'r4' }, evaluation_writeback: { status: 'done', release_id: 'r4', version: '0.1.4', base_version: '0.1.3' } };
  assert.deepEqual(writebackText(done), { tone: 'success', text: '已回写评测服务：新版本 0.1.4 处理了 0.1.3 的优化建议' });
  assert.deepEqual(writebackText({ evaluation_writeback: { status: 'pending' } }), { tone: 'warning', text: '新版本的处理结果暂未回写评测服务，打开已发布版本时会自动重试' });
  assert.equal(writebackText({ version: '0.1.4' }), null);
});
