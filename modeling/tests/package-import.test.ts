import assert from 'node:assert/strict';
import { test } from 'node:test';
import { strToU8, zipSync } from 'fflate';

import {
  decodeLegacyZipEntryName,
  parsePackageArchive,
  renameImportedPackageArchive,
} from '../src/workflow/package-import.js';
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
      appliesToOperations: ['find_customer'],
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
        {
          name: 'verbose', flag: '--verbose', type: 'boolean', required: false,
          action: 'store_true',
        },
        { name: 'labels', flag: '--label', type: 'string', required: false, multiple: true },
      ],
      outputs: [{ key: 'name', type: 'string' }], boundaries: ['只读取已授权数据。'],
      sourceCode: [
        'import argparse',
        'import json',
        'parser = argparse.ArgumentParser()',
        'parser.add_argument("--customer-id", required=True)',
        'parser.add_argument("--mode", default="standard")',
        'parser.add_argument("--verbose", action="store_true")',
        'parser.add_argument("--label", action="append")',
        'args = parser.parse_args()',
        'print(json.dumps({"name": args.customer_id, "mode": args.mode}))',
      ].join('\n'),
    }],
  };
}

function replaceZipEntryName(archive: Uint8Array, from: string, to: Uint8Array): Uint8Array {
  const source = strToU8(from);
  assert.equal(source.length, to.length);
  const result = new Uint8Array(archive);
  let replacements = 0;
  for (let offset = 0; offset <= result.length - source.length; offset += 1) {
    if (!source.every((byte, index) => result[offset + index] === byte)) continue;
    result.set(to, offset);
    replacements += 1;
    offset += source.length - 1;
  }
  assert.equal(replacements, 2);
  return result;
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
    sourceRefs: [{
      kind: 'table',
      table: 'customer_table',
      role: 'primary',
      columns: ['customer_id', 'name'],
      fieldBindings: [
        { fieldKey: 'customer_id', sourceColumn: 'customer_id' },
        { fieldKey: 'name', sourceColumn: 'name' },
      ],
    }],
    primaryKey: ['customer_id'],
    fields: [['customer_id', '客户编号', 'string', true], ['name', '客户名称', 'string', false]],
  }]);
  assert.deepEqual(imported.spec.rules?.map((rule) => ({
    key: rule.key,
    appliesToOperations: rule.appliesToOperations,
  })), [{ key: 'active_customer', appliesToOperations: ['find_customer'] }]);
  assert.deepEqual(imported.spec.metrics?.map((metric) => metric.key), ['customer_count']);
  assert.equal(imported.spec.scripts?.[0].parameters[1].defaultValue, 'standard');
  assert.equal(imported.spec.scripts?.[0].parameters[2].action, 'store_true');
  assert.equal(imported.spec.scripts?.[0].parameters[2].defaultValue, false);
  assert.equal(imported.spec.scripts?.[0].parameters[3].multiple, true);
  assert.match(files['scripts/find_customer_script.md'], /\| 是否多值 \| Action \|/u);
  assert.match(files['scripts/find_customer_script.md'], /\| store_true \|/u);
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

test('导入副本改名会同步更新规格、文本 manifest 和原始 manifest 字节', () => {
  const files = renderPackage(demoSpec());
  files['manifest.yaml'] = `# 保留来源注释\n${files['manifest.yaml']}`;
  const imported = parsePackageArchive(zipPackage(files, 'import-demo'));

  const renamed = renameImportedPackageArchive(imported, '导入演示知识包（2）');

  assert.equal(renamed.spec.name, '导入演示知识包（2）');
  assert.match(renamed.files['manifest.yaml'], /^# 保留来源注释$/mu);
  assert.match(renamed.files['manifest.yaml'], /^name: 导入演示知识包（2）$/mu);
  assert.equal(
    new TextDecoder().decode(renamed.rawFiles['manifest.yaml']),
    renamed.files['manifest.yaml'],
  );
  assert.match(renamed.warnings.at(-1) ?? '', /导入演示知识包（2）/u);
  assert.equal(imported.spec.name, '导入演示知识包');
  assert.doesNotMatch(imported.files['manifest.yaml'], /导入演示知识包（2）/u);
});

test('来源 ZIP 缺少 manifest status 时按 draft 兼容导入，但仍拒绝非法状态', () => {
  const files = renderPackage(demoSpec());
  const manifestWithoutStatus = files['manifest.yaml'].replace(/^status:.*\n?/mu, '');
  const compatibleFiles = { ...files, 'manifest.yaml': manifestWithoutStatus };

  const imported = parsePackageArchive(zipPackage(compatibleFiles, 'import-demo'));

  assert.equal(imported.spec.status, 'draft');
  assert.equal(imported.files['manifest.yaml'], manifestWithoutStatus);
  assert.equal(
    new TextDecoder().decode(imported.rawFiles['manifest.yaml']),
    manifestWithoutStatus,
  );
  assert.ok(imported.warnings.some((warning) => warning.includes('按 draft 兼容导入')));

  assert.throws(
    () => parsePackageArchive(zipPackage({
      ...files,
      'manifest.yaml': files['manifest.yaml'].replace(/^status:.*$/mu, 'status: unknown'),
    }, 'import-demo')),
    /manifest 的 status 不正确/u,
  );
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

  assert.deepEqual(imported.spec, {
    ...spec,
    objects: spec.objects.map((object) => ({
      ...object,
      fields: object.fields.map((field) => ({ ...field, required: field.required === true })),
    })),
  });
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

test('ZIP 未标 UTF-8 时兼容解码 GB18030 文件名', () => {
  const spec = demoSpec();
  const rendered = renderPackage(spec);
  const entries = Object.fromEntries(Object.entries(rendered).map(([path, content]) => (
    [`pkg/${path}`, strToU8(content)]
  )));
  entries['pkg/model/package-spec.json'] = strToU8(JSON.stringify(spec));
  entries['pkg/model/coverage.json'] = strToU8(JSON.stringify({ covered: [], uncovered: [] }));
  entries['pkg/objects/abcd.md'] = strToU8('# 合成条目');
  const gbkPath = Uint8Array.from([
    ...strToU8('pkg/objects/'),
    0xb2, 0xe2, 0xca, 0xd4,
    ...strToU8('.md'),
  ]);
  const archive = replaceZipEntryName(
    zipSync(entries),
    'pkg/objects/abcd.md',
    gbkPath,
  );

  const imported = parsePackageArchive(archive);

  assert.equal(decodeLegacyZipEntryName(String.fromCharCode(0xb2, 0xe2, 0xca, 0xd4)), '测试');
  assert.equal(imported.files['objects/测试.md'], '# 合成条目');
  assert.equal(Object.hasOwn(imported.files, 'objects/abcd.md'), false);
});

test('ZIP 文件名兼容不会误改无法可靠识别的单字节名称', () => {
  const legacyLatinName = `caf${String.fromCharCode(0xe9)}.md`;
  assert.equal(decodeLegacyZipEntryName(legacyLatinName), legacyLatinName);
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
