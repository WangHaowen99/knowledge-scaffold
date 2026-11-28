// 代码生成与求解页：页面纯逻辑（scriptsModel）与 mock（routes-scripts：SC-01～SC-03、SV-01～SV-03、页面命令）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { SOLVER_TYPE as MOCK_SOLVER_TYPE, lockScope, register, taskSeeds, validateOperations } from '../src/api/mock/routes-scripts.js';
import {
  diffContracts, draftKey, setEnabled, setField, setOptionLabel, setTemplateParam, toManual, toPayload,
} from '../src/lib/solver-contract-model.js';
import {
  LEGACY_CASES_FILE, SOLVER_TYPE, availableScriptsTab, legacyCasesText, candidateView, checkLines, contractChip, contractLocked, contractProblems, contractTitle, defaultPicked, defaultScript,
  descriptorChip, draftOf, evaluatorCommand, evaluatorOptions, evaluatorTarget, familyOptions, fmtSecs, fmtStamp, generateCommand,
  lockText, modelOperation, moreItems, pickedOperations, removeUnit, revisionTag, rowAction, sandboxChip, sandboxView, savedToast,
  saveErrorView, saveState, scanCommand, scanLock, setUnit, templateCommand, toggleCapability, togglePicked, verificationView,
} from '../src/pages/scripts/scriptsModel.js';

const at = (m, d, h, mi) => new Date(2026, m - 1, d, h, mi).toISOString();
const P = `/api/v1/projects/${F.MAIN_PID}`;

function server({ sc = {}, lock = null } = {}) {
  const routes = [];
  const events = [];
  const hooks = [];
  const cards = F.projectCards();
  const scope = { lock };
  const db = {};
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db,
    sc,
    F,
    cardOf: (id) => cards.find((c) => c.id === id),
    convsOf: () => [{ id: 'c1' }],
    scopeOf: () => scope.lock || { state: 'none', entries: [], artifacts: [], paths: [], slice: null },
    setScope: (pid, l) => { scope.lock = l; events.push({ type: 'scope.updated', data: l }); },
    emitProj: (pid, type, data) => events.push({ type, data }),
    commandHooks: hooks,
  };
  register(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    const r = routes.find((x) => x.method === method && x.re.test(u.pathname));
    assert.ok(r, `${method} ${path} 没有路由`);
    const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {} });
    return { status: res.status, body: await res.json() };
  };
  // 模拟 index.js 的 S-20：找到命中的钩子，取标题，跑完调 finish
  const run = (body) => {
    const hook = hooks.find((h) => h.match(body));
    assert.ok(hook, `没有命中的命令钩子：${body.command}`);
    const title = hook.title(body);
    return { title, steps: hook.steps, finish: () => hook.finish(F.MAIN_PID, body, { state: 'done' }) };
  };
  return { call, run, events, scope, cards, db };
}

const fromPage = (cmd) => ({ ...cmd, origin: { page: 'scripts', label: '代码生成' } });

test('本地及旧验证记录不能冒充沙箱执行或原始输出', () => {
  for (const runtime of ['local', 'jiuwenbox', undefined]) {
    const v = verificationView({ status: 'passed', runtime, network_access: runtime === 'jiuwenbox' ? false : null,
      cases: [{ name: 'normal', expected: 'success', passed: true }] }, 'synthetic.py');
    assert.equal(v.chip.tone, runtime === 'jiuwenbox' ? 'teal' : 'amber');
    assert.equal(v.lines[0].text, '用例：normal');
    assert.equal(v.lines[1].text, '预期：success（记录未保存原始输出）');
    assert.equal(v.summary.text.endsWith('网络隔离未证实'), runtime !== 'jiuwenbox');
  }
  assert.equal(sandboxChip({ sandbox: { status: 'passed', runtime: 'local' } }).text, '本地通过');
});

test('确定性页签直接链接等待插件状态后再决定是否回退', () => {
  assert.equal(availableScriptsTab('dt', null), 'dt');
  assert.equal(availableScriptsTab('dt', true), 'dt');
  assert.equal(availableScriptsTab('dt', false), 'sc');
  assert.equal(availableScriptsTab('sv', false), 'sv');
});

