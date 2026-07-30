import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { DataDictTable, PackageSpec, SpecObject } from '../src/types.js';
import {
  assertObjectDataMappings,
  findObjectDataMappingProblems,
} from '../src/workflow/data-mapping.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { runFormatChecks } from '../src/workflow/package-check.js';
import {
  documentSourcesFor,
  sourceLabelsFor,
  sourceRefsFor,
  tableSourceFor,
  tableSourcesFor,
} from '../src/workflow/source-references.js';

function object(overrides: Partial<SpecObject> = {}): SpecObject {
  return {
    key: 'Customer',
    title: '企业客户',
    description: '企业银行对公客户',
    fields: [
      { key: 'id', cnName: '客户号', type: 'string', required: true, sourceColumn: 'CUST_ID' },
    ],
    primaryKey: ['id'],
    ...overrides,
  };
}

function spec(objects: SpecObject[]): PackageSpec {
  return {
    id: 'source-reference-demo',
    name: '企业对公营销',
    description: '对公客户营销知识包',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'business-data',
    objects,
    operations: [],
  };
}

function table(name = 'CUSTOMERS'): DataDictTable {
  return {
    name,
    rowCount: 1,
    fields: [{
      name: 'CUST_ID',
      inferredType: 'string',
      sampleValues: ['10001'],
      distinctRatio: 1,
    }],
  };
}

test('旧 sourceTable 仍能作为表格来源使用', () => {
  const legacy = object({ sourceTable: 'CUSTOMERS' });

  assert.deepEqual(sourceRefsFor(legacy), [{ kind: 'table', table: 'CUSTOMERS' }]);
  assert.deepEqual(tableSourceFor(legacy), { kind: 'table', table: 'CUSTOMERS' });
  assert.deepEqual(documentSourcesFor(legacy), []);
  assert.deepEqual(findObjectDataMappingProblems(spec([legacy]), [table()]), []);

  const files = renderPackage(spec([legacy]));
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);
  assert.ok(mappings.objects.Customer);
  assert.match(files['objects/Customer.md'], /表格：CUSTOMERS/);
});

test('文档来源能渲染，但不会生成数据 mapping', () => {
  const product = object({
    key: 'Product',
    title: '银行产品',
    description: '从产品手册提取的业务概念',
    sourceRefs: [{
      kind: 'document',
      documentId: 'doc-product-manual',
      filename: '企业银行对公产品手册.docx',
      anchors: [{ heading: '第五章 现金管理', offset: 1280 }],
    }],
  });

  assert.deepEqual(findObjectDataMappingProblems(spec([product]), []), []);
  assert.equal(tableSourceFor(product), undefined);
  assert.equal(documentSourcesFor(product).length, 1);

  const files = renderPackage(spec([product]));
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);
  assert.deepEqual(mappings.objects, {});
  assert.doesNotMatch(files['objects/Product.md'], /^mapping:/m);
  assert.match(
    files['objects/Product.md'],
    /文档：企业银行对公产品手册\.docx \/ 第五章 现金管理/,
  );
  assert.match(
    files['objects/index.md'],
    /文档：企业银行对公产品手册\.docx \/ 第五章 现金管理/,
  );
});

test('新格式表格来源会生成数据 mapping', () => {
  const customer = object({
    sourceRefs: [{
      kind: 'table',
      documentId: 'doc-customer-data',
      table: 'CUSTOMERS',
      columns: ['CUST_ID'],
    }],
  });

  assert.deepEqual(findObjectDataMappingProblems(spec([customer]), [table()]), []);
  const files = renderPackage(spec([customer]));
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);

  assert.equal(mappings.objects.Customer.resource, 'Customer');
  assert.deepEqual(mappings.objects.Customer.primary_key, ['id']);
  assert.match(files['objects/Customer.md'], /mapping:\n  provider_alias: business-data/);
  assert.match(files['objects/Customer.md'], /表格：CUSTOMERS/);
  assert.deepEqual(runFormatChecks(spec([customer]), files), []);
});

