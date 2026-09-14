import assert from 'node:assert/strict';
import test from 'node:test';

import type { PackageSpec } from '../src/types.js';
import { runFormatChecks } from '../src/workflow/package-check.js';
import { renderPackage, renderRootIndex } from '../src/workflow/package-render.js';

function syntheticSpec(): PackageSpec {
  return {
    id: 'package-root-index',
    name: '合成知识包',
    description: '用于根索引一致性测试的合成知识包。',
    version: '1.0.0',
    status: 'review',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [{
      key: 'synthetic_record',
      title: '合成记录',
      description: '绑定物理数据表的合成对象',
      sourceTable: 'synthetic_record',
      fields: [{ key: 'record_id', cnName: '记录号', sourceColumn: 'record_id' }],
    }],
    operations: [],
  };
}

test('根索引的业务数据查询能力随对象数据表绑定切换', () => {
  const bound = syntheticSpec();
  assert.match(renderRootIndex(bound), /\| 业务数据查询 \| enabled \| 通过 mappings\/object-data-mappings\.json/u);

  const unbound: PackageSpec = { ...bound, objects: [] };
  assert.match(renderRootIndex(unbound), /\| 业务数据查询 \| disabled \| 当前包没有绑定业务数据表。/u);
});

test('根索引单独渲染时按当前修订决定列不列流程图目录', () => {
  const spec = syntheticSpec();
  assert.ok(renderRootIndex(spec, true).includes('- 流程图目录：diagrams/index.md'));
  assert.ok(!renderRootIndex(spec).includes('- 流程图目录：diagrams/index.md'));
  // 不带流程图时必须与全量渲染逐字节一致，两条渲染路径不能有第二套口径
  assert.equal(renderRootIndex(spec), renderPackage(spec)['index.md']);
});

test('根索引与数据映射矛盾时包格式检查发布阻断', () => {
  const spec = syntheticSpec();
  const files = renderPackage(spec);
  assert.equal(runFormatChecks(spec, files).filter((finding) => finding.severity === '错误').length, 0);

  // 模拟增量写入漏刷根索引：对象已绑定数据表、mapping 已生成，根索引仍停在空 spec 快照
  files['index.md'] = renderRootIndex({ ...spec, objects: [] });
  const findings = runFormatChecks(spec, files)
    .filter((finding) => finding.category === '根索引能力不一致');

  assert.equal(findings.length, 1);
  assert.equal(findings[0].target, 'index.md');
  assert.equal(findings[0].severity, '错误');
  assert.equal(findings[0].releaseBlocking, true);
  assert.match(findings[0].message, /业务数据查询标为 disabled/u);
});

test('人工改写的根索引缺少 Runtime 能力行时不误报一致性问题', () => {
  const spec = syntheticSpec();
  const files = renderPackage(spec);
  files['index.md'] = files['index.md'].replace(/^\| 业务数据查询 \|.*$/mu, '');

  assert.equal(
    runFormatChecks(spec, files).filter((finding) => finding.category === '根索引能力不一致').length,
    0,
  );
});
