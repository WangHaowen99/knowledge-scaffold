import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  advancedText,
  collectMarks,
  diffContracts,
  disablePreview,
  draftFromView,
  draftKey,
  engineCapabilities,
  evaluatorCommandPlan,
  findProblemFamily,
  isEnabled,
  isManagedSolverPath,
  liveSaveBlocker,
  localProblems,
  locateFinding,
  newDraftItem,
  normalizeParams,
  normalizeUnits,
  problemFamily,
  regenerate,
  renderedSidecars,
  setEnabled,
  setEngineCapabilities,
  setField,
  setAdvanced,
  setOperation,
  setOptionLabel,
  setProblemFamily,
  setSidecar,
  setTemplateParam,
  shownChecks,
  suggestSolverKey,
  toManual,
  toPayload,
  unloadedDirectories,
  validationState,
} from './solver-contract-model.js';

const SIDECARS = {
  entry: 'profile_id: ordered_binary_route.v1\n',
  input_schema: '{"type": "object"}\n',
  result_schema: '{"type": "object"}\n',
  fact_snapshot_schema: '{"type": "object"}\n',
  presentation: 'columns: []\n',
};

function operation(solverKey = 'plan_route', binding = {}) {
  return {
    solver_key: solverKey,
    operation: '规划路线',
    decision_model: 'route_plan',
    enabled: true,
    evaluator: 'ontology/scripts/route_eval.py',
    binding: {
      solver_key: solverKey,
      operation: '规划路线',
      decision_model: 'route_plan',
      declaration: { engine_requirements: { problem_family: 'binary_ordered_route.v1' } },
      operation_frontmatter: {},
      units: [{ field: 'distance', unit: 'km' }],
      x_imported: { kept: true },
      ...binding,
    },
    sidecars: { ...SIDECARS },
  };
}

const view = (operations = [operation()], extra = {}) => ({
  enabled: operations.length > 0,
  revision: 'sha256:base',
  contract: { operations },
  findings: [],
  gate: [],
  validation: null,
  ...extra,
});

const PARAMS = {
  display_mapping: { OPTION_0: '方案甲', OPTION_1: '方案乙' },
  title: '路线方案',
  evaluator_path: '',
  process_name: '',
  stream_name: '',
};

// 附属文件与模板参数生成结果一致的契约（后端按字节比对判定为 managed）
function managedOperation(solverKey = 'plan_route', params = PARAMS) {
  return { ...operation(solverKey), managed: true, template_params: params, unit_options: ['U1'] };
}

test('draft round-trips to the same payload and keeps unknown binding keys', () => {
  const items = draftFromView(view());
  assert.equal(items[0].uid, 'saved:plan_route');
  assert.equal(items[0].evaluator, 'ontology/scripts/route_eval.py');
  const [payload] = toPayload(items);
  assert.deepEqual(payload.binding, operation().binding);
  assert.deepEqual(Object.keys(payload).sort(), ['binding', 'sidecars']);
  assert.equal(diffContracts(view().contract.operations, items).dirty, false);
});

test('enabled mirrors the backend three-switch rule', () => {
  assert.equal(isEnabled({}), true);
  assert.equal(isEnabled({ operation_frontmatter: { execution: { enabled: false } } }), false);
  assert.equal(isEnabled({ operation_frontmatter: { enabled: 'no' } }), false);
  assert.equal(isEnabled({ declaration: { enabled: 0 } }), false);
  assert.equal(isEnabled({ declaration: { enabled: 'Yes' } }), true);
  assert.equal(isEnabled({ operation_frontmatter: { enabled: null } }), false);
});

