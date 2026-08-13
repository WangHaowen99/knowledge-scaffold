import assert from 'node:assert/strict';
import { test } from 'node:test';
import { strToU8, zipSync } from 'fflate';

import { parsePackageArchive } from '../src/workflow/package-import.js';
import { zipPackage } from '../src/workflow/export.js';
import { renderPackage } from '../src/workflow/package-render.js';
import type { PackageSpec } from '../src/types.js';

function demoSpec(): PackageSpec {
  return {
    id: 'import-demo',
    name: '导入演示知识包',
    description: '用于验证知识包工作区导入',
    version: '1.2.0',
    language: 'zh-CN',
    status: 'review',
    defaultProviderAlias: 'demo-provider',
    objects: [{
      key: 'Customer',
      title: '客户',
      description: '客户主数据',
      sourceRefs: [{ kind: 'table', table: 'customer_table' }],
      primaryKey: ['customer_id'],
      fields: [
        { key: 'customer_id', cnName: '客户编号', type: 'string', required: true },
        { key: 'name', cnName: '客户名称', type: 'string' },
      ],
    }],
    rules: [{
      key: 'active_customer', title: '有效客户', description: '客户必须有效',
      content: '客户状态必须为有效。', appliesToObjects: ['Customer'],
    }],
    metrics: [{
      key: 'customer_count', title: '客户数', description: '有效客户数量',
      caliber: '对有效客户去重计数。', boundaries: ['只统计有效客户'], usesObjects: ['Customer'],
    }],
    operations: [{
      key: 'find_customer', title: '查询客户', description: '根据编号查询客户',
      inputs: [{ param: 'customer_id', cnName: '客户编号', type: 'string', required: true }],
      outputs: [{ field: 'name', cnName: '客户名称', type: 'string' }],
      flow: ['按客户编号筛选客户。'], relatedObjects: ['Customer'],
      usesRules: ['active_customer'], usesMetrics: ['customer_count'],
      competencyQuestion: '客户是谁？',
      script: { key: 'find_customer_script', required: true },
    }],
    scripts: [{
      key: 'find_customer_script', title: '查询客户脚本', description: '按标识查询客户。',
      boundOperation: 'find_customer', runtime: 'python', entrypoint: 'find_customer.py', timeoutSeconds: 30,
      parameters: [
        { name: 'customer_id', flag: '--customer-id', type: 'string', required: true },
        { name: 'mode', flag: '--mode', type: 'string', required: false, defaultValue: 'standard' },
      ],
      outputs: [{ key: 'name', type: 'string' }], boundaries: ['只读取已授权数据。'],
      sourceCode: [
        'import argparse',
        'import json',
        'parser = argparse.ArgumentParser()',
        'parser.add_argument("--customer-id", required=True)',
        'parser.add_argument("--mode", default="standard")',
        'args = parser.parse_args()',
        'print(json.dumps({"name": args.customer_id, "mode": args.mode}))',
      ].join('\n'),
    }],
  };
}

test('标准知识包 ZIP 可导入为统一规格和文件快照', () => {
  const spec = demoSpec();
  const files = renderPackage(spec);
  const imported = parsePackageArchive(zipPackage(files, 'import-demo'));

  assert.equal(imported.spec.id, spec.id);
  assert.equal(imported.spec.version, spec.version);
  assert.equal(imported.spec.defaultProviderAlias, spec.defaultProviderAlias);
  assert.deepEqual(imported.spec.objects.map((object) => ({
    key: object.key,
    title: object.title,
    sourceRefs: object.sourceRefs,
    primaryKey: object.primaryKey,
    fields: object.fields.map((field) => [field.key, field.cnName, field.type, Boolean(field.required)]),
  })), [{
    key: 'Customer',
    title: '客户',
    sourceRefs: [{ kind: 'table', table: 'customer_table' }],
    primaryKey: ['customer_id'],
    fields: [['customer_id', '客户编号', 'string', true], ['name', '客户名称', 'string', false]],
  }]);
  assert.deepEqual(imported.spec.rules?.map((rule) => rule.key), ['active_customer']);
  assert.deepEqual(imported.spec.metrics?.map((metric) => metric.key), ['customer_count']);
  assert.equal(imported.spec.scripts?.[0].parameters[1].defaultValue, 'standard');
  assert.deepEqual(imported.spec.operations.map((operation) => ({
    key: operation.key,
    relatedObjects: operation.relatedObjects,
    usesRules: operation.usesRules,
    usesMetrics: operation.usesMetrics,
    competencyQuestion: operation.competencyQuestion,
  })), [{
    key: 'find_customer',
    relatedObjects: ['Customer'],
    usesRules: ['active_customer'],
    usesMetrics: ['customer_count'],
    competencyQuestion: '客户是谁？',
  }]);
  assert.deepEqual(imported.coverage, {
    covered: [{ question: '客户是谁？', operation: 'find_customer' }],
    uncovered: [],
  });
  assert.equal(imported.files['manifest.yaml'], files['manifest.yaml']);
  assert.equal(imported.files['objects/Customer.md'], files['objects/Customer.md']);
});

