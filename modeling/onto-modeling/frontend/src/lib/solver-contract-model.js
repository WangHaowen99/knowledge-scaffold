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

export const VALIDATION_STATES = {
  none: { label: '未启用求解', color: 'default' },
  not_needed: { label: '无需验证', color: 'default' },
  required: { label: '待验证', color: 'warning' },
  running: { label: '验证中', color: 'processing' },
  passed: { label: '已通过', color: 'success' },
  stale: { label: '已过期', color: 'warning' },
  failed: { label: '未通过', color: 'error' },
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
  const record = status.validation;
  if (record && ['queued', 'running'].includes(record.status)) return 'running';
  if (record && record.status === 'passed' && record.authoritative === true) return 'passed';
  if ((status.gate || []).some((item) => item.code === 'solver_contract_validation_stale')) return 'stale';
  if (record && ['failed', 'passed'].includes(record.status)) return 'failed';
  return 'required';
}

// ---- 端到端验证用例：存于工作区状态（不进知识包），结构校验以后端 normalize_cases 为准 ----

export const isValidationActive = (record) => ['queued', 'running'].includes(record?.status);

export const casesText = (cases) => `${JSON.stringify(cases || [], null, 2)}\n`;

// 上限与后端 solver_validation_run.normalize_cases 一致
export const MAX_CASES = 20;
export const MAX_CASE_NAME = 80;
export const MAX_QUESTION = 4000;
export const MAX_EXPECTED_CANDIDATES = 100000;
const CASE_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_ARGUMENTS_BYTES = 64 * 1024;
const MAX_CASES_BYTES = 512 * 1024;

const byteLength = (text) => new TextEncoder().encode(text).length;

const enabledKeys = (operations) =>
  operations.filter((operation) => operation.enabled).map((operation) => operation.solver_key);

// 模板契约（Profile 生成的输入 Schema）的用例入参：计划月份、装置编号与按加工顺序排列的批次
export const BATCH_FIELDS = [
  { key: 'item_id', label: '批次号' },
  { key: 'crude_origin', label: '原油品种' },
  { key: 'quantity_wan_tons', label: '批次量（万吨）' },
  { key: 'process_days', label: '加工天数' },
];
export const MAX_BATCHES = 64;
const ROUTE_ARGUMENT_KEYS = ['plan_month', 'diesel_unit_no', 'items'];
const BATCH_KEYS = ['item_order', ...BATCH_FIELDS.map((field) => field.key)];
const NUMERIC_BATCH_KEYS = ['quantity_wan_tons', 'process_days'];
const PLAN_MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;

const withoutOrder = (row) =>
  Object.fromEntries(Object.entries(isObject(row) ? row : {}).filter(([key]) => key !== 'item_order'));

// 批次按 item_order 展示（缺省按数组顺序），表格里不出现 item_order
export function caseBatches(args) {
  const items = Array.isArray(args?.items) ? args.items : [];
  const order = (row, index) => (Number.isInteger(row?.item_order) ? row.item_order : index + 1);
  return items
    .map((row, index) => ({ row, index, order: order(row, index) }))
    .sort((a, b) => a.order - b.order || a.index - b.index)
    .map(({ row }) => withoutOrder(row));
}

// 表格行的顺序即加工顺序：item_order 按行重排为从 1 开始的连续编号
export function setCaseBatches(args, rows) {
  return {
    ...(isObject(args) ? args : {}),
    items: rows.map((row, index) => ({ item_order: index + 1, ...withoutOrder(row) })),
  };
}

// 补零编号（B01、B02…）：当前求解服务按批次号字典序排序，不补零时 B10 会排到 B2 之前
export function nextBatchId(rows = []) {
  const taken = new Set(rows.map((row) => String(row?.item_id ?? '').trim()));
  const id = (index) => `B${String(index).padStart(2, '0')}`;
  let index = rows.length + 1;
  while (taken.has(id(index))) index += 1;
  return id(index);
}

export function newBatch(rows = []) {
  return { item_id: nextBatchId(rows), crude_origin: '', quantity_wan_tons: '', process_days: '' };
}

