import assert from 'node:assert/strict';
import test from 'node:test';

import type { PackageSpec } from '../src/types.js';
import { runFormatChecks } from '../src/workflow/package-check.js';
import { publicKnowledgePackageFiles } from '../src/workflow/package-file-scope.js';
import { renderPackage } from '../src/workflow/package-render.js';

function syntheticSpec(): PackageSpec {
  return {
    id: 'package-synthetic',
    name: 'Synthetic package',
    description: 'Fully synthetic package for delivery boundary tests.',
    version: '1.0.0',
    status: 'review',
    language: 'en',
    defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
}

test('公开交付投影排除内部工作记录并保留文本和二进制文件', () => {
  const files = publicKnowledgePackageFiles<string | Uint8Array>({
    'index.md': '# public',
    'assets/synthetic.bin': new Uint8Array([1, 2, 3]),
    '.modeling/plan.md': '# internal',
    '.modeling/cache/synthetic.bin': new Uint8Array([4, 5, 6]),
  });

  assert.deepEqual(Object.keys(files).sort(), ['assets/synthetic.bin', 'index.md']);
});

test('包格式检查忽略内部 Markdown 但仍准确阻断非法公开 Markdown', () => {
  const spec = syntheticSpec();
  const files = {
    ...renderPackage(spec),
    '.modeling/plan.md': '# Synthetic internal work note\n',
  };

  assert.equal(runFormatChecks(spec, files).filter((finding) => finding.severity === '错误').length, 0);

  files['index.md'] = '# Invalid public index\n';
  const errors = runFormatChecks(spec, files).filter((finding) => finding.severity === '错误');
  assert.ok(errors.some((finding) => finding.target === 'index.md' && finding.category === 'frontmatter 缺失'));
  assert.ok(errors.every((finding) => finding.target !== '.modeling/plan.md'));
});
