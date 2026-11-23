// 求解模型面板的纯状态逻辑：草稿与服务端契约互转、本地预检、差异与停用预览、问题定位与即时检查。
// 口径对齐后端 solver_contract.py（SIDECARS、SOLVER_KEY_RE、三处 enabled 开关）；
// 保存时的结构校验与发布门禁仍以后端为准，这里只做提前提示；Profile 等语义规则不在前端复写，
// 由即时检查接口按后端同一套规则给出。

export const SOLVER_DIR = 'ontology/solver/';
export const BINDING_FILE = 'binding.yaml';
export const SOLVER_KEY_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
export const VALIDATIONS_FILE = '.ontology-state/solver-validations.json';

export const SIDECAR_TABS = [
  { role: 'entry', file: 'model.yaml', label: 'Decision Model' },
  { role: 'input_schema', file: 'input.schema.json', label: '输入 Schema' },
  { role: 'result_schema', file: 'result.schema.json', label: '结果 Schema' },
  { role: 'fact_snapshot_schema', file: 'fact-snapshot.schema.json', label: '事实快照 Schema' },
  { role: 'presentation', file: 'presentation.yaml', label: 'Presentation' },
];

export const FINDING_TITLES = {
  // 该错误码同时覆盖不支持的 Profile 与评估器不符合 Consumer 兼容层接口，标题不能只说版本
  solver_contract_version_incompatible: '契约不兼容',
  solver_contract_reference_missing: '引用缺失',
  solver_context_missing: 'SOLVER_CONTEXT 不完整',
  solver_runtime_binding_forbidden: '含运行时绑定',
  solver_contract_invalid: '契约不合法',
  solver_contract_validation_required: '待验证',
  solver_contract_validation_stale: '验证已过期',
  solver_contract_validation_failed: '验证未通过',
  solver_contract_revision_conflict: '修订冲突',
};

// 端到端验证改在发布后的独立评测中进行：这里只区分是否需要在发布后验证
export const VALIDATION_STATES = {
  none: { label: '未启用求解', color: 'default' },
  not_needed: { label: '无需验证', color: 'default' },
  after_publish: { label: '发布后验证', color: 'processing' },
};

// 表单直接编辑的 binding 字段；其余（声明里的能力与策略、Operation frontmatter、
// 导入时保留的扩展字段）在「高级字段」页签以 JSON 编辑
const FORM_KEYS = ['solver_key', 'operation', 'decision_model', 'units'];
export const ADVANCED = 'advanced';

// binding 字段 → 面板表单项；不在表中的字段定位到「高级字段」
const BINDING_FIELDS = {
  solver_key: 'solver_key',
  operation: 'operation',
  decision_model: 'decision_model',
  'declaration.engine_requirements.problem_family': 'problem_family',
  'declaration.engine_requirements.capabilities': 'engine_capabilities',
  'declaration.engine_requirements': 'problem_family',
  declaration: 'problem_family',
  operation_frontmatter: 'enabled',
  'operation_frontmatter.execution': 'enabled',
  units: 'units',
};

const clone = (value) => (value == null ? value : JSON.parse(JSON.stringify(value)));
const isObject = (value) => value != null && typeof value === 'object' && !Array.isArray(value);