test('disabling writes execution.enabled and enabling clears every disable flag', () => {
  const [item] = draftFromView(view([operation('plan_route', {
    declaration: { enabled: false, engine_requirements: { problem_family: 'f' } },
    operation_frontmatter: { title: '规划', execution: { enabled: 'false', note: 'x' } },
  })]));
  assert.equal(isEnabled(item.binding), false);
  const enabled = setEnabled(item, true);
  assert.equal(isEnabled(enabled.binding), true);
  assert.deepEqual(enabled.binding.operation_frontmatter, { title: '规划', execution: { note: 'x' } });
  assert.deepEqual(enabled.binding.declaration, { engine_requirements: { problem_family: 'f' } });
  assert.equal(isEnabled(item.binding), false, 'original draft is not mutated');

  const disabled = setEnabled(enabled, false);
  assert.deepEqual(disabled.binding.operation_frontmatter.execution, { note: 'x', enabled: false });
  const reenabled = setEnabled(setEnabled(draftFromView(view())[0], false), true);
  assert.deepEqual(reenabled.binding.operation_frontmatter, {}, 'empty execution is pruned');
});

test('problem family edits keep other declaration keys', () => {
  const [item] = draftFromView(view([operation('plan_route', {
    declaration: { engine_requirements: { problem_family: 'a', max_items: 9 }, tags: ['t'] },
  })]));
  const next = setProblemFamily(item, 'b');
  assert.equal(problemFamily(next.binding), 'b');
  assert.deepEqual(next.binding.declaration, { engine_requirements: { problem_family: 'b', max_items: 9 }, tags: ['t'] });
  assert.equal(problemFamily(item.binding), 'a');
});

test('advanced fields edit everything outside the form and keep form fields', () => {
  const [item] = draftFromView(view());
  const rest = JSON.parse(advancedText(item.binding));
  assert.deepEqual(Object.keys(rest).sort(), ['declaration', 'operation_frontmatter', 'x_imported']);

  rest.declaration.features = ['time_window'];
  rest.x_new = 1;
  delete rest.x_imported;
  const next = setAdvanced(item, JSON.stringify(rest));
  assert.equal(next.advancedError, '');
  assert.deepEqual(next.binding.declaration.features, ['time_window']);
  assert.equal(next.binding.x_new, 1);
  assert.equal('x_imported' in next.binding, false);
  assert.equal(next.binding.solver_key, 'plan_route');
  assert.deepEqual(next.binding.units, [{ field: 'distance', unit: 'km' }]);
  assert.deepEqual(diffContracts(view().contract.operations, [next]).changed[0].files, ['binding.yaml']);

  const broken = setAdvanced(next, '{ not json');
  assert.match(broken.advancedError, /合法 JSON/);
  assert.equal(broken.binding, next.binding, 'invalid text leaves the binding untouched');
  assert.deepEqual(localProblems([broken], ['规划路线']).map((p) => p.field), ['advanced']);
  assert.match(setAdvanced(item, '[]').advancedError, /JSON 对象/);
  assert.match(setAdvanced(item, '{"solver_key": "x"}').advancedError, /表单/);

  // 表单改动声明/开关时高级字段编辑器重建，未生效的错误文本随之丢弃
  const touched = setProblemFamily(broken, 'other');
  assert.equal(touched.advancedError, '');
  assert.equal(touched.advancedRev, (broken.advancedRev || 0) + 1);
  assert.equal(setEnabled(item, false).advancedRev, 1);
});

test('units are trimmed, blank rows dropped and extra keys kept', () => {
  assert.deepEqual(
    normalizeUnits([{ field: ' distance ', unit: ' km', note: 'n' }, { field: '', unit: '' }, null]),
    [{ field: 'distance', unit: 'km', note: 'n' }],
  );
  const [item] = draftFromView(view([operation('plan_route', { units: undefined })]));
  delete item.binding.units;
  assert.equal('units' in toPayload([item])[0].binding, false, 'absent units stay absent');
});

