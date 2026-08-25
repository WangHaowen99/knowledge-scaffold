import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  assertNoFieldNameConflicts,
  fieldAliasTokens,
  findFieldNameConflicts,
  normalizeFieldAlias,
} from '../src/workflow/field-alias.js';
import { runFormatChecks } from '../src/workflow/package-check.js';
import { parseRenderedPackageFiles } from '../src/workflow/package-import.js';
import { renderPackage } from '../src/workflow/package-render.js';
import type { PackageSpec } from '../src/types.js';

function syntheticSpec(): PackageSpec {
  return {
    id: 'knowledge-package-demo',
    name: 'Synthetic package',
    description: 'Package containing only synthetic field definitions.',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'default',
    objects: [{
      key: 'ObjectDemo',
      title: 'Synthetic object',
      description: 'Object containing only synthetic fields.',
      fields: [
        { key: 'empty_alias', cnName: 'Empty alias' },
        { key: 'ascii_dash', cnName: 'ASCII dash', alias: '-' },
        { key: 'em_dash', cnName: 'Em dash', alias: '—' },
        { key: 'en_dash', cnName: 'En dash', alias: '–' },
        { key: 'none_text', cnName: 'None text', alias: '无' },
        { key: 'na_text', cnName: 'NA text', alias: 'N/A' },
      ],
    }],
    operations: [],
  };
}

test('字段别名规范化：约定占位符统一成为空值', () => {
  for (const marker of ['', '-', '—', '–', '无', 'N/A', ' n/a ']) {
    assert.equal(normalizeFieldAlias(marker), undefined);
    assert.deepEqual(fieldAliasTokens(marker), []);
  }
  assert.equal(normalizeFieldAlias('Code、N/A'), 'Code');
  assert.deepEqual(fieldAliasTokens('Code，–；Reference'), ['Code', 'Reference']);
});

test('知识包渲染：无别名字段输出空单元格', () => {
  const markdown = renderPackage(syntheticSpec())['objects/ObjectDemo.md'];
  const rows = markdown.split('\n').filter((line) => /^\| (?:empty_alias|ascii_dash|em_dash|en_dash|none_text|na_text) /u.test(line));

  assert.equal(rows.length, 6);
  assert.ok(rows.every((line) => line.endsWith('|  |')));
});

test('知识包解析：历史占位符不会进入结构化字段别名', () => {
  const files = renderPackage(syntheticSpec());
  files['objects/ObjectDemo.md'] = files['objects/ObjectDemo.md']
    .replace('| em_dash | Em dash | 未声明 | 未声明 |  |  |', '| em_dash | Em dash | 未声明 | 未声明 |  | — |');

  const parsed = parseRenderedPackageFiles(files);

  assert.equal(parsed.objects[0].fields.find((field) => field.key === 'em_dash')?.alias, undefined);
});

test('字段别名预检：忽略占位符但阻断真实重复名称', () => {
  const placeholderSpec = syntheticSpec();
  assert.deepEqual(findFieldNameConflicts(placeholderSpec.objects), []);
  assert.doesNotThrow(() => assertNoFieldNameConflicts(placeholderSpec.objects));

  const conflictSpec = syntheticSpec();
  conflictSpec.objects[0].fields = [
    { key: 'field_a', cnName: 'Field A', alias: 'Shared name' },
    { key: 'field_b', cnName: 'Field B', alias: 'Shared name' },
  ];
  const conflicts = findFieldNameConflicts(conflictSpec.objects);
  assert.equal(conflicts.length, 1);
  assert.equal(conflicts[0].path, 'objects/ObjectDemo.md');
  assert.deepEqual(conflicts[0].fieldKeys, ['field_a', 'field_b']);
  assert.throws(
    () => assertNoFieldNameConflicts(conflictSpec.objects),
    /objects\/ObjectDemo\.md.*field_a.*Shared name.*field_b.*Shared name/u,
  );

  const findings = runFormatChecks(conflictSpec, renderPackage(conflictSpec));
  assert.ok(findings.some((finding) => (
    finding.severity === '错误'
    && finding.category === '字段名称冲突'
    && finding.target === 'objects/ObjectDemo.md'
  )));
});