// 与后端 json/yaml 解析结果比较用：键序无关
function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (isObject(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stable(value[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value ?? null);
}

export const isManagedSolverPath = (path) =>
  path === SOLVER_DIR.slice(0, -1) || String(path || '').startsWith(SOLVER_DIR);

// ---- Profile 模板：表单只填模板参数，五份附属文件由后端按 Profile 确定性生成 ----
// 参数口径同后端 normalize_template_params；生成结果由即时检查接口返回（rendered）

export const ROUTE_OPTIONS = ['OPTION_0', 'OPTION_1'];
export const MAX_TEMPLATE_TEXT = 128; // 标题、选项显示名称与结果文字名称的长度上限
// 结果文字的两个名称（口径同后端 TEXT_PRESENTATION_FIELDS）：都留空时不生成结果文字
export const TEXT_PRESENTATION_FIELDS = ['process_name', 'stream_name'];

// 与 Python str.strip() 去掉的空白一致（比 String.prototype.trim 多 \x1c-\x1f、\x85，不含 \ufeff）
const PY_EDGE_SPACE = /^[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;
const trimmed = (value) => (typeof value === 'string' ? value.replace(PY_EDGE_SPACE, '') : '');
const charLength = (value) => [...String(value)].length; // 与后端 len() 一样按字符计

// 与后端 _CONTROL_CHARS_RE（\x00-\x1f、\x7f-\x9f）同口径
function hasControlChar(value) {
  for (const char of String(value)) {
    const code = char.codePointAt(0);
    if (code <= 0x1f || (code >= 0x7f && code <= 0x9f)) return true;
  }
  return false;
}

export function normalizeParams(params) {
  const source = isObject(params) ? params : {};
  const mapping = isObject(source.display_mapping) ? source.display_mapping : {};
  return {
    display_mapping: Object.fromEntries(ROUTE_OPTIONS.map((option) => [option, trimmed(mapping[option])])),
    title: trimmed(source.title),
    evaluator_path: trimmed(source.evaluator_path),
    ...Object.fromEntries(TEXT_PRESENTATION_FIELDS.map((name) => [name, trimmed(source[name])])),
  };
}

const paramsKey = (params) => stable(normalizeParams(params));

// ---- 草稿 ----

function draftItem(operation, uid) {
  const managed = operation.managed === true;
  const params = normalizeParams(operation.template_params);
  return {
    uid,
    origin: operation.solver_key, // 加载时的 solver_key：差异比对与问题定位按它找回原契约
    binding: clone(operation.binding) || {},
    sidecars: { ...(operation.sidecars || {}) },
    evaluator: operation.evaluator || '',
    managed, // 附属文件由模板参数生成；false 时按原文编辑
    params,
    // 当前 sidecars 是哪份参数的生成结果：参数没改过时预览直接用它
    renderedFrom: managed ? paramsKey(params) : null,
  };
}

export function draftFromView(view) {
  return (view?.contract?.operations || []).map((operation) =>
    draftItem(operation, `saved:${operation.solver_key}`),
  );
}

// 由 Operation 文件名推一个 solver_key：只在本身就是合法 ASCII 标识时采用，中文名留给用户填写
export function suggestSolverKey(operation, taken = []) {
  const key = String(operation || '')
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return SOLVER_KEY_PATTERN.test(key) && !taken.includes(key) ? key : '';
}

// 中文 Operation 名推不出标识时按序号给一个，业务用户不必理解 solver_key 也能直接保存
function autoSolverKey(operation, taken) {
  const suggested = suggestSolverKey(operation, taken);
  if (suggested) return suggested;
  let index = 1;
  while (taken.includes(`route_choice_${index}`)) index += 1;
  return `route_choice_${index}`;
}

// template 为 view.template：{declaration, params}
export function newDraftItem(template, operation, items = []) {
  let index = items.length + 1;
  while (items.some((item) => item.uid === `new:${index}`)) index += 1;
  const solverKey = autoSolverKey(operation, items.map((item) => String(item.binding.solver_key || '').trim()));
  const params = normalizeParams(template?.params);
  return {
    uid: `new:${index}`,
    origin: null,
    binding: {
      solver_key: solverKey,
      operation: operation || '',
      decision_model: `${solverKey}_model`,
      declaration: clone(template?.declaration) || {},
      operation_frontmatter: {},
      units: [],
    },
    sidecars: {},
    evaluator: '',
    managed: true,
    params: { ...params, title: params.title || operation || '' },
    renderedFrom: null,
  };
}

export function setField(item, field, value) {
  return { ...item, binding: { ...item.binding, [field]: value } };
}

// 换绑 Operation 时，标题仍是原 Operation 名（未手动改过）就跟着换
export function setOperation(item, operation) {
  const next = setField(item, 'operation', operation);
  if (!item.managed || item.params.title !== String(item.binding.operation || '')) return next;
  return { ...next, params: { ...item.params, title: operation || '' } };
}

export function setTemplateParam(item, name, value) {
  return { ...item, params: { ...item.params, [name]: value ?? '' } };
}

export function setOptionLabel(item, option, value) {
  const mapping = { ...item.params.display_mapping, [option]: value };
  return setTemplateParam(item, 'display_mapping', mapping);
}

export function setSidecar(item, role, text) {
  return { ...item, sidecars: { ...item.sidecars, [role]: text }, renderedFrom: null };
}

// 模板条目的附属文件预览：即时检查的生成结果优先，参数没改过时用已保存的文件；都没有时为 null（生成中）
export function renderedSidecars(item, rendered = null) {
  if (rendered) return rendered;
  return item.renderedFrom && item.renderedFrom === paramsKey(item.params) ? item.sidecars : null;
}

// 转为手动编辑：以当前生成结果为起点按原文编辑，此后保存不再按模板生成
export function toManual(item, sidecars) {
  return { ...item, managed: false, sidecars: { ...sidecars }, renderedFrom: paramsKey(item.params) };
}

// 按模板重新生成：手动改过的附属文件在保存时被模板产物覆盖
export function regenerate(item) {
  return { ...item, managed: true };
}

// ---- 启停：与后端 WorkspaceSolverContract.enabled 同口径（三处开关，缺省为启用）----

function flag(container) {
  if (!isObject(container) || !('enabled' in container)) return true;
  const value = container.enabled;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value === 1;
  return ['true', '1', 'yes', 'on', 'enabled'].includes(String(value ?? '').trim().toLowerCase());
}

export function isEnabled(binding) {
  const frontmatter = isObject(binding?.operation_frontmatter) ? binding.operation_frontmatter : {};
  return flag(frontmatter) && flag(frontmatter.execution) && flag(binding?.declaration);
}

export function setEnabled(item, enabled) {
  const binding = clone(item.binding);
  const frontmatter = isObject(binding.operation_frontmatter) ? binding.operation_frontmatter : {};
  binding.operation_frontmatter = frontmatter;
  if (enabled) {
    // 去掉任何一处的停用标记（导入包可能把开关放在 frontmatter 或声明里）
    if (!flag(frontmatter)) delete frontmatter.enabled;
    if (isObject(frontmatter.execution) && !flag(frontmatter.execution)) {
      delete frontmatter.execution.enabled;
      if (!Object.keys(frontmatter.execution).length) delete frontmatter.execution;
    }
    if (isObject(binding.declaration) && !flag(binding.declaration)) delete binding.declaration.enabled;
  } else {
    frontmatter.execution = { ...(isObject(frontmatter.execution) ? frontmatter.execution : {}), enabled: false };
  }
  return touchAdvanced({ ...item, binding });
}

export function problemFamily(binding) {
  const requirements = binding?.declaration?.engine_requirements;
  return isObject(requirements) ? String(requirements.problem_family ?? '') : '';
}

export function setProblemFamily(item, value) {
  const declaration = isObject(item.binding.declaration) ? clone(item.binding.declaration) : {};
  const requirements = isObject(declaration.engine_requirements) ? declaration.engine_requirements : {};
  declaration.engine_requirements = { ...requirements, problem_family: value };
  return touchAdvanced({ ...item, binding: { ...item.binding, declaration } });
}

export function engineCapabilities(binding) {
  const capabilities = binding?.declaration?.engine_requirements?.capabilities;
  return Array.isArray(capabilities) ? capabilities.map(String) : [];
}

export function setEngineCapabilities(item, values) {
  const declaration = isObject(item.binding.declaration) ? clone(item.binding.declaration) : {};
  const requirements = isObject(declaration.engine_requirements) ? declaration.engine_requirements : {};
  declaration.engine_requirements = { ...requirements, capabilities: [...values] };
  return touchAdvanced({ ...item, binding: { ...item.binding, declaration } });
}

// 问题族目录（view.problem_families，口径同后端 PROBLEM_FAMILY_CATALOG）：旧名称按别名归到注册名
export function findProblemFamily(families = [], name = '') {
  const value = String(name || '').trim();
  return families.find((family) => family.id === value || (family.aliases || []).includes(value)) || null;
}

// ---- 高级字段：binding 中表单以外的部分 ----

export function advancedText(binding) {
  const rest = Object.fromEntries(Object.entries(binding || {}).filter(([key]) => !FORM_KEYS.includes(key)));
  return `${JSON.stringify(rest, null, 2)}\n`;
}

// 表单改了声明或 frontmatter：高级字段编辑器据 advancedRev 重建，丢弃其中未生效的文本
function touchAdvanced(item) {
  return { ...item, advancedRev: (item.advancedRev || 0) + 1, advancedError: '' };
}

// 合法 JSON 对象才写回 binding；不合法时 binding 不变，记下错误并阻止保存
export function setAdvanced(item, text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return { ...item, advancedError: `高级字段不是合法 JSON：${error.message}` };
  }
  if (!isObject(parsed)) return { ...item, advancedError: '高级字段须是一个 JSON 对象' };
  const misplaced = FORM_KEYS.filter((key) => key in parsed);
  if (misplaced.length) {
    return { ...item, advancedError: `${misplaced.join('、')} 在上方表单中编辑，不要写在高级字段里` };
  }
  const kept = Object.fromEntries(FORM_KEYS.filter((key) => key in item.binding).map((key) => [key, item.binding[key]]));
  return { ...item, binding: { ...kept, ...parsed }, advancedError: '' };
}

// ---- 单位：存于 binding.units，[{field, unit}]，保留行内其他键 ----

export function normalizeUnits(units) {
  if (!Array.isArray(units)) return units;
  return units
    .map((row) => (isObject(row) ? row : {}))
    .map((row) => ({ ...row, field: String(row.field ?? '').trim(), unit: String(row.unit ?? '').trim() }))
    .filter((row) => row.field || row.unit);
}

// ---- 提交 ----

function normalizeBinding(binding) {
  const out = { ...binding };
  for (const key of ['solver_key', 'operation', 'decision_model']) {
    if (typeof out[key] === 'string') out[key] = out[key].trim();
  }
  if ('units' in out) out.units = normalizeUnits(out.units);
  return out;
}

// 模板条目只提交参数（后端生成并忽略 sidecars），手动条目提交原文
export function toPayload(items) {
  return items.map((item) => {
    const binding = normalizeBinding(item.binding);
    return item.managed
      ? { binding, template: normalizeParams(item.params) }
      : { binding, sidecars: { ...item.sidecars } };
  });
}

// ---- 本地预检：保存前就能看出的结构问题（error 阻止保存，warning 只提示）----
// problemFamilies 为空（目录未加载）时不做目录比对，以免误报

function familyProblems(binding, families) {
  const name = problemFamily(binding).trim();
  if (!name) return [['problem_family', '未填写问题族，发布时会被拦截']];
  if (!families.length) return [];
  const family = findProblemFamily(families, name);
  if (!family) {
    const supported = families.map((entry) => entry.id).join('、');
    return [['problem_family', `问题族「${name}」不在平台注册目录中，发布时会被拦截；支持范围：${supported}`]];
  }
  const problems = [];
  if (family.id !== name) problems.push(['problem_family', `「${name}」是旧名称，建议改为 ${family.id}`]);
  const declared = binding?.declaration?.engine_requirements?.capabilities;
  if (declared !== undefined && !Array.isArray(declared)) {
    problems.push(['engine_capabilities', '引擎能力须是能力名列表，发布时会被拦截']);
  } else {
    const unsupported = engineCapabilities(binding).filter(
      (capability) => !(family.capabilities || []).includes(capability),
    );
    if (unsupported.length) {
      problems.push(['engine_capabilities', `问题族 ${family.id} 不支持能力：${unsupported.join('、')}，发布时会被拦截`]);
    }
  }
  return problems;
}

// 模板参数：空名、重名与超长在保存前就拦下，免得生成一份发布时才被拦截的契约
function templateProblems(params) {
  const normalized = normalizeParams(params);
  const { display_mapping: mapping, title } = normalized;
  const labels = ROUTE_OPTIONS.map((option) => mapping[option]);
  const problems = [];
  if (labels.some((label) => !label)) problems.push(['display_mapping', '填写两个选项的显示名称']);
  else if (new Set(labels).size !== labels.length) problems.push(['display_mapping', '两个选项的显示名称相同，结果无法区分']);
  else if (labels.some((label) => charLength(label) > MAX_TEMPLATE_TEXT || hasControlChar(label))) {
    problems.push(['display_mapping', `显示名称不能超过 ${MAX_TEMPLATE_TEXT} 个字符，也不能含换行等控制字符`]);
  }
  if (charLength(title) > MAX_TEMPLATE_TEXT || hasControlChar(title)) {
    problems.push(['title', `标题不能超过 ${MAX_TEMPLATE_TEXT} 个字符，也不能含换行等控制字符`]);
  }
  // 口径同后端 _check_text_presentation：只填一项时 Consumer 会拒收整个求解结果
  const names = TEXT_PRESENTATION_FIELDS.map((name) => normalized[name]);
  if (names.some(Boolean) && !names.every(Boolean)) {
    problems.push(['text_presentation', '结果文字的工艺名称与物流名称须同时填写，或都留空']);
  } else if (names.some((name) => charLength(name) > MAX_TEMPLATE_TEXT || hasControlChar(name))) {
    problems.push(['text_presentation', `结果文字的名称不能超过 ${MAX_TEMPLATE_TEXT} 个字符，也不能含换行等控制字符`]);
  }
  return problems;
}

export function localProblems(items, availableOperations = [], problemFamilies = []) {
  const problems = [];
  const add = (item, field, message, severity = 'error') =>
    problems.push({ uid: item.uid, field, message, severity });
  const stems = new Set(availableOperations);
  const keyCount = new Map();
  const operationCount = new Map();
  for (const item of items) {
    const { solver_key: key = '', operation = '' } = normalizeBinding(item.binding);
    keyCount.set(key, (keyCount.get(key) || 0) + 1);
    operationCount.set(operation, (operationCount.get(operation) || 0) + 1);
  }
  for (const item of items) {
    const binding = normalizeBinding(item.binding);
    const key = binding.solver_key || '';
    const operation = binding.operation || '';
    if (!SOLVER_KEY_PATTERN.test(key)) {
      add(item, 'solver_key', '求解标识须以小写字母开头，只含小写字母、数字、下划线，最长 32 位');
    } else if (keyCount.get(key) > 1) {
      add(item, 'solver_key', `求解标识「${key}」重复`);
    } else if (key !== operation && stems.has(key)) {
      add(item, 'solver_key', `求解标识「${key}」与另一个 Operation 同名，导出后会互相覆盖`);
    }
    if (!operation) add(item, 'operation', '未选择绑定的 Operation');
    else if (!stems.has(operation)) add(item, 'operation', `Operation「${operation}」不存在`);
    else if (operationCount.get(operation) > 1) add(item, 'operation', `Operation「${operation}」被多个求解契约绑定`);
    if (!binding.decision_model) add(item, 'decision_model', '未填写 Decision Model ID');
    for (const [field, message] of familyProblems(binding, problemFamilies)) add(item, field, message, 'warning');
    const units = Array.isArray(binding.units) ? binding.units : [];
    const fields = new Set();
    for (const row of units) {
      if (!row.field || !row.unit) add(item, 'units', '单位行须同时填写字段与单位');
      else if (fields.has(row.field)) add(item, 'units', `字段「${row.field}」的单位重复`);
      fields.add(row.field);
    }
    if (item.managed) {
      for (const [field, message] of templateProblems(item.params)) add(item, field, message);
    } else {
      for (const tab of SIDECAR_TABS) {
        if (!String(item.sidecars?.[tab.role] ?? '').trim()) add(item, tab.role, `${tab.label} 内容为空`);
      }
    }
    if (item.advancedError) add(item, ADVANCED, item.advancedError);
  }
  return problems;
}

// ---- 差异：保存确认、停用预览与冲突摘要共用 ----

// 模板参数各自落在哪份文件（口径同后端 render_template）
const PARAM_FILES = {
  display_mapping: 'model.yaml',
  evaluator_path: 'model.yaml',
  title: 'presentation.yaml',
  ...Object.fromEntries(TEXT_PRESENTATION_FIELDS.map((name) => [name, 'model.yaml'])),
};

function changedParts(base, item) {
  const parts = [];
  const before = { ...base.binding };
  const after = normalizeBinding(item.binding);
  if (stable(before) !== stable(after)) parts.push(BINDING_FILE);
  if (item.managed && base.managed === true) {
    const was = normalizeParams(base.template_params);
    const now = normalizeParams(item.params);
    const files = new Set(
      Object.keys(PARAM_FILES).filter((name) => stable(was[name]) !== stable(now[name])).map((name) => PARAM_FILES[name]),
    );
    return [...parts, ...SIDECAR_TABS.filter((tab) => files.has(tab.file)).map((tab) => tab.file)];
  }
  // 手动编辑过的契约按模板重新生成：五份附属文件整体覆盖
  if (item.managed) return [...parts, ...SIDECAR_TABS.map((tab) => tab.file)];
  for (const tab of SIDECAR_TABS) {
    if ((base.sidecars?.[tab.role] ?? '') !== (item.sidecars?.[tab.role] ?? '')) parts.push(tab.file);
  }
  return parts;
}

// baseOperations：服务端契约（view.contract.operations）；items：草稿
export function diffContracts(baseOperations = [], items = []) {
  const byKey = new Map(baseOperations.map((operation) => [operation.solver_key, operation]));
  const kept = new Set();
  const diff = { added: [], removed: [], changed: [], enabled: [], disabled: [] };
  for (const item of items) {
    const key = String(item.binding.solver_key || '').trim();
    const base = item.origin != null ? byKey.get(item.origin) : undefined;
    const nowEnabled = isEnabled(item.binding);
    if (!base) {
      diff.added.push({ uid: item.uid, solverKey: key, operation: item.binding.operation, enabled: nowEnabled });
      continue;
    }
    kept.add(base.solver_key);
    const parts = changedParts(base, item);
    if (parts.length) {
      diff.changed.push({
        uid: item.uid,
        solverKey: key,
        renamedFrom: base.solver_key !== key ? base.solver_key : '',
        files: parts,
      });
    }
    const wasEnabled = isEnabled(base.binding);
    if (wasEnabled && !nowEnabled) diff.disabled.push(key);
    if (!wasEnabled && nowEnabled) diff.enabled.push(key);
  }
  for (const operation of baseOperations) {
    if (!kept.has(operation.solver_key)) {
      diff.removed.push({
        solverKey: operation.solver_key,
        operation: operation.operation,
        enabled: isEnabled(operation.binding),
      });
    }
  }
  diff.dirty = Boolean(diff.added.length || diff.removed.length || diff.changed.length);
  diff.becomesSolverPackage = baseOperations.length === 0 && items.length > 0;
  diff.revertsToPlain = baseOperations.length > 0 && items.length === 0;
  return diff;
}

// 停用预览：哪些原本对外提供的求解能力会在发布后消失
export function disablePreview(diff) {
  return [
    ...diff.disabled.map((solverKey) => ({ solverKey, reason: 'disabled' })),
    ...diff.removed.filter((item) => item.enabled).map((item) => ({ solverKey: item.solverKey, reason: 'removed' })),
  ];
}

// 有问题但没能加载成契约的目录（binding 无法解析等）：整体保存会移除它们，需在确认时提示
export function unloadedDirectories(view) {
  const loaded = new Set((view?.contract?.operations || []).map((operation) => operation.solver_key));
  const keys = new Set();
  for (const finding of view?.findings || []) {
    const path = String(finding.path || '');
    if (!path.startsWith(SOLVER_DIR)) continue;
    const key = path.slice(SOLVER_DIR.length).split('/')[0];
    if (key && !loaded.has(key)) keys.add(key);
  }
  return [...keys].sort();
}

// ---- 问题定位 ----

// 模板条目的附属文件不在面板里编辑：参数相关的问题定位到对应表单项
function templateField(field) {
  const name = String(field || '');
  if (name === 'template' || name === 'display_mapping' || name.startsWith('display_mapping.')) return 'display_mapping';
  if (name === 'title') return 'title';
  if (name === 'evaluator_path' || name.endsWith('.evaluator_path')) return 'evaluator_path';
  if (name === 'text_presentation' || TEXT_PRESENTATION_FIELDS.includes(name)) return 'text_presentation';
  return '';
}

export function locateFinding(finding, items) {
  const path = String(finding?.path || '');
  const indexed = String(finding?.field || '').match(/^operations\[(\d+)\]$/);
  if (indexed) {
    const item = items[Number(indexed[1])];
    return item ? { uid: item.uid } : null;
  }
  if (path === VALIDATIONS_FILE) return { validation: true };
  if (!path.startsWith(SOLVER_DIR)) return path ? { file: path } : null;
  const [key, filename] = path.slice(SOLVER_DIR.length).split('/');
  const item =
    items.find((candidate) => String(candidate.binding.solver_key || '').trim() === key) ||
    items.find((candidate) => candidate.origin === key);
  if (!item) return { file: path };
  const tab = SIDECAR_TABS.find((candidate) => candidate.file === filename);
  if (tab && item.managed && templateField(finding.field)) return { uid: item.uid, field: templateField(finding.field) };
  if (tab) return { uid: item.uid, role: tab.role };
  const field = BINDING_FIELDS[finding.field];
  if (field) return { uid: item.uid, field };
  return finding.field ? { uid: item.uid, role: ADVANCED } : { uid: item.uid };
}

// 问题的出处：文件 · 字段 · 错误码（DEV-FE-004：无需看后端日志即可定位）
export function findingSource(finding) {
  return [finding?.path, finding?.field, finding?.code].filter(Boolean).join(' · ');
}

// ---- 问题标记：本地预检与服务端问题按条目汇总，表单字段与附属文件页签据此标红 ----
// 表单字段只显示第一条；页签列出全部（一份 model.yaml 可能同时有几处问题）。
// 本地预检已标出的位置不再叠加服务端问题，两边说的是同一处，计数不重复

export function collectMarks(items, problems, findings) {
  const marks = new Map(items.map((item) => [item.uid, { fields: new Map(), roles: new Map(), count: 0 }]));
  const local = new Set();
  const put = (uid, kind, keyName, value) => {
    const entry = marks.get(uid);
    if (!entry) return;
    entry.count += 1;
    if (!keyName) return;
    if (kind === 'roles') entry.roles.set(keyName, [...(entry.roles.get(keyName) || []), value]);
    else if (!entry.fields.has(keyName)) entry.fields.set(keyName, value);
  };
  for (const problem of problems) {
    const isRole = problem.field === ADVANCED || SIDECAR_TABS.some((tab) => tab.role === problem.field);
    const kind = isRole ? 'roles' : 'fields';
    put(problem.uid, kind, problem.field, problem);
    local.add(`${problem.uid}|${kind}|${problem.field}`);
  }
  for (const finding of findings) {
    const target = locateFinding(finding, items);
    if (!target?.uid) continue;
    const [kind, keyName] = target.role ? ['roles', target.role] : ['fields', target.field];
    if (!local.has(`${target.uid}|${kind}|${keyName}`)) put(target.uid, kind, keyName, finding);
  }
  return marks;
}

// ---- 即时检查：草稿经后端 check 接口按保存与发布的同一套规则检查（不落盘）----
// live 为 { key, rejected, findings, rendered }，key 是发起检查时的 toPayload 序列化结果，
// rendered 按条目序号给出模板条目的生成结果（role → 原文），手动条目与参数不合法的条目为 null

export const draftKey = (items) => JSON.stringify(toPayload(items));

// 面板展示哪份结论：有草稿且已有即时检查结果时用它，否则用已保存修订的检查与最近一次保存被拒的问题
export function shownChecks({ dirty = false, live = null, rejected = [], view = null } = {}) {
  if (dirty && live) return { draft: true, rejected: live.rejected || [], findings: live.findings || [] };
  return { draft: false, rejected, findings: [...(view?.findings || []), ...(view?.gate || [])] };
}

// 即时检查确认当前草稿保存会被拒时阻止保存；结果还不是当前草稿的（检查中）不拦，以保存时的校验为准
export function liveSaveBlocker(live, key) {
  if (!live || live.key !== key || !live.rejected?.length) return '';
  return `保存会被拒绝：${live.rejected[0].message}`;
}

export const shortRevision = (revision) => (revision ? String(revision).replace(/^sha256:/, '').slice(0, 10) : '—');

// ---- 验证状态：面板与发布检查共用；入参是 view 或 package/check 的 solver 段 ----

export function validationState(status) {
  if (!status?.enabled) return 'none';
  const operations = status.operations || status.contract?.operations || [];
  if (!operations.some((operation) => operation.enabled)) return 'not_needed';
  return 'after_publish';
}

// ---- 评估器：助手按已保存的契约（ontology/solver/<solver_key>/）生成声明式评估器 ----

export const EVALUATOR_COMMAND = '/ontology-solver-evaluator';

// 让助手生成评估器：助手只读已保存的契约，且它的提交会改变修订（草稿再保存时会冲突），
// 所以契约未保存或面板有未保存的修改（dirty）时先保存（needsSave），保存成功后再发命令，
// 命令按保存后的求解标识。saveBlocker 是面板「保存」不可用的原因；blocker 为空表示可以发起
export function evaluatorCommandPlan(item, { dirty = false, saveBlocker = '' } = {}) {
  const needsSave = dirty || item?.origin == null;
  const solverKey = String((needsSave ? item?.binding?.solver_key : item?.origin) ?? '').trim();
  let blocker = '';
  if (!SOLVER_KEY_PATTERN.test(solverKey)) blocker = '求解标识不合法，先在高级设置中修正';
  else if (needsSave && saveBlocker) blocker = saveBlocker;
  return { command: `${EVALUATOR_COMMAND} ${solverKey}`, needsSave, blocker };
}