// 模板用例入参的提示（不阻止保存，结构以求解服务的输入校验为准）
export function routeCaseHints(args, unitOptions = []) {
  const source = isObject(args) ? args : {};
  const hints = [];
  if (!PLAN_MONTH_PATTERN.test(String(source.plan_month ?? '').trim())) hints.push('计划月份须是 YYYY-MM 格式');
  const unit = String(source.diesel_unit_no ?? '').trim();
  if (!unit) hints.push('未选择装置编号');
  else if (unitOptions.length && !unitOptions.includes(unit)) {
    hints.push(`装置编号「${unit}」不在评估器支持的范围内：${unitOptions.join('、')}`);
  }
  const items = Array.isArray(source.items) ? source.items : [];
  if (!items.length) hints.push('至少添加一个批次');
  else if (items.length > MAX_BATCHES) hints.push(`批次最多 ${MAX_BATCHES} 个`);
  const rows = caseBatches(source);
  const value = (row, key) => String(row[key] ?? '').trim();
  const blank = rows.findIndex((row) => BATCH_FIELDS.some((field) => !value(row, field.key)));
  if (blank >= 0) hints.push(`第 ${blank + 1} 个批次有未填写的字段`);
  const notNumber = rows.findIndex((row) =>
    NUMERIC_BATCH_KEYS.some((key) => value(row, key) && !Number.isFinite(Number(value(row, key)))));
  if (notNumber >= 0) hints.push(`第 ${notNumber + 1} 个批次的批次量和加工天数须是数字`);
  const ids = rows.map((row) => value(row, 'item_id')).filter(Boolean);
  const sorted = [...ids].sort();
  const duplicate = ids.find((id, index) => ids.indexOf(id) !== index);
  if (duplicate) hints.push(`批次号「${duplicate}」重复`);
  else if (ids.some((id, index) => id !== sorted[index])) {
    hints.push('批次号的字典序与加工顺序不一致，求解服务会按错误顺序处理，建议用补零编号 B01、B02…');
  }
  const orders = items.map((row) => row?.item_order).sort((a, b) => a - b);
  if (orders.some((order, index) => order !== index + 1)) hints.push('item_order 须从 1 开始连续编号');
  const unknown = new Set([
    ...Object.keys(source).filter((key) => !ROUTE_ARGUMENT_KEYS.includes(key)),
    ...items.flatMap((row) => Object.keys(isObject(row) ? row : {}).filter((key) => !BATCH_KEYS.includes(key))),
  ]);
  if (unknown.size) hints.push(`输入不允许额外字段：${[...unknown].join('、')}`);
  return hints;
}

// 一条新用例：id 取 <solver_key>_<序号>；模板契约预置一个批次，装置只有一个可选时直接填上
export function newCase(operation, cases = []) {
  const ids = new Set(cases.map((item) => item?.id));
  let index = 1;
  while (ids.has(`${operation.solver_key}_${index}`)) index += 1;
  const name = `${operation.operation || operation.solver_key} 示例${index > 1 ? ` ${index}` : ''}`;
  const units = operation.unit_options || [];
  const args = operation.managed
    ? setCaseBatches({ plan_month: '', diesel_unit_no: units.length === 1 ? units[0] : '' }, [newBatch([])])
    : {};
  return {
    id: `${operation.solver_key}_${index}`,
    name: [...name].slice(0, MAX_CASE_NAME).join(''),
    solver_key: operation.solver_key,
    arguments: args,
  };
}

// 给还没有用例的启用能力各补一条
export function sampleCases(operations = [], cases = []) {
  const covered = new Set(cases.map((item) => item?.solver_key));
  const next = [...cases];
  for (const operation of operations) {
    if (operation.enabled && !covered.has(operation.solver_key)) next.push(newCase(operation, next));
  }
  return next;
}