// ---------- 页面纯逻辑：脚本 ----------

test('时间与耗时的写法', () => {
  assert.equal(fmtStamp(at(10, 31, 15, 18)), '10-31 15:18');
  assert.equal(fmtStamp(null), '');
  assert.equal(fmtSecs(1200), '1.2s');
  assert.equal(fmtSecs(800), '0.8s');
  assert.equal(fmtSecs(12400), '12s');
  assert.equal(fmtSecs(null), '');
});

test('脚本表三列状态与行末按钮', async () => {
  const { call } = server();
  const { body } = await call('GET', `${P}/scripts`);
  const [a, b, c] = body.items;
  assert.deepEqual([a, b, c].map((r) => contractChip(r).text), ['通过', '通过', '未通过']);
  assert.equal(contractChip(c).pop, '头部注释声明的参数和 argparse 对不上：缺少 delivery_cycle');
  assert.deepEqual([a, b, c].map((r) => descriptorChip(r).text), ['已同步', '需更新', '—']);
  assert.deepEqual([a, b, c].map((r) => sandboxChip(r).text), ['环境未记录通过 · 1.2s', '环境未记录通过 · 0.8s', '未运行']);
  assert.equal(rowAction(a).kind, 'more');
  assert.deepEqual(moreItems(a).map((x) => x.label), ['重新生成脚本', '去 Studio 编辑']);
  assert.deepEqual(rowAction(b), {
    kind: 'descriptor', label: '重新生成', command: '/ontology-codegen',
    args: { script: 'build_replenish_order', mode: 'descriptor' }, text: '重新生成 build_replenish_order.py 的描述符',
  });
  assert.equal(rowAction(c).text, '修复 overage_compare.py 的契约问题');
  // 默认预览要处理的那一个（描述符过期），它是 Studio 里的脚本条目
  assert.equal(defaultScript(body.items), 'build_replenish_order');
  assert.equal(b.script.id, 'script:build_replenish_order');
  assert.equal(defaultScript([]), null);
});

test('可代码化候选：标题、默认勾选、不建议代码化的说明、生成命令', async () => {
  const { call } = server();
  const { body } = await call('GET', `${P}/scripts`);
  const cv = candidateView(body.candidates);
  assert.equal(cv.title, '可代码化的操作 3 个');
  assert.equal(cv.sub, '10-31 15:18 扫描');
  assert.equal(cv.unsuitableText, '不建议代码化：受理三道校验（需要语义判断）');
  const picked = defaultPicked(body.candidates);
  assert.deepEqual(picked, ['safety_stock_calc', 'display_qty_check']);
  assert.deepEqual(togglePicked(picked, 'safety_stock_calc'), ['display_qty_check']);
  assert.deepEqual(togglePicked(picked, 'overage_and_hint'), [...picked, 'overage_and_hint']);
  const ops = pickedOperations(body.candidates, picked);
  assert.deepEqual(generateCommand(ops), {
    command: '/ontology-codegen', args: { operations: ['safety_stock_calc', 'display_qty_check'] }, text: '生成 2 个脚本并在沙箱里跑通',
  });
  assert.equal(candidateView({ scanned_at: null, items: [], unsuitable: [] }).scanned, false);
  assert.deepEqual(scanCommand(), { command: '/ontology-codegen-scan', text: '扫描可代码化的操作' });
  // 这一页的代码生成任务在跑时扫描按钮置灰并说明原因，没在跑时可点
  assert.equal(scanLock({ task_id: 't-1', state: 'running' }), '代码生成任务正在运行，请等待本轮完成');
  assert.equal(scanLock(null), null);
});

