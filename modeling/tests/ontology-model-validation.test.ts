import assert from 'node:assert/strict';
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
      message: '不支持的关系类型',
    },
    {
      code: 'RELATION_UNKNOWN_FIELD',
      path: 'changes.relations[1].extra',
      message: '未知字段',
    },
  ]);
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