test('new contracts start from the template with an automatic key and form parameters', () => {
  const template = {
    declaration: { engine_requirements: { problem_family: 'p' } },
    params: {
      display_mapping: { OPTION_0: '', OPTION_1: '' }, title: '', evaluator_path: '', process_name: '', stream_name: '',
    },
  };
  const items = draftFromView(view());
  const added = newDraftItem(template, '规划路线', items);
  assert.equal(added.origin, null);
  assert.equal(added.managed, true);
  assert.equal(added.binding.solver_key, 'route_choice_1', 'a Chinese operation name still gets a valid key');
  assert.equal(added.binding.decision_model, 'route_choice_1_model');
  assert.equal(problemFamily(added.binding), 'p');
  assert.equal(added.params.title, '规划路线', 'the title defaults to the operation name');
  assert.deepEqual(added.sidecars, {});
  assert.deepEqual(localProblems([added], ['规划路线']).map((p) => p.field), ['display_mapping']);
  const next = newDraftItem(template, '', [...items, added]);
  assert.notEqual(added.uid, next.uid);
  assert.equal(next.binding.solver_key, 'route_choice_2');
  assert.equal(newDraftItem(template, 'Route-Plan', items).binding.solver_key, 'route_plan');
  template.declaration.engine_requirements.problem_family = 'changed';
  assert.equal(problemFamily(added.binding), 'p', 'the template declaration is copied');
  assert.equal(suggestSolverKey('Route-Plan'), 'route_plan');
  assert.equal(suggestSolverKey('route_plan', ['route_plan']), '');
  assert.equal(suggestSolverKey('9route'), '');
});

test('template parameters are trimmed and limited to the known keys', () => {
  const empty = {
    display_mapping: { OPTION_0: '', OPTION_1: '' }, title: '', evaluator_path: '', process_name: '', stream_name: '',
  };
  assert.deepEqual(normalizeParams(null), empty);
  assert.deepEqual(
    normalizeParams({
      display_mapping: { OPTION_0: ' 甲 ', OPTION_2: '丙' }, title: 3, evaluator_path: ' scripts/e.py ', x: 1,
      process_name: ' 工序甲 ', stream_name: null,
    }),
    {
      display_mapping: { OPTION_0: '甲', OPTION_1: '' }, title: '', evaluator_path: 'scripts/e.py',
      process_name: '工序甲', stream_name: '',
    },
  );
});

test('template contracts submit their parameters and manual ones their sidecars', () => {
  const base = [managedOperation()];
  const [item] = draftFromView(view(base));
  assert.equal(item.managed, true);
  assert.deepEqual(item.params, PARAMS);
  const [payload] = toPayload([item]);
  assert.deepEqual(Object.keys(payload).sort(), ['binding', 'template']);
  assert.deepEqual(payload.template, PARAMS);
  assert.equal(diffContracts(base, [item]).dirty, false);
  assert.equal(diffContracts(base, [setOptionLabel(item, 'OPTION_0', ' 方案甲 ')]).dirty, false, 'spaces are not a change');

  const manual = toManual(item, SIDECARS);
  assert.deepEqual(Object.keys(toPayload([manual])[0]).sort(), ['binding', 'sidecars']);
  assert.equal(diffContracts(base, [manual]).dirty, false, 'switching to manual alone keeps the same bytes');
  const edited = setSidecar(manual, 'presentation', 'columns: [x]\n');
  assert.deepEqual(diffContracts(base, [edited]).changed[0].files, ['presentation.yaml']);
});

test('parameter edits report only the files the template writes them to', () => {
  const base = [managedOperation()];
  const [item] = draftFromView(view(base));
  const files = (next) => diffContracts(base, [next]).changed[0]?.files || [];
  assert.deepEqual(files(setOptionLabel(item, 'OPTION_1', '方案丙')), ['model.yaml']);
  assert.deepEqual(files(setTemplateParam(item, 'evaluator_path', 'scripts/other_eval.py')), ['model.yaml']);
  assert.deepEqual(files(setTemplateParam(item, 'title', '新标题')), ['presentation.yaml']);
  assert.deepEqual(files(setTemplateParam(item, 'stream_name', '物流甲')), ['model.yaml']);
  assert.deepEqual(
    files(setField(setTemplateParam(item, 'title', '新标题'), 'decision_model', 'route_plan_v2')),
    ['binding.yaml', 'presentation.yaml'],
  );

  // 手动编辑过的契约按模板重新生成：五份附属文件整体覆盖
  const [raw] = draftFromView(view());
  assert.equal(raw.managed, false);
  assert.deepEqual(diffContracts([operation()], [regenerate(raw)]).changed[0].files, [
    'model.yaml', 'input.schema.json', 'result.schema.json', 'fact-snapshot.schema.json', 'presentation.yaml',
  ]);
});

