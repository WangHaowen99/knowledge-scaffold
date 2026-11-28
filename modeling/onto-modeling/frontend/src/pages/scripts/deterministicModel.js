const EMPTY_SCHEMA = JSON.stringify({
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  properties: {},
  additionalProperties: false,
}, null, 2);

export const DATA_ACCESS_OPTIONS = [
  { value: 'none', label: '仅使用输入参数' },
  { value: 'workspace_provider', label: '读取工作空间数据' },
];

const DEFAULT_TEST_VECTORS = [{
  name: '正常请求',
  text: '请执行这个确定性操作',
  known_arguments: {},
  expected_matched: true,
  expected_arguments: {},
  expected_issues: [],
  expected_output: {},
  provider_fixtures: [],
}];

export function newContract(view = {}, index = 0) {
  const used = new Set((view.operations || []).map((item) => item.operation));
  const operation = (view.available_operations || []).find((name) => !used.has(name)) || '';
  const handler = (view.available_handlers || []).find((name) => name === operation)
    || (view.available_handlers || [])[0] || '';
  const stem = String(operation || `operation_${index + 1}`).replace(/[^a-zA-Z0-9_]+/g, '_').toLowerCase();
  const key = /^[a-z]/.test(stem) ? stem.slice(0, 64) : `operation_${index + 1}`;
  return {
    key, operation, handler, data_access: 'none', enabled: true,
    aliases: [], routing: { all_terms: [], any_terms: [] },
    parser_source: [
      'import argparse',
      'import json',
      '',
      'parser = argparse.ArgumentParser()',
      "parser.add_argument('--request-json', required=True)",
      'args = parser.parse_args()',
      'request = json.loads(args.request_json)',
      "known = dict(request.get('known_arguments') or {})",
      "print(json.dumps({'matched': True, 'arguments': known, 'issues': []}, ensure_ascii=False))",
      '',
    ].join('\n'),
    input_schema: `${EMPTY_SCHEMA}\n`, output_schema: `${EMPTY_SCHEMA}\n`,
    test_vectors: DEFAULT_TEST_VECTORS,
    test_vectors_source: JSON.stringify(DEFAULT_TEST_VECTORS, null, 2),
    verification: { status: 'missing', current: false },
  };
}

export const normalizeContract = (item = {}) => ({
  key: String(item.key || ''), operation: String(item.operation || ''), handler: String(item.handler || ''),
  data_access: item.data_access === 'workspace_provider' ? 'workspace_provider' : 'none',
  enabled: item.enabled !== false,
  aliases: Array.isArray(item.aliases) ? item.aliases.map(String) : [],
  routing: {
    all_terms: Array.isArray(item.routing?.all_terms) ? item.routing.all_terms.map(String) : [],
    any_terms: Array.isArray(item.routing?.any_terms) ? item.routing.any_terms.map(String) : [],
  },
  parser_source: String(item.parser_source || ''), input_schema: String(item.input_schema || ''),
  output_schema: String(item.output_schema || ''),
  test_vectors: Array.isArray(item.test_vectors) ? item.test_vectors : [],
  test_vectors_source: typeof item.test_vectors_source === 'string'
    ? item.test_vectors_source : JSON.stringify(Array.isArray(item.test_vectors) ? item.test_vectors : [], null, 2),
  verification: item.verification && typeof item.verification === 'object'
    ? item.verification : { status: 'missing', current: false },
});

export const draftOf = (view) => (view?.operations || []).map(normalizeContract);
export const payloadOf = (items) => items.map((item) => {
  const normalized = normalizeContract(item);
  if (typeof item.aliases_source === 'string') normalized.aliases = splitTerms(item.aliases_source);
  if (typeof item.any_terms_source === 'string') normalized.routing.any_terms = splitTerms(item.any_terms_source);
  if (typeof item.all_terms_source === 'string') normalized.routing.all_terms = splitTerms(item.all_terms_source);
  delete normalized.verification;
  normalized.test_vectors = normalized.test_vectors_source;
  delete normalized.test_vectors_source;
  delete normalized.test_vectors_error;
  return normalized;
});
export const hasDraftErrors = (items) => (items || []).some((item) => Boolean(item.test_vectors_error));
const nonFiniteNumberPath = (value, path = '$') => {
  if (typeof value === 'number') return Number.isFinite(value) ? '' : path;
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const found = nonFiniteNumberPath(value[index], `${path}[${index}]`);
      if (found) return found;
    }
  } else if (value && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      const found = nonFiniteNumberPath(child, `${path}.${key}`);
      if (found) return found;
    }
  }
  return '';
};
export const testVectorsPatch = (source) => {
  try {
    const value = JSON.parse(source);
    if (!Array.isArray(value)) {
      return {
        test_vectors_source: source,
        test_vectors_error: '测试向量根节点必须是 JSON 数组',
      };
    }
    const invalidNumber = nonFiniteNumberPath(value);
    if (invalidNumber) {
      return {
        test_vectors_source: source,
        test_vectors_error: `测试向量 ${invalidNumber} 包含超出有限范围的数值`,
      };
    }
    return { test_vectors_source: source, test_vectors_error: '' };
  } catch (_error) {
    return { test_vectors_source: source, test_vectors_error: '测试向量必须是有效 JSON' };
  }
};
export const responseMode = (requestEditVersion, currentEditVersion) => (
  requestEditVersion === currentEditVersion ? 'replace' : 'preserve'
);
export function selectionAfterRefresh(items, selected, nextItems) {
  const selectedKey = items[selected]?.key;
  const index = selectedKey ? nextItems.findIndex((item) => item.key === selectedKey) : -1;
  return index >= 0 ? index : Math.min(selected, Math.max(0, nextItems.length - 1));
}
export const draftKey = (items) => JSON.stringify({
  operations: payloadOf(items),
  terms: items.map((item) => ['aliases', 'any_terms', 'all_terms'].map((field) => termSource(item, field))),
});
export const isDirty = (view, items) => draftKey(draftOf(view)) !== draftKey(items);
export const splitTerms = (text) => String(text || '').split(/[\n,，]/).map((value) => value.trim()).filter(Boolean);
export const joinTerms = (values) => (values || []).join('\n');
export const termSource = (item, field) => item[`${field}_source`]
  ?? joinTerms(field === 'aliases' ? item.aliases : item.routing?.[field]);