test('沙箱：状态 chip 与验证输出', async () => {
  // 可用时只有状态 chip，不再挂说明；不可用时一句话让用户找管理员
  assert.deepEqual(sandboxView({ available: true, network: 'denied' }), { text: '脚本验证沙箱可用', tone: 'teal', icon: 'check', note: null });
  assert.equal(sandboxView({ available: false, network: 'denied' }).tone, 'red');
  assert.equal(sandboxView({ available: false }).note, '暂时不能验证和保存脚本，请联系管理员');
  assert.equal(sandboxView(null), null);
  const { call } = server();
  const v = (await call('GET', `${P}/scripts/build_replenish_order/verification`)).body;
  assert.equal(v.network_access, false);
  const vv = verificationView(v, 'build_replenish_order.py');
  assert.equal(vv.chip.text, '环境未记录通过');
  assert.equal(vv.sub, '10-31 15:19');
  assert.equal(vv.lines[0].text, '$ build_replenish_order.py --store_code A001 --request_qty 96');
  assert.equal(vv.summary.text, '✓ 2/2 用例通过 · 0.8s · 环境未记录执行 · 网络隔离未证实');
  const nr = (await call('GET', `${P}/scripts/overage_compare/verification`)).body;
  assert.equal(verificationView(nr, 'overage_compare.py').status, 'not_run');
  assert.equal(verificationView(nr, 'overage_compare.py').sub, null);
  const failed = verificationView({ status: 'failed', duration_ms: 900, network_access: false, cases: [{ args: '-x', output: 'boom', passed: false }] }, 'a.py');
  assert.equal(failed.summary.text, '✗ 0/1 用例通过 · 0.9s · 环境未记录执行 · 网络隔离未证实');
  assert.equal(failed.lines[1].kind, 'err');
  assert.equal((await call('GET', `${P}/scripts/nope/verification`)).status, 404);
});

test('SC-03 与 sandbox:off；scripts:empty 时什么都没有', async () => {
  const off = server({ sc: { sandbox: 'off' } });
  assert.equal((await off.call('GET', '/api/v1/sandbox/status')).body.available, false);
  assert.equal((await off.call('GET', `${P}/scripts`)).body.sandbox.available, false);
  const empty = server({ sc: { scripts: 'empty' } });
  const r = (await empty.call('GET', `${P}/scripts`)).body;
  assert.equal(r.items.length, 0);
  assert.equal(r.candidates.scanned_at, null);
  const v = (await empty.call('GET', `${P}/solver/contract`)).body;
  assert.equal(v.operations.length, 0);
  assert.equal(v.enabled, false);
});