test('the preview uses the live render and falls back to saved files while parameters are unchanged', () => {
  const [item] = draftFromView(view([managedOperation()]));
  assert.deepEqual(renderedSidecars(item), SIDECARS);
  const rendered = { ...SIDECARS, presentation: 'title: 新标题\n' };
  assert.equal(renderedSidecars(item, rendered), rendered);

  const edited = setTemplateParam(item, 'title', '新标题');
  assert.equal(renderedSidecars(edited), null, 'changed parameters wait for the live render');
  assert.deepEqual(renderedSidecars(setTemplateParam(edited, 'title', ` ${PARAMS.title}`)), SIDECARS);
  assert.equal(renderedSidecars(newDraftItem({}, '规划路线', [])), null, 'a new contract has nothing saved');

  const manual = toManual(edited, rendered);
  assert.equal(manual.managed, false);
  assert.deepEqual(manual.sidecars, rendered);
  assert.deepEqual(localProblems([manual], ['规划路线']), []);
  const back = regenerate(manual);
  assert.equal(back.managed, true);
  assert.deepEqual(renderedSidecars(back), rendered, 'untouched manual text is still the render of these parameters');
  assert.equal(renderedSidecars(regenerate(setSidecar(manual, 'entry', 'x\n'))), null, 'edited text is not a render');
});

test('template parameter problems block saving before the backend would', () => {
  const [item] = draftFromView(view([managedOperation()]));
  const problems = (next) => localProblems([next], ['规划路线']).map((p) => [p.field, p.message]);
  assert.deepEqual(problems(item), []);
  assert.deepEqual(problems({ ...item, sidecars: {} }), [], 'template contracts need no sidecar text');
  assert.match(problems(setOptionLabel(item, 'OPTION_0', ' '))[0][1], /填写两个选项/);
  assert.match(problems(setOptionLabel(item, 'OPTION_1', ' 方案甲 '))[0][1], /相同/);
  assert.match(problems(setOptionLabel(item, 'OPTION_0', 'x'.repeat(129)))[0][1], /128/);
  assert.deepEqual(problems(setOptionLabel(item, 'OPTION_0', '𝑥'.repeat(128))), [], 'length counts characters');
  assert.deepEqual(problems(setOptionLabel(item, 'OPTION_0', '甲\n乙')).map(([field]) => field), ['display_mapping']);
  assert.deepEqual(problems(setTemplateParam(item, 'title', '标\u0007题')).map(([field]) => field), ['title']);
  assert.deepEqual(problems(setTemplateParam(item, 'title', '标题\n\u0085')), [], 'edge whitespace is stripped like the backend');

  // 结果文字：两项同时填写或都留空，口径同后端 _check_text_presentation
  const named = setTemplateParam(setTemplateParam(item, 'process_name', '工序甲'), 'stream_name', '物流甲');
  assert.deepEqual(problems(named), []);
  assert.deepEqual(problems(setTemplateParam(named, 'stream_name', ' ')).map(([field]) => field), ['text_presentation']);
  assert.match(problems(setTemplateParam(named, 'stream_name', ''))[0][1], /同时填写/);
  assert.match(problems(setTemplateParam(named, 'process_name', 'x'.repeat(129)))[0][1], /128/);
  assert.deepEqual(problems(setTemplateParam(named, 'process_name', '工\u0007序')).map(([field]) => field), ['text_presentation']);
});

