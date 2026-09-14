import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import type { PackageSpec } from '../src/types.js';
import {
  introducedSpecIntegrityProblems,
  normalizeObjectFieldsTable,
  normalizeObjectMetadataPatch,
  normalizeStudioRelations,
  validateOntologyRelationTargets,
} from '../src/workflow/ontology-model-validation.js';

function baseSpec(): PackageSpec {
  return {
    id: 'ontology-validation-test',
    name: '本体验证',
    description: '本体验证测试基线',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
}

test('关系归一化保留合法项并返回结构化字段错误', () => {
  const result = normalizeStudioRelations(
    'rule',
    [{
      kind: 'applies_to_object',
      targetId: 'object:inventory',
      label: '  适用对象  ',
      description: '  安全库存适用对象  ',
    }, {
      kind: 'uses_object',
      targetId: 'object:inventory',
      extra: true,
    }],
    'changes.relations',
    [{ kind: 'applies_to_object', targetKinds: ['object'] }],
  );

  assert.deepEqual(result.relations, [{
    kind: 'applies_to_object',
    targetKind: 'object',
    targetKey: 'inventory',
    label: '适用对象',
    description: '安全库存适用对象',
  }]);
  assert.deepEqual(result.issues.map((item) => ({
    code: item.code,
    path: item.path,
    message: item.message,
  })), [
    {
      code: 'RELATION_KIND_INVALID',
      path: 'changes.relations[1].kind',
      message: '不支持的关系类型 uses_object；kind 只接受 applies_to_object，'
        + '业务谓词（例如「归属于」）写进 label，不要写进 kind',
    },
    {
      code: 'RELATION_UNKNOWN_FIELD',
      path: 'changes.relations[1].extra',
      message: '未知字段；关系只接受 kind、targetId、label、description',
    },
  ]);
});

test('关系类型报错必须给出合法值和业务谓词该写哪里', () => {
  /* 同一件事在三个接口上叫法不同：推荐接口返回 relation: "belongs_to"，写入接口要
     kind + label。报错只说「不支持」的话，调用方只能靠猜——实测助手会反复试
     belongs_to、「归属」等值，原地打转数轮后整批任务放弃。 */
  const result = normalizeStudioRelations('object', [{
    kind: 'belongs_to',
    targetId: 'object:customer',
    label: '归属于',
  }], 'changes.relations', [{ kind: 'object_relation', targetKinds: ['object'] }]);
  const kindIssue = result.issues.find((item) => item.code === 'RELATION_KIND_INVALID');
  assert.ok(kindIssue, JSON.stringify(result.issues));
  // 合法值必须来自入参，不是手写的平行清单
  assert.match(kindIssue.message, /object_relation/);
  // 还要告诉调用方业务谓词放哪，否则它只会换一个词继续猜
  assert.match(kindIssue.message, /label/);
  assert.doesNotMatch(kindIssue.message, /^不支持的关系类型$/);
});

test('对象元数据 patch 会裁剪文本并拒绝未知字段', () => {
  const result = normalizeObjectMetadataPatch({
    title: '  客户  ',
    aliases: [' 主数据 ', '客户实体'],
    queryBoundary: '  只返回主数据  ',
    extra: true,
  }, 'metadata');

  assert.deepEqual(result.changes, {
    title: '客户',
    aliases: ['主数据', '客户实体'],
    queryBoundary: '只返回主数据',
  });
  assert.deepEqual(result.issues.map((item) => ({
    code: item.code,
    path: item.path,
    message: item.message,
  })), [{
    code: 'OBJECT_METADATA_UNKNOWN_FIELD',
    path: 'metadata.extra',
    message: '未知字段',
  }]);
});

test('对象字段表归一化会保留合法字段并拦截重复 key', () => {
  const result = normalizeObjectFieldsTable([
    {
      key: ' customer_id ',
      cnName: ' 客户ID ',
      type: ' string ',
      required: true,
      sourceColumn: ' CUSTOMER_ID ',
    },
    {
      key: 'customer_id',
      cnName: '重复字段',
    },
  ], 'field_ops.final_fields');

  assert.deepEqual(result.fields, [{
    key: 'customer_id',
    cnName: '客户ID',
    type: 'string',
    required: true,
    sourceColumn: 'CUSTOMER_ID',
  }]);
  assert.deepEqual(result.issues.map((item) => ({
    code: item.code,
    path: item.path,
    message: item.message,
  })), [{
    code: 'FIELD_KEY_DUPLICATE',
    path: 'field_ops.final_fields[1].key',
    message: '字段 key 重复',
  }]);
});

test('关系目标校验基于最终 spec 判断缺失引用', () => {
  const spec = baseSpec();
  spec.objects.push(
    { key: 'customer', title: '客户', description: '客户对象', fields: [] },
    { key: 'order', title: '订单', description: '订单对象', fields: [] },
  );

  const issues = validateOntologyRelationTargets(spec, [{
    path: 'changes.relations',
    relations: [
      { kind: 'object_relation', targetKind: 'object', targetKey: 'order', label: '关联' },
      { kind: 'object_relation', targetKind: 'object', targetKey: 'missing', label: '缺失对象' },
    ],
  }]);

  assert.deepEqual(issues.map((item) => ({
    code: item.code,
    path: item.path,
    message: item.message,
  })), [{
    code: 'RELATION_TARGET_NOT_FOUND',
    path: 'changes.relations[1].targetId',
    message: '关系目标不存在',
  }]);
});

test('完整性差量校验只返回本次新引入的问题', () => {
  const previous = baseSpec();
  previous.objects.push({
    key: 'customer',
    title: '客户',
    description: '客户对象',
    fields: [],
  });
  previous.rules = [{
    key: 'legacy-policy',
    title: '存量规则',
    description: '历史问题',
    content: '按存量口径执行',
    appliesToObjects: ['retired-customer'],
  }];

  const next = structuredClone(previous);
  next.objects.push({
    key: 'warehouse',
    title: '仓库',
    description: '仓库对象',
    fields: [],
    aliases: ['customer'],
  });

  const issues = introducedSpecIntegrityProblems(next, [
    '规则 legacy-policy 引用了不存在的对象 retired-customer',
  ]);

  assert.equal(issues.length, 1);
  assert.match(issues[0], /对象别名.*customer.*冲突/u);
  assert.equal(issues[0].includes('retired-customer'), false);
});

test('关系 kind 不暴露给模型：唯一合法值由服务端补，模型只决定业务谓词', () => {
  /* kind 只有 object_relation 一个合法值，create_object 那条路直接硬编码、不看传入值，
     patch_object 那条路服务端也会补默认值。把这样一个字段放进工具 schema，模型会把推荐
     接口返回的 belongs_to 抄进去，被拒后反复猜、原地打转数轮，最后整批任务放弃（见 #48）。 */
  const tools = readFileSync('modeling-agent/modeling_agent/tools.py', 'utf8');
  /* 不能只查 DOMAIN_RELATION：update_relation 走的是 DOMAIN_ACTION_CHANGES，
     那里也有一份 kind，只盯一个块会漏（实测就漏了，部署后助手照样撞上）。
     直接要求全文不再出现裸的 "kind": STRING。 */
  assert.doesNotMatch(tools, /"kind":\s*STRING/, '关系类型不应以裸字符串暴露给模型');
  const block = tools.slice(tools.indexOf('DOMAIN_RELATION = {'));
  const body = block.slice(0, block.indexOf('}\n') + 1);
  assert.match(body, /"target_id"/);
  assert.match(body, /业务谓词/, 'label 要说明它承载业务谓词，否则模型仍会找地方塞关系类型');

  // 服务端保持容忍：不传就补默认值，传了也不报错，老调用不受影响
  const mutation = readFileSync('src/agent/domain-resource-mutation.ts', 'utf8');
  assert.match(mutation, /kind: value\.kind === undefined \? 'object_relation'/);
});