// 结构问题（与后端 normalize_cases 同口径，返回第一条；空串表示没有）
function caseError(cases) {
  if (cases.length > MAX_CASES) return `验证用例最多 ${MAX_CASES} 条`;
  const ids = new Set();
  for (const [index, item] of cases.entries()) {
    const label = `第 ${index + 1} 条用例`;
    if (
      !isObject(item) || !String(item.id ?? '').trim() || !isObject(item.arguments)
      || !SOLVER_KEY_PATTERN.test(String(item.solver_key ?? ''))
    ) {
      return `${label}须包含 id、合法的 solver_key 与对象类型的 arguments`;
    }
    const id = String(item.id).trim();
    if (!CASE_ID_PATTERN.test(id)) return `${label}的 id 只能由字母、数字、下划线和短横线组成，最长 64 位`;
    if (ids.has(id)) return `用例 id 重复：${id}`;
    ids.add(id);
    if (charLength(String(item.name ?? '').trim()) > MAX_CASE_NAME) return `${label}的名称超过 ${MAX_CASE_NAME} 个字符`;
    if (item.question != null && typeof item.question !== 'string') return `${label}的 question 须是文本`;
    if (charLength(String(item.question ?? '').trim()) > MAX_QUESTION) return `${label}的提问超过 ${MAX_QUESTION} 个字符`;
    if (byteLength(JSON.stringify(item.arguments)) > MAX_ARGUMENTS_BYTES) return `${label}的 arguments 超过 64 KB`;
    const expected = item.expected_candidate_count;
    if (expected != null && !(Number.isInteger(expected) && expected >= 0 && expected <= MAX_EXPECTED_CANDIDATES)) {
      return `${label}的预期候选数须是 0～${MAX_EXPECTED_CANDIDATES} 的整数`;
    }
  }
  if (byteLength(JSON.stringify(cases)) > MAX_CASES_BYTES) return '验证用例合计不能超过 512 KB';
  return '';
}

// 编辑器文本 → 用例：error 阻止保存；uncovered / unknown 是发起验证前就能看出的覆盖问题
export function parseCasesText(text, operations = []) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return { error: `用例不是合法 JSON：${error.message}` };
  }
  if (!Array.isArray(parsed)) return { error: '用例须是 JSON 数组' };
  const error = caseError(parsed);
  if (error) return { error };
  const keys = enabledKeys(operations);
  const covered = new Set(parsed.map((item) => item.solver_key));
  return {
    cases: parsed,
    uncovered: keys.filter((key) => !covered.has(key)),
    unknown: [...covered].filter((key) => !keys.includes(key)),
  };
}

// 发起验证前的阻断原因；空串表示可以发起
export function startBlocker(view, { dirty = false, casesDirty = false, cases = [] } = {}) {
  if (validationState(view) === 'running' || isValidationActive(view?.validation)) return '当前修订的验证进行中';
  if (dirty) return '求解契约有未保存的修改，先保存再验证';
  if (casesDirty) return '验证用例有未保存的修改，先保存用例';
  if ((view?.findings || []).length) return '先修正上方的求解契约检查问题';
  if (!cases.length) return '先添加并保存验证用例';
  const uncovered = parseCasesText(JSON.stringify(cases), view?.contract?.operations || []).uncovered || [];
  if (uncovered.length) return `求解能力 ${uncovered.join('、')} 还没有验证用例`;
  return '';
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

// 验证可选的数据源：验证不导入数据。带导入的 Neo4j 连接直接读图中已有数据（后端先核对
// 隔离范围非空）；dataset 要先建评测库，暂不支持，列出但置灰
export function validationSourceOptions(sources = []) {
  const label = (source) => {
    if (source.mode === 'dataset') return `${source.name}（数据集需导入评测库，暂不支持）`;
    const importsGraph = source.mode === 'connection' && source.provider === 'neo4j'
      && Boolean(source.files?.length || source.include_mock);
    return importsGraph ? `${source.name}（使用图中已有数据，不导入）` : source.name;
  };
  return [
    { value: '', label: '不使用数据源' },
    ...sources
      .filter((source) => source.mode !== 'none')
      .map((source) => ({
        value: source.id,
        label: label(source),
        disabled: source.mode === 'dataset',
      })),
  ];
}

// 验证记录证据里的逐条用例结论：面板列表展示用
export function validationCaseRows(record) {
  return (record?.evidence?.cases || []).map((item) => {
    const failed = (item.assertions || []).find((assertion) => !assertion.passed);
    return {
      id: item.id,
      name: item.name || item.id,
      solverKey: item.solver_key,
      passed: item.passed === true,
      authoritative: item.authoritative === true,
      feasibility: item.feasibility || '',
      candidateCount: item.candidate_count ?? null,
      expected: item.expected_candidate_count ?? null,
      code: item.code || item.candidate_results_error || '',
      failedAssertion: failed?.name || '',
      // Agent 链路：null 表示没走到这一步（如 Agent 运行本身失败），不按未调用展示
      toolCalled: typeof item.solver_tool_called === 'boolean' ? item.solver_tool_called : null,
      argumentsMatch: typeof item.arguments_match === 'boolean' ? item.arguments_match : null,
      argumentDiff: Array.isArray(item.argument_diff) ? item.argument_diff : [],
      question: item.question || '',
      detail: item.detail || '',
    };
  });
}