test('template findings point at the form field that produced them', () => {
  const items = draftFromView(view([managedOperation()]));
  const uid = 'saved:plan_route';
  const at = (file, field, list = items) => locateFinding({ path: `ontology/solver/plan_route/${file}`, field }, list);
  assert.deepEqual(at('model.yaml', 'display_mapping'), { uid, field: 'display_mapping' });
  assert.deepEqual(at('model.yaml', 'display_mapping.OPTION_1'), { uid, field: 'display_mapping' });
  assert.deepEqual(at('model.yaml', 'template'), { uid, field: 'display_mapping' });
  assert.deepEqual(at('model.yaml', 'fact_source.evaluator_path'), { uid, field: 'evaluator_path' });
  assert.deepEqual(at('presentation.yaml', 'title'), { uid, field: 'title' });
  assert.deepEqual(at('model.yaml', 'text_presentation'), { uid, field: 'text_presentation' });
  assert.deepEqual(at('model.yaml', 'process_name'), { uid, field: 'text_presentation' });
  assert.deepEqual(at('model.yaml', 'objectives'), { uid, role: 'entry' });
  assert.deepEqual(at('model.yaml', 'display_mapping', [toManual(items[0], SIDECARS)]), { uid, role: 'entry' });

  const local = localProblems([setOptionLabel(items[0], 'OPTION_0', '')], ['规划路线']);
  const marks = collectMarks(items, local, [{ path: 'ontology/solver/plan_route/model.yaml', field: 'display_mapping' }]);
  assert.equal(marks.get(uid).count, 1, 'the server finding on the same field is not counted twice');
});

test('changing the operation carries along a title that was never edited', () => {
  const added = newDraftItem({}, '规划路线', []);
  assert.equal(setOperation(added, '查询站点').params.title, '查询站点');
  const titled = setTemplateParam(added, 'title', '自定义标题');
  assert.equal(setOperation(titled, '查询站点').params.title, '自定义标题');
  const [raw] = draftFromView(view());
  assert.deepEqual(setOperation(raw, '查询站点').params, raw.params, 'manual contracts keep their parameters');
});

test('local problems flag key, operation, units and empty sidecars; a missing decision model is filled in on save', () => {
  const [base] = draftFromView(view());
  const second = { ...setField(setField(base, 'decision_model', ' '), 'units', [{ field: 'd', unit: '' }]), uid: 'new:2', origin: null };
  const problems = localProblems([base, setSidecar(second, 'presentation', '  ')], ['规划路线', '查询站点']);
  const byField = (uid) => problems.filter((p) => p.uid === uid).map((p) => p.field).sort();
  assert.deepEqual(byField('saved:plan_route'), ['operation', 'solver_key']);
  assert.deepEqual(byField('new:2'), ['operation', 'presentation', 'solver_key', 'units']);
  // 表单上没有 Decision Model ID：不报给用户，提交时按求解标识补齐
  const [payload] = toPayload([second]);
  assert.equal(payload.binding.decision_model, `${String(second.binding.solver_key).trim()}_model`);

  const clash = setField(base, 'solver_key', 'cha_xun');
  assert.match(localProblems([clash], ['规划路线', 'cha_xun'])[0].message, /同名/);
  assert.equal(localProblems([setField(base, 'solver_key', 'Plan')], ['规划路线'])[0].field, 'solver_key');
  const warning = localProblems([setProblemFamily(base, '')], ['规划路线']);
  assert.deepEqual(warning.map((p) => [p.field, p.severity]), [['problem_family', 'warning']]);
  assert.deepEqual(localProblems([base], ['规划路线']), []);
});

const FAMILIES = [{
  id: 'candidate_selection.v1',
  capabilities: ['domain_candidate_evaluation', 'lexicographic_objective'],
  aliases: ['binary_ordered_route.v1'],
}];

