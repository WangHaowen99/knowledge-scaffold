// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import test from 'node:test';
import assert from 'node:assert/strict';
import { summarizeImportPreview } from './importPreviewSummary.js';

test('summarizeImportPreview reduces source and result file contents to operational metadata', () => {
  const summary = summarizeImportPreview({
    source: {
      total_files: 5,
      truncated: true,
      files: [
        {
          path: 'objects/a.md', size: 20, content: 'a'.repeat(100), truncated: false, binary: false,
        },
        {
          path: 'assets/logo.png', size: 12, content: '', truncated: false, binary: true,
        },
        {
          path: 'operations/b.md', size: 40, content: 'b', truncated: true, binary: false,
        },
      ],
    },
    result: {
      total_files: 4,
      truncated: false,
      files: [{
        path: 'objects/a.md', size: 18, content: 'normalized', truncated: false, binary: false,
      }],
    },
    warnings: { aliases: ['别名重复'], normalization_warnings: ['路径已规范化'] },
  });

  assert.equal(summary.kind, 'structured');
  assert.deepEqual(summary.source, {
    totalFiles: 5,
    previewedFiles: 3,
    textFiles: 2,
    binaryFiles: 1,
    truncatedFiles: 1,
    isTruncated: true,
    paths: ['objects/a.md', 'assets/logo.png', 'operations/b.md'],
  });
  assert.deepEqual(summary.result, {
    totalFiles: 4,
    previewedFiles: 1,
    textFiles: 1,
    binaryFiles: 0,
    truncatedFiles: 0,
    isTruncated: true,
    paths: ['objects/a.md'],
  });
  assert.deepEqual(summary.warnings, [
    { category: 'aliases', message: '别名重复' },
    { category: 'normalization_warnings', message: '路径已规范化' },
  ]);
});

test('summarizeImportPreview supports text and legacy array previews without exposing content in summary', () => {
  assert.deepEqual(summarizeImportPreview('import complete'), {
    kind: 'text',
    text: 'import complete',
  });

  const summary = summarizeImportPreview([
    {
      path: 'objects/a.md', size: 3, content: 'abc', binary: false, truncated: false,
    },
    'legacy message',
  ]);
  assert.deepEqual(summary, {
    kind: 'array',
    totalFiles: 2,
    previewedFiles: 2,
    textFiles: 1,
    binaryFiles: 0,
    truncatedFiles: 0,
    isTruncated: false,
    paths: ['objects/a.md', 'legacy message'],
  });
});

test('empty or missing preview is explicitly marked empty', () => {
  assert.deepEqual(summarizeImportPreview(null), { kind: 'empty' });
  assert.deepEqual(summarizeImportPreview({}), { kind: 'empty' });
});