test('ZIP 中带结构化规格时优先使用规格且保留包文件', () => {
  const spec = demoSpec();
  const rendered = renderPackage(spec);
  const entries = Object.fromEntries(Object.entries(rendered).map(([path, content]) => (
    [`pkg/${path}`, strToU8(content)]
  )));
  entries['pkg/model/package-spec.json'] = strToU8(JSON.stringify(spec));
  entries['pkg/model/coverage.json'] = strToU8(JSON.stringify({ covered: [], uncovered: ['待补问题'] }));

  const imported = parsePackageArchive(zipSync(entries));

  assert.deepEqual(imported.spec, spec);
  assert.deepEqual(imported.coverage, { covered: [], uncovered: ['待补问题'] });
  assert.ok(!Object.hasOwn(imported.files, 'model/package-spec.json'));
});

test('ZIP 导入保留术语 binding 和字段引用', () => {
  const spec = demoSpec();
  spec.valueBindings = [{
    key: 'airport-code',
    resource: '机场中文代号',
    valueField: 'ARP_CD',
    semanticField: '机场名',
  }];
  spec.objects[0].fields[0].valueBindingRef = 'airport-code';
  const files = renderPackage(spec);

  const imported = parsePackageArchive(zipPackage(files, 'import-demo'));

  assert.deepEqual(imported.spec.valueBindings, spec.valueBindings);
  assert.equal(imported.spec.objects[0].fields[0].valueBindingRef, 'airport-code');
});

test('ZIP 中的二进制附件按原始字节保留且不作为文本解析', () => {
  const spec = demoSpec();
  const rendered = renderPackage(spec);
  const binary = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0xff]);
  const entries = Object.fromEntries(Object.entries(rendered).map(([path, content]) => (
    [`pkg/${path}`, strToU8(content)]
  )));
  entries['pkg/assets/logo.png'] = binary;

  const imported = parsePackageArchive(zipSync(entries));

  assert.equal(Object.hasOwn(imported.files, 'assets/logo.png'), false);
  assert.deepEqual(imported.rawFiles['assets/logo.png'], binary);
  assert.ok(imported.warnings.some((warning) => warning.includes('二进制附件')));
});

test('ZIP 导入拒绝路径穿越和缺少 manifest 的压缩包', () => {
  assert.throws(
    () => parsePackageArchive(zipSync({ '../escape.txt': strToU8('bad') })),
    /路径不安全/,
  );
  assert.throws(
    () => parsePackageArchive(zipSync({ 'pkg/index.md': strToU8('# empty') })),
    /manifest\.yaml/,
  );
});

test('ZIP 导入在解压前拒绝危险盘符、规范化重复路径和超大展开体积', () => {
  assert.throws(
    () => parsePackageArchive(zipSync({ 'C:/pkg/manifest.yaml': strToU8('name: bad') })),
    /路径不安全/,
  );
  assert.throws(
    () => parsePackageArchive(zipSync({
      'pkg/manifest.yaml': strToU8('name: demo'),
      'pkg\\manifest.yaml': strToU8('name: duplicate'),
    })),
    /重复文件/,
  );

  const oversized = new Uint8Array((64 * 1024 * 1024) + 1);
  assert.throws(
    () => parsePackageArchive(zipSync({
      'pkg/manifest.yaml': strToU8('name: demo'),
      'pkg/assets/oversized.bin': oversized,
    }, { level: 9 })),
    /解压后超过 64 MB/,
  );
});