test('problem family and capabilities are checked against the registered catalog', () => {
  const [base] = draftFromView(view());
  const current = setProblemFamily(base, 'candidate_selection.v1');
  const warnings = (item) => localProblems([item], ['规划路线'], FAMILIES).map((p) => [p.field, p.severity, p.message]);
  assert.deepEqual(warnings(current), []);
  assert.deepEqual(warnings(setEngineCapabilities(current, ['lexicographic_objective'])), []);

  const [[field, severity, legacy]] = warnings(base);
  assert.deepEqual([field, severity], ['problem_family', 'warning']);
  assert.match(legacy, /旧名称.*candidate_selection\.v1/);

  const [[, , unknown]] = warnings(setProblemFamily(base, 'vehicle_routing.v1'));
  assert.match(unknown, /不在平台注册目录中.*支持范围：candidate_selection\.v1/);

  const unsupported = warnings(setEngineCapabilities(current, ['time_windows', 'lexicographic_objective', 'column_generation']));
  assert.deepEqual(unsupported.map(([name, level]) => [name, level]), [['engine_capabilities', 'warning']]);
  assert.match(unsupported[0][2], /不支持能力：time_windows、column_generation/);

  const malformed = setAdvanced(current, JSON.stringify({
    declaration: { engine_requirements: { problem_family: 'candidate_selection.v1', capabilities: 'all' } },
  }));
  assert.match(warnings(malformed)[0][2], /须是能力名列表/);
  // 目录未加载时不做比对，以免误报
  assert.deepEqual(localProblems([setProblemFamily(base, 'vehicle_routing.v1')], ['规划路线']), []);
});

test('catalog lookup resolves legacy aliases and capability edits keep the declaration', () => {
  assert.equal(findProblemFamily(FAMILIES, ' binary_ordered_route.v1 ').id, 'candidate_selection.v1');
  assert.equal(findProblemFamily(FAMILIES, 'candidate_selection.v1').id, 'candidate_selection.v1');
  assert.equal(findProblemFamily(FAMILIES, 'other.v1'), null);
  assert.equal(findProblemFamily([], 'candidate_selection.v1'), null);

  const [item] = draftFromView(view([operation('plan_route', {
    declaration: { engine_requirements: { problem_family: 'candidate_selection.v1', max_items: 9 }, tags: ['t'] },
  })]));
  assert.deepEqual(engineCapabilities(item.binding), []);
  const next = setEngineCapabilities(item, ['lexicographic_objective']);
  assert.deepEqual(next.binding.declaration, {
    engine_requirements: { problem_family: 'candidate_selection.v1', max_items: 9, capabilities: ['lexicographic_objective'] },
    tags: ['t'],
  });
  assert.equal(next.advancedRev, 1, 'advanced editor is rebuilt after a form edit');
  assert.deepEqual(engineCapabilities(item.binding), [], 'original draft is not mutated');
  assert.deepEqual(
    locateFinding({ path: 'ontology/solver/plan_route/binding.yaml', field: 'declaration.engine_requirements.capabilities' }, [item]),
    { uid: 'saved:plan_route', field: 'engine_capabilities' },
  );
});

test('diff reports added, removed, renamed, changed files and enable transitions', () => {
  const base = [operation('plan_route'), operation('other_route', { operation: '查询站点' })];
  const [first, second] = draftFromView(view(base));
  const template = { declaration: {} };
  const items = [
    setEnabled(setSidecar(setField(first, 'solver_key', 'plan_route_v2'), 'entry', 'changed\n'), false),
    newDraftItem(template, '查询站点', [first, second]),
  ];
  const diff = diffContracts(base, items);
  assert.equal(diff.dirty, true);
  assert.deepEqual(diff.changed, [{
    uid: 'saved:plan_route', solverKey: 'plan_route_v2', renamedFrom: 'plan_route',
    files: ['binding.yaml', 'model.yaml'],
  }]);
  assert.deepEqual(diff.disabled, ['plan_route_v2']);
  assert.deepEqual(diff.removed.map((item) => item.solverKey), ['other_route']);
  assert.equal(diff.added.length, 1);
  assert.deepEqual(disablePreview(diff), [
    { solverKey: 'plan_route_v2', reason: 'disabled' },
    { solverKey: 'other_route', reason: 'removed' },
  ]);
  assert.equal(diffContracts(base, []).revertsToPlain, true);
  assert.equal(diffContracts([], items).becomesSolverPackage, true);
});