test('新格式表格来源在 NL2SQL 下使用真实 Sheet 作为 resource', () => {
  const customer = object({
    sourceRefs: [{ kind: 'table', table: 'CUSTOMERS', columns: ['CUST_ID'] }],
  });
  const packageSpec = { ...spec([customer]), dataSourceType: 'nl2sql' as const };
  const files = renderPackage(packageSpec);
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);

  assert.equal(mappings.objects.Customer.resource, 'CUSTOMERS');
  assert.deepEqual(runFormatChecks(packageSpec, files), []);
});

test('混合来源同时保留文档证据和表格映射', () => {
  const customer = object({
    sourceRefs: [
      {
        kind: 'document',
        filename: '客户营销口径.md',
        anchors: [{ heading: '一、客户识别' }],
      },
      { kind: 'table', table: 'CUSTOMERS', columns: ['CUST_ID'] },
    ],
  });

  assert.equal(tableSourceFor(customer)?.table, 'CUSTOMERS');
  assert.deepEqual(sourceLabelsFor(customer), [
    '文档：客户营销口径.md / 一、客户识别',
    '表格：CUSTOMERS',
  ]);
  assert.deepEqual(findObjectDataMappingProblems(spec([customer]), [table()]), []);

  const files = renderPackage(spec([customer]));
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);
  assert.ok(mappings.objects.Customer);
  assert.match(files['objects/Customer.md'], /文档：客户营销口径\.md \/ 一、客户识别/);
  assert.match(files['objects/Customer.md'], /表格：CUSTOMERS/);
});

test('数据映射校验拒绝未知表，但允许外部输入对象暂不绑定来源', () => {
  const unknownTable = object({
    key: 'UnknownCustomer',
    sourceRefs: [{ kind: 'table', table: 'MISSING_TABLE' }],
  });
  const missingSource = object({ key: 'NoSourceCustomer' });

  const problems = findObjectDataMappingProblems(spec([unknownTable, missingSource]), [table()]);
  assert.equal(problems.length, 1);
  assert.match(problems[0].message, /来源表「MISSING_TABLE」不存在/);
  assert.throws(
    () => assertObjectDataMappings(spec([unknownTable, missingSource]), [table()]),
    /知识包数据映射无效.*MISSING_TABLE/,
  );
  assert.doesNotThrow(() => assertObjectDataMappings(spec([missingSource]), [table()]));
});

test('多表对象字段按全部来源表并集校验', () => {
  const multiTable = object({
    sourceRefs: [
      { kind: 'table', table: 'CUSTOMERS' },
      { kind: 'table', table: 'PRODUCTS' },
    ],
    fields: [
      { key: 'id', cnName: '客户号', sourceColumn: 'CUST_ID' },
      { key: 'product_id', cnName: '产品号', sourceColumn: 'PRODUCT_ID' },
    ],
    primaryKey: ['id'],
  });
  const productTable: DataDictTable = {
    name: 'PRODUCTS', rowCount: 1,
    fields: [{ name: 'PRODUCT_ID', inferredType: 'string', sampleValues: ['P1'], distinctRatio: 1 }],
  };

  assert.deepEqual(tableSourcesFor(multiTable).map((source) => source.table), ['CUSTOMERS', 'PRODUCTS']);
  assert.deepEqual(findObjectDataMappingProblems(spec([multiTable]), [table(), productTable]), []);
});

test('文档来源会显示第一个有名称的中文章节', () => {
  const product = object({
    sourceRefs: [{
      kind: 'document',
      filename: '业务制度.txt',
      anchors: [
        { offset: 42, quote: '集团客户资金归集' },
        { heading: '第三章　集团现金管理', offset: 260 },
      ],
    }],
  });

  assert.deepEqual(sourceLabelsFor(product), [
    '文档：业务制度.txt / 第三章　集团现金管理',
  ]);
  assert.match(
    renderPackage(spec([product]))['objects/Customer.md'],
    /文档：业务制度\.txt \/ 第三章　集团现金管理/,
  );
});