export const findingText = (findings = []) => findings.map((item) => item.message).filter(Boolean);

export const verificationText = (verification = {}) => {
  if (verification.status === 'passed' && verification.current) return '执行闭包验证通过';
  if (verification.status === 'failed' && verification.current) return '执行闭包验证未通过';
  if (verification.status !== 'missing' && !verification.current) return '执行依赖已变化，需要重新验证';
  return '尚未验证执行闭包';
};

export function executionNextStep({ dirty, newer, checking, findings, verification = {}, enabled }) {
  if (newer) return '已有更新，请刷新并确认最新内容后再保存。';
  if (!enabled) return '当前操作未启用，不会随知识包发布。';
  if (checking) return '正在检查业务草稿，请稍候。';
  if (findings) return '草稿还有问题，请让助手完善；无需自行编写代码。';
  if (dirty) return '请确认下方业务示例，保存后运行真实沙箱验证。当前改动尚未验证。';
  if (verification.status === 'passed' && verification.current) return '沙箱验证已通过。下一步到发布页面检查并发布知识包，再到消费端选择该版本试用。';
  if (verification.status === 'failed' && verification.current) return '验证未通过，请让助手根据失败原因修复，再读取新版草稿、保存和验证。';
  if (verification.status !== 'missing' && !verification.current) return '业务规则或实现已变化，旧验证结果已失效，请重新运行沙箱验证。';
  return '草稿已保存，请运行真实沙箱验证；保存成功不等于执行验证通过。';
}

export function businessExamples(item = {}) {
  let vectors;
  try { vectors = JSON.parse(testVectorsSource(item)); } catch { return []; }
  if (!Array.isArray(vectors)) return [];
  let properties = {};
  try {
    const schema = typeof item.output_schema === 'string' ? JSON.parse(item.output_schema) : item.output_schema;
    properties = schema?.properties || {};
  } catch { /* Invalid schemas are reported by the contract check. */ }
  return vectors.filter((vector) => vector && typeof vector === 'object' && !Array.isArray(vector)).map((vector) => {
    const issues = Array.isArray(vector.expected_issues) ? vector.expected_issues.filter((issue) => issue && typeof issue === 'object') : [];
    const expected = vector.expected_matched === false ? '不执行此操作'
      : issues.length ? issues.map((issue) => issue.message || issue.field || '请补充或更正输入').join('；')
        : Object.entries(vector.expected_output || {}).map(([key, value]) => (
          `${properties[key]?.title || properties[key]?.description || key}：${typeof value === 'object' ? JSON.stringify(value) : String(value)}`
        )).join('；') || '未填写预期业务结果';
    const known = Object.entries(vector.known_arguments || {}).map(([key, value]) => `${key}：${JSON.stringify(value)}`).join('；');
    return { name: vector.name, text: vector.text, expected, known };
  });
}

const testVectorsSource = (item) => item.test_vectors_source ?? JSON.stringify(item.test_vectors || []);

export function assistantDraftCommand(item, findings = [], verification = {}) {
  return {
    command: '/ontology-deterministic-generate',
    args: {
      operation: item.operation, handler: item.handler, key: item.key,
      findings: findingText(findings),
      failed_cases: (verification.cases || []).filter((entry) => !entry.ok).map(({ name, reason }) => ({ name, reason })),
    },
    text: `请完善「${item.operation}」的确定性执行实现和业务示例，处理检查与验证失败原因。我只确认业务，不编写代码。完成后告诉我下一步。`,
  };
}