test('re-enabling a disabled contract is reported as enabled', () => {
  const base = [operation('plan_route', { operation_frontmatter: { execution: { enabled: false } } })];
  const [item] = draftFromView(view(base));
  const diff = diffContracts(base, [setEnabled(item, true)]);
  assert.deepEqual(diff.enabled, ['plan_route']);
  assert.deepEqual(disablePreview(diff), []);
});

test('findings locate the draft item, sidecar tab, form field or plain file', () => {
  const items = draftFromView(view());
  const renamed = [setField(items[0], 'solver_key', 'plan_route_v2')];
  const at = (path, field = '') => locateFinding({ path, field }, items);
  assert.deepEqual(at('ontology/solver/plan_route/model.yaml', 'profile_id'), { uid: 'saved:plan_route', role: 'entry' });
  assert.deepEqual(at('ontology/solver/plan_route/binding.yaml', 'declaration.engine_requirements.problem_family'), {
    uid: 'saved:plan_route', field: 'problem_family',
  });
  assert.deepEqual(at('ontology/solver/plan_route/binding.yaml', 'x_unknown'), { uid: 'saved:plan_route', role: 'advanced' });
  assert.deepEqual(at('ontology/solver/plan_route/binding.yaml'), { uid: 'saved:plan_route' });
  assert.deepEqual(at('ontology/scripts/route_eval.py'), { file: 'ontology/scripts/route_eval.py' });
  assert.deepEqual(at('.ontology-state/solver-validations.json'), { validation: true });
  assert.deepEqual(at('ontology/solver/ghost/binding.yaml'), { file: 'ontology/solver/ghost/binding.yaml' });
  assert.deepEqual(locateFinding({ path: '', field: 'operations[0]' }, items), { uid: 'saved:plan_route' });
  assert.equal(locateFinding({ path: '', field: '' }, items), null);
  // 保存被拒时 finding 指向草稿里的新 key，仍能找回同一条
  assert.deepEqual(
    locateFinding({ path: 'ontology/solver/plan_route_v2/binding.yaml', field: 'operation' }, renamed),
    { uid: 'saved:plan_route', field: 'operation' },
  );
});

test('marks list every finding on a tab and skip spots the local check already flags', () => {
  const items = draftFromView(view());
  const uid = 'saved:plan_route';
  const local = [{ uid, field: 'operation', severity: 'error', message: '本地' }];
  const model = (field) => ({ path: 'ontology/solver/plan_route/model.yaml', field, message: field });
  const findings = [
    model('required_facts'),
    model('objectives'),
    { path: 'ontology/solver/plan_route/binding.yaml', field: 'operation', message: '服务端' },
    { path: 'ontology/solver/ghost/binding.yaml', field: 'operation' },
  ];
  const entry = collectMarks(items, local, findings).get(uid);
  assert.deepEqual(entry.roles.get('entry').map((mark) => mark.field), ['required_facts', 'objectives']);
  assert.equal(entry.fields.get('operation').message, '本地');
  assert.equal(entry.count, 3, 'the same spot is counted once');
});

test('the panel shows the live draft check while editing and the saved revision check otherwise', () => {
  const saved = view([operation()], { findings: [{ code: 'saved' }], gate: [{ code: 'gate' }] });
  const rejected = [{ code: 'rejected' }];
  const live = { key: 'k', rejected: [{ code: 'blocking' }], findings: [{ code: 'draft' }] };
  assert.deepEqual(shownChecks({ dirty: true, live, rejected, view: saved }), {
    draft: true, rejected: live.rejected, findings: live.findings,
  });
  assert.deepEqual(shownChecks({ dirty: true, live: null, rejected, view: saved }), {
    draft: false, rejected, findings: [{ code: 'saved' }, { code: 'gate' }],
  });
  assert.equal(shownChecks({ dirty: false, live, rejected: [], view: saved }).draft, false);
  assert.deepEqual(shownChecks(), { draft: false, rejected: [], findings: [] });
});

