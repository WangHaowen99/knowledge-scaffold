import assert from 'node:assert/strict';
import test from 'node:test';

import { uploadModelingMaterials } from '../frontend/src/modeling-material-upload.js';

test('知识包建模助手一次上传用户选择的全部建模资料并返回摘要', async () => {
  const uploaded: Array<[string, string]> = [];
  const files = [
    { name: '访谈纪要.md' },
    { name: '业务数据.xlsx' },
  ];

  const result = await uploadModelingMaterials({
    projectId: 'project-ai-construction',
    files,
    uploadFile: async (projectId, file) => {
      uploaded.push([projectId, file.name]);
      return { document: { id: `doc-${uploaded.length}` } };
    },
  });

  assert.deepEqual(uploaded, [
    ['project-ai-construction', '访谈纪要.md'],
    ['project-ai-construction', '业务数据.xlsx'],
  ]);
  assert.deepEqual(result, {
    count: 2,
    filenames: ['访谈纪要.md', '业务数据.xlsx'],
  });
});