test('脚本原文放进 db.files（Studio 的 F-02 从这里读 Python 原文）', () => {
  const { db } = server();
  assert.match(db.files.get('scripts/build_replenish_order.py').content, /^"""生成补货单/);
  assert.ok(db.files.has('scripts/overage_compare.py'));
});

// ---------- 求解契约 ----------

test('SV-01：合成场景的求解契约，参数都从响应里读', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  assert.equal(view.revision_no, 7);
  assert.deepEqual(view.operations.map((o) => [o.solver_key, o.operation.name, o.enabled, o.mode]), [
    ['restock_route', '补货编制主流程', true, 'template'],
    ['expiry_disposal', '临期损耗编制', false, 'template'],
  ]);
  const [main] = view.operations;
  assert.deepEqual(main.option_labels, ['门店直送', '仓库中转']);
  assert.deepEqual(main.template_params.display_mapping, { OPTION_0: '门店直送', OPTION_1: '仓库中转' });
  assert.equal(main.evaluator.path, 'ontology/scripts/restock_route_evaluator.py');
  assert.deepEqual(main.field_units.map((u) => u.field), ['batch_qty', 'days', 'daily_sales', 'shelf_capacity']);
  assert.equal(view.requires_validation_after_publish, true);
  assert.ok(!('receipt' in view) && !('gate' in view));
  assert.equal(view.lock, null);
  // 模板参数只有这几项，表单的参数名、标题、选项都从响应里读
  view.operations.forEach((o) => assert.deepEqual(Object.keys(o.template_params).sort(), ['display_mapping', 'evaluator_path', 'process_name', 'stream_name', 'title']));
});

test('SV-01 → 表单草稿 → 原样回传不算改动；契约字段缺 binding 时也能拼出草稿', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  const items = draftOf(view);
  assert.equal(items.length, 2);
  assert.equal(items[0].managed, true);
  assert.equal(items[0].evaluator, 'ontology/scripts/restock_route_evaluator.py');
  const base = view.operations.map(modelOperation);
  assert.equal(diffContracts(base, items).dirty, false);
  const changed = [setTemplateParam(items[0], 'title', '补货调拨计划'), items[1]];
  assert.deepEqual(diffContracts(base, changed).changed.map((c) => c.files), [['presentation.yaml']]);
  // 7.12 只列了表单字段：没有 binding / template_params 时按字段拼
  const bare = { ...view.operations[0] };
  delete bare.binding;
  delete bare.template_params;
  const m = modelOperation(bare);
  assert.equal(m.binding.operation, '补货编制主流程');
  assert.equal(m.binding.declaration.engine_requirements.problem_family, 'candidate_selection.v1');
  assert.deepEqual(m.template_params.display_mapping, { OPTION_0: '门店直送', OPTION_1: '仓库中转' });
  assert.equal(m.template_params.title, '补货调拨方案');
});

test('卡片标题、问题族与评估器下拉', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  const [main] = draftOf(view);
  assert.equal(contractTitle(main, view.available_operations), '补货编制主流程');
  assert.equal(contractTitle({ binding: { solver_key: 'x1' } }, []), 'x1');
  assert.equal(contractTitle({ binding: {} }, []), '新的求解契约');
  assert.deepEqual(familyOptions(view.problem_families, 'candidate_selection.v1'), [{ value: 'candidate_selection.v1', label: 'candidate_selection.v1 · 批次二选一规划' }]);
  assert.equal(familyOptions([{ id: 'a.v1', label: 'a.v1', capabilities: [] }], 'old')[1].label, 'old（不在注册目录里）');
  const ev = evaluatorOptions(view, main);
  assert.equal(ev[0].label, '自动查找 → ontology/scripts/restock_route_evaluator.py');
  assert.equal(evaluatorOptions(view, { evaluator: '', params: {} })[0].label, '自动查找');
  assert.equal(evaluatorOptions(view, { evaluator: '', params: { evaluator_path: 'x.py' } }).at(-1).label, 'x.py（不存在或不是评估器）');
});

test('写入范围只读：锁住的求解契约、锁定条两态文字', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  const [main, other] = draftOf(view);
  const lock = { state: 'assistant_running', entries: [], artifacts: lockScope.artifacts, paths: lockScope.paths };
  assert.equal(SOLVER_TYPE, MOCK_SOLVER_TYPE);
  assert.equal(contractLocked(lock, main), true);
  assert.equal(contractLocked(lock, other), false);
  assert.equal(contractLocked({ state: 'none', entries: [], artifacts: lockScope.artifacts, paths: [] }, main), false);
  // 只按路径锁也算
  assert.equal(contractLocked({ state: 'pending_confirm', entries: [], artifacts: [], paths: ['ontology/solver/**'] }, other), true);
  const t = lockText(lock, '补货编制主流程');
  // 只读条只有标题（旁边有「回到对话」「去确认」「查看运行」），不再挂范围说明
  assert.deepEqual(t.running, ['助手正在为「补货编制主流程」生成评估器，暂时只读']);
  assert.deepEqual(t.pending, ['评估器已生成，待确认']);
  const auto = lockText({ state: 'autobuild_slice' }, 'X');
  assert.deepEqual(auto.running, ['自主构建正在改「X」，暂时只读']);
  assert.equal(auto.pending, undefined);
});

test('检查卡：已保存、草稿、检查中、回执几种情况', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  const items = draftOf(view);
  const key = draftKey(items);
  const live = { key, rejected: [], findings: [], evaluator: { kind: 'declarative', constraints: 3 } };
  const texts = (o) => checkLines({ view, item: items[0], draftKey: key, live, ...o }).map((l) => l.text);
  // 验证回执退役（ADR-0098）：检查卡只讲契约问题与评估器，验证在发布后的独立评测里做
  assert.deepEqual(texts({ dirty: false }), ['r7 未发现契约问题', '评估器已解析：声明式 · 3 条约束']);
  assert.deepEqual(texts({ dirty: true }), ['草稿未发现契约问题', '评估器已解析：声明式 · 3 条约束']);
  assert.equal(texts({ dirty: true, live: { ...live, key: 'old' } })[0], '正在检查草稿…');
  assert.equal(texts({ dirty: true, live: { key, failed: true } })[0], '暂时检查不了草稿');
  assert.equal(texts({ dirty: true, live: { ...live, rejected: [{ message: '被拒了' }] } })[0], '被拒了');
  // 新建的那份还没有评估器：不借用别的契约的解析结果
  assert.equal(checkLines({ view, item: { ...items[0], evaluator: '' }, dirty: true, live, draftKey: key })[1].text, '还没有评估器');
  // 本地预检的错误排在最前；不是当前这份的带上名字
  const bad = [items[0], setOptionLabel(items[1], 'OPTION_1', '打折促销')];
  const problems = contractProblems(bad, view, bad[0]);
  assert.deepEqual(problems.map((p) => p.message), ['「临期损耗编制」：两个选项的显示名称相同，结果无法区分']);
  assert.equal(checkLines({ view, item: bad[0], dirty: true, live, draftKey: draftKey(bad), problems })[0].tone, 'err');
});

test('修订标签、保存按钮、保存后发评估器命令', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  assert.equal(revisionTag(view, false), 'r7');
  assert.equal(revisionTag(view, true), 'r8 草稿');
  assert.equal(revisionTag({ revision_no: 0 }, false), '');
  const items = draftOf(view);
  const key = draftKey(items);
  assert.deepEqual(saveState({ dirty: false, problems: [] }), { disabled: true, title: '没有要保存的修改' });
  assert.equal(saveState({ dirty: true, locked: true }).title, '这份求解契约在助手写入范围里，这一轮结束后才能保存');
  assert.equal(saveState({ dirty: true, problems: [{ severity: 'error' }, { severity: 'warning' }] }).title, '还有 1 个问题要先改');
  assert.equal(saveState({ dirty: true, live: { key, rejected: [{ message: 'm' }] }, draftKey: key }).title, '保存会被拒绝：m');
  assert.deepEqual(saveState({ dirty: true, problems: [{ severity: 'warning' }] }), { disabled: false, title: '' });
  // 改了停用的那份不发；改了启用的那份发它
  const base = view.operations.map(modelOperation);
  const onlyOther = [items[0], setField(items[1], 'units', [])];
  assert.equal(evaluatorTarget(diffContracts(base, onlyOther), onlyOther, items[1]), null);
  const both = [setTemplateParam(items[0], 'title', 'T'), setEnabled(items[1], true)];
  assert.equal(evaluatorTarget(diffContracts(base, both), both, both[1]).uid, both[1].uid);
  assert.deepEqual(evaluatorCommand('restock_route', '补货编制主流程'), {
    command: '/ontology-solver-evaluator', args: { solver_key: 'restock_route' }, text: '为「补货编制主流程」生成评估器',
  });
  // 「手动修改会被覆盖」放在点按钮后的确认框里，命令本身只说做什么
  assert.deepEqual(templateCommand('restock_route', '补货编制主流程'), {
    command: '/ontology-solver-evaluator', args: { solver_key: 'restock_route', mode: 'template' }, text: '按模板重新生成「补货编制主流程」的附属文件和评估器',
  });
  assert.equal(savedToast({ revision_no: 8 }), '已保存 r8');
});

test('保存失败的说法：冲突、被拒、被锁', () => {
  const now = Date.parse(at(11, 1, 10, 0));
  const c = saveErrorView({ code: 'revision_conflict', details: { changed_by: 'assistant', changed_at: at(11, 1, 9, 58), version_label: 'v0.13' } }, now);
  assert.deepEqual(c, { kind: 'conflict', text: '求解契约已被助手修改（2 分钟前，版本 v0.13），这次没有保存。刷新看最新内容后再改。' });
  const r = saveErrorView({ code: 'solver_contract_rejected', details: { findings: [{ message: 'x' }] } });
  assert.equal(r.kind, 'rejected');
  assert.equal(r.findings.length, 1);
  assert.equal(saveErrorView({ code: 'write_scope_locked' }).kind, 'locked');
  assert.equal(saveErrorView({ code: 'internal_error', message: '出错了' }).text, '出错了');
});

test('字段单位与引擎能力的小改动', () => {
  const units = [{ field: 'a', unit: '箱', note: '' }];
  assert.deepEqual(setUnit(units, 0, 'unit', '天'), [{ field: 'a', unit: '天', note: '' }]);
  assert.deepEqual(setUnit(units, 1, 'field', 'b'), [...units, { field: 'b', unit: '', note: '' }]);
  assert.deepEqual(removeUnit(units, 0), []);
  assert.deepEqual(toggleCapability(['x'], 'x'), []);
  assert.deepEqual(toggleCapability(['x'], 'y'), ['x', 'y']);
});

// ---------- mock：保存、检查、回执 ----------

test('SV-03：模板参数生成五份附属文件，评估器已解析；结构问题进 rejected', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  const items = draftOf(view);
  const r = (await call('POST', `${P}/solver/contract/check`, { contract: { operations: toPayload(items) } })).body;
  assert.deepEqual(r.rejected, []);
  assert.deepEqual(Object.keys(r.rendered[0]).sort(), ['entry', 'fact_snapshot_schema', 'input_schema', 'presentation', 'result_schema']);
  assert.match(r.rendered[0].entry, /OPTION_0: 门店直送/);
  assert.deepEqual(r.evaluator, { kind: 'declarative', constraints: 3 });
  const bad = (await call('POST', `${P}/solver/contract/check`, { contract: { operations: toPayload([setField(items[0], 'solver_key', 'Bad')]) } })).body;
  assert.equal(bad.rejected[0].field, 'solver_key');
  assert.equal((await call('POST', `${P}/solver/contract/check`, { contract: {} })).status, 400);
});

test('SV-02：保存修订号加 1、旧回执失效；手动编辑按原文写入', async () => {
  const { call, events, cards } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  const items = draftOf(view);
  const next = [setTemplateParam(items[0], 'title', '补货调拨计划'), items[1]];
  const saved = await call('PUT', `${P}/solver/contract`, { base_revision: view.revision, contract: { operations: toPayload(next) }, summary: '改标题' });
  assert.equal(saved.status, 200);
  assert.equal(saved.body.revision_no, 8);
  assert.notEqual(saved.body.revision, view.revision);
  assert.equal(saved.body.version_label, 'v0.13');
  assert.equal(cards.find((c) => c.id === F.MAIN_PID).version_label, 'v0.13');
  assert.equal(saved.body.requires_validation_after_publish, true);
  assert.equal(saved.body.operations[0].result_title, '补货调拨计划');
  assert.ok(events.some((e) => e.type === 'artifacts.updated' && e.data.type === SOLVER_TYPE));
  // 转为手动编辑后保存：mode=manual，附属文件按原文
  const again = draftOf(saved.body);
  const manual = toManual(again[0], { ...again[0].sidecars, presentation: 'title: 手写标题\n' });
  const r2 = await call('PUT', `${P}/solver/contract`, { base_revision: saved.body.revision, contract: { operations: toPayload([manual, again[1]]) } });
  assert.equal(r2.status, 200);
  assert.equal(r2.body.operations[0].mode, 'manual');
  assert.equal(r2.body.operations[0].sidecars.presentation, 'title: 手写标题\n');
  assert.deepEqual(r2.body.operations[0].managed, []);
});

test('SV-02：旧修订 409、sv:conflict 第一次保存冲突、结构被拒 422、写入范围 423', async () => {
  const { call } = server();
  const view = (await call('GET', `${P}/solver/contract`)).body;
  const items = draftOf(view);
  const stale = await call('PUT', `${P}/solver/contract`, { base_revision: 'sha256:old', contract: { operations: toPayload(items) } });
  assert.equal(stale.status, 409);
  assert.equal(stale.body.details.kind, 'solver_contract');

  const cf = server({ sc: { sv: 'conflict' } });
  const v2 = (await cf.call('GET', `${P}/solver/contract`)).body;
  const first = await cf.call('PUT', `${P}/solver/contract`, { base_revision: v2.revision, contract: { operations: toPayload(draftOf(v2)) } });
  assert.equal(first.status, 409);
  assert.equal(first.body.details.changed_by, 'assistant');
  const fresh = (await cf.call('GET', `${P}/solver/contract`)).body;
  assert.equal(fresh.revision_no, 8);
  assert.equal((await cf.call('PUT', `${P}/solver/contract`, { base_revision: fresh.revision, contract: { operations: toPayload(draftOf(fresh)) } })).status, 200);

  const rejected = await call('PUT', `${P}/solver/contract`, { base_revision: view.revision, contract: { operations: toPayload([setField(items[0], 'operation', '不存在的操作'), items[1]]) } });
  assert.equal(rejected.status, 422);
  assert.equal(rejected.body.code, 'solver_contract_rejected');
  assert.equal((await call('PUT', `${P}/solver/contract`, { contract: { operations: 'x' } })).status, 400);

  const lock = { state: 'assistant_running', entries: [], artifacts: lockScope.artifacts, paths: [] };
  const locked = server({ lock });
  const v3 = (await locked.call('GET', `${P}/solver/contract`)).body;
  assert.equal(v3.lock.state, 'assistant_running');
  const d3 = draftOf(v3);
  const r423 = await locked.call('PUT', `${P}/solver/contract`, { base_revision: v3.revision, contract: { operations: toPayload([setTemplateParam(d3[0], 'title', 'X'), d3[1]]) } });
  assert.equal(r423.status, 423);
  // 范围外的那份照常保存
  const ok = await locked.call('PUT', `${P}/solver/contract`, { base_revision: v3.revision, contract: { operations: toPayload([d3[0], setTemplateParam(d3[1], 'title', '临期处理计划')]) } });
  assert.equal(ok.status, 200);
});

test('mock 的结构检查与前端预检同口径（几条主要规则）', () => {
  const names = ['补货编制主流程'];
  const op = (b, t) => ({ binding: { solver_key: 'k1', operation: '补货编制主流程', declaration: { engine_requirements: { problem_family: 'candidate_selection.v1', capabilities: [] } }, units: [{ field: 'a', unit: '箱' }], ...b }, template: { display_mapping: { OPTION_0: 'A', OPTION_1: 'B' }, ...t } });
  assert.deepEqual(validateOperations([op()], names).rejected, []);
  assert.equal(validateOperations([op({}, { display_mapping: { OPTION_0: 'A', OPTION_1: 'A' } })], names).rejected[0].field, 'display_mapping');
  assert.equal(validateOperations([op({}, { process_name: 'x' })], names).rejected[0].field, 'text_presentation');
  assert.equal(validateOperations([op(), op()], names).rejected[0].message, '求解标识「k1」重复');
  assert.equal(validateOperations([op({ declaration: { engine_requirements: { problem_family: 'other.v1' } } })], names).rejected[0].code, 'solver_contract_version_incompatible');
  assert.equal(validateOperations([op({ units: [] })], names).findings[0].level, 'warning');
});

// ---------- mock：页面命令 ----------

test('页面命令：修复、重新生成描述符、生成脚本、扫描，跑完数据回到列表', async () => {
  const s = server();
  const list = async () => (await s.call('GET', `${P}/scripts`)).body;
  const before = await list();
  const fix = s.run(fromPage(rowAction(before.items[2])));
  assert.equal(fix.title, '助手正在修复 overage_compare.py 的契约问题');
  fix.finish();
  const desc = s.run(fromPage(rowAction(before.items[1])));
  assert.equal(desc.title, '助手正在重新生成 build_replenish_order.py 的描述符');
  desc.finish();
  let after = await list();
  assert.deepEqual(after.items.map((r) => [r.contract.status, r.descriptor, r.sandbox.status]), [
    ['passed', 'synced', 'passed'], ['passed', 'synced', 'passed'], ['passed', 'synced', 'passed'],
  ]);
  assert.ok(s.events.some((e) => e.type === 'artifacts.updated' && e.data.type === 'codegen.script'));
  assert.equal((await s.call('GET', `${P}/scripts/overage_compare/verification`)).body.status, 'passed');

  const gen = s.run(fromPage(generateCommand(pickedOperations(before.candidates, defaultPicked(before.candidates)))));
  assert.equal(gen.title, '助手正在生成 2 个脚本并在沙箱里跑通');
  gen.finish();
  after = await list();
  assert.deepEqual(after.items.map((r) => r.script.name), ['safety_stock_strictest.py', 'build_replenish_order.py', 'overage_compare.py', 'display_qty_lookup.py']);
  assert.equal(after.items[3].operation.name, '陈列量核定');

  const scan = s.run(fromPage(scanCommand()));
  assert.equal(scan.title, '助手正在扫描可代码化的操作');
  scan.finish();
  assert.notEqual((await list()).candidates.scanned_at, before.candidates.scanned_at);
  // 不是这一页发的命令不归这里
  assert.throws(() => s.run({ ...scanCommand(), origin: { page: 'scenarios', label: '场景模拟' } }));
});

test('生成评估器：跑的时候这份求解契约进写入范围，跑完解除、修订号加 1', async () => {
  const s = server();
  const run = s.run(fromPage(evaluatorCommand('restock_route', '补货编制主流程')));
  assert.equal(run.title, '助手正在为「补货编制主流程」生成评估器');
  assert.deepEqual(run.steps, ['第 1 步 / 共 3 步', '第 2 步 / 共 3 步', '第 3 步 / 共 3 步']);
  assert.equal(s.scope.lock.state, 'assistant_running');
  const view = (await s.call('GET', `${P}/solver/contract`)).body;
  const [main, other] = draftOf(view);
  assert.equal(contractLocked(s.scope.lock, main), true);
  assert.equal(contractLocked(s.scope.lock, other), false);
  run.finish();
  assert.equal(s.scope.lock.state, 'none');
  assert.equal((await s.call('GET', `${P}/solver/contract`)).body.revision_no, 8);
  // 已经有别的锁时不覆盖
  const busy = server({ lock: { state: 'pending_confirm', entries: [], artifacts: [], paths: [], slice: { id: 's5', name: '超量与审批提示' } } });
  busy.run(fromPage(evaluatorCommand('restock_route', '补货编制主流程'))).finish();
  assert.equal(busy.scope.lock.state, 'pending_confirm');
});

test('按模板重新生成：手动编辑过的改回模板', async () => {
  const s = server();
  const view = (await s.call('GET', `${P}/solver/contract`)).body;
  const items = draftOf(view);
  const manual = toManual(items[0], items[0].sidecars);
  await s.call('PUT', `${P}/solver/contract`, { base_revision: view.revision, contract: { operations: toPayload([manual, items[1]]) } });
  assert.equal((await s.call('GET', `${P}/solver/contract`)).body.operations[0].mode, 'manual');
  const run = s.run(fromPage(templateCommand('restock_route', '补货编制主流程')));
  assert.equal(run.title, '助手正在按模板重新生成「补货编制主流程」的附属文件和评估器');
  run.finish();
  assert.equal((await s.call('GET', `${P}/solver/contract`)).body.operations[0].mode, 'template');
});

test('进行中任务的初始状态（?mock=task:scripts）', () => {
  const t = taskSeeds.scripts();
  assert.equal(t.origin.page, 'scripts');
  assert.equal(t.title, '助手正在为「补货编制主流程」生成评估器');
  assert.equal(t.step_label, '第 2 步 / 共 3 步');
  assert.deepEqual(lockScope.artifacts.map((a) => [a.type, a.key]), [[SOLVER_TYPE, 'restock_route']]);
});

// SV-05（同 develop）：旧验证用例按 {cases} 导出成 JSON 文件，没有用例时不下载
test('旧验证用例导出', () => {
  assert.equal(LEGACY_CASES_FILE, 'solver-validation-cases.json');
  assert.equal(legacyCasesText([]), null);
  assert.equal(legacyCasesText(undefined), null);
  assert.deepEqual(JSON.parse(legacyCasesText([{ id: 'case_a' }])), { cases: [{ id: 'case_a' }] });
});