test('only a live check of the current draft that predicts a rejection blocks saving', () => {
  const items = draftFromView(view());
  const key = draftKey(items);
  assert.deepEqual(JSON.parse(key), toPayload(items));
  assert.notEqual(draftKey([setField(items[0], 'decision_model', 'route_plan_v2')]), key);
  const live = { key, rejected: [{ message: '绑定的工作区 Operation「示例」不存在' }], findings: [] };
  assert.equal(liveSaveBlocker(live, key), '保存会被拒绝：绑定的工作区 Operation「示例」不存在');
  assert.equal(liveSaveBlocker(live, 'stale'), '', 'a result for an older draft does not block');
  assert.equal(liveSaveBlocker({ ...live, rejected: [] }, key), '');
  assert.equal(liveSaveBlocker(null, key), '');
});

test('directories that failed to load are listed so saving does not drop them silently', () => {
  const findings = [
    { path: 'ontology/solver/broken/binding.yaml' },
    { path: 'ontology/solver/plan_route/model.yaml' },
    { path: 'ontology/scripts/route_eval.py' },
  ];
  assert.deepEqual(unloadedDirectories(view([operation()], { findings })), ['broken']);
});

test('validation state follows the gate of the current revision', () => {
  const status = (extra) => ({ enabled: true, operations: [{ enabled: true }], gate: [], validation: null, ...extra });
  assert.equal(validationState({ enabled: false }), 'none');
  assert.equal(validationState(status({ operations: [{ enabled: false }] })), 'not_needed');
  assert.equal(validationState(view()), 'required');
  assert.equal(validationState(status({ validation: { status: 'running' } })), 'running');
  assert.equal(validationState(status({ validation: { status: 'passed', authoritative: true } })), 'passed');
  assert.equal(validationState(status({ validation: { status: 'passed', authoritative: false } })), 'failed');
  assert.equal(validationState(status({ validation: { status: 'failed' } })), 'failed');
  assert.equal(
    validationState(status({ gate: [{ code: 'solver_contract_validation_stale' }] })),
    'stale',
  );
});

test('managed path detection covers the directory and its files only', () => {
  assert.equal(isManagedSolverPath('ontology/solver/plan_route/model.yaml'), true);
  assert.equal(isManagedSolverPath('ontology/solver'), true);
  assert.equal(isManagedSolverPath('ontology/solvers/x.md'), false);
  assert.equal(isManagedSolverPath('ontology/operation/规划路线.md'), false);
});

test('the evaluator command saves pending drafts first and targets the saved solver key', () => {
  const [saved] = draftFromView(view([managedOperation()]));
  assert.deepEqual(evaluatorCommandPlan(saved), {
    command: '/ontology-solver-evaluator plan_route', needsSave: false, blocker: '',
  });
  // 有未保存的修改：先保存，命令按保存后的求解标识
  const renamed = setField(saved, 'solver_key', 'other_route');
  assert.deepEqual(evaluatorCommandPlan(renamed, { dirty: true }), {
    command: '/ontology-solver-evaluator other_route', needsSave: true, blocker: '',
  });
  // 保存不可用时不能发起；不需要保存时与保存状态无关
  assert.equal(evaluatorCommandPlan(renamed, { dirty: true, saveBlocker: '还有 1 个问题需要先修正' }).blocker,
    '还有 1 个问题需要先修正');
  assert.equal(evaluatorCommandPlan(saved, { saveBlocker: '还有 1 个问题需要先修正' }).blocker, '');
  const draft = setField(newDraftItem({}, '规划路线', [saved]), 'solver_key', 'new_route');
  assert.deepEqual(evaluatorCommandPlan(draft), {
    command: '/ontology-solver-evaluator new_route', needsSave: true, blocker: '',
  }, 'unsaved contracts are saved first');
  assert.match(evaluatorCommandPlan(setField(saved, 'solver_key', 'Bad Key'), { dirty: true }).blocker, /求解标识/);
  const [invalid] = draftFromView(view([operation('Bad Key')]));
  assert.match(evaluatorCommandPlan(invalid).blocker, /求解标识/);
});
