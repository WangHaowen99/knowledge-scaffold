// 对话里「这一轮改了哪些文件」卡片的分组与标题（lib/changeSummary.js）。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { changeHeadline, displayName, groupChanges } from '../src/lib/changeSummary.js';

test('标题用大白话：单一类改动只说动作，多类改动给总数和分项', () => {
  assert.equal(changeHeadline({ added: 38 }), '这一轮新建了 38 个文件');
  assert.equal(changeHeadline({ modified: 1 }), '这一轮修改了 1 个文件');
  assert.equal(
    changeHeadline({ added: 2, modified: 3, deleted: 1 }),
    '这一轮改动了 6 个文件：新建 2 个，修改 3 个，删除 1 个',
  );
});

test('按业务类别分组，按固定顺序展示，未知目录归入其他文件', () => {
  const groups = groupChanges([
    { path: 'ontology/excel/订单明细.xlsx', status: 'added' },
    { path: 'ontology/operation/订单审核.md', status: 'added' },
    { path: 'ontology/diagrams/订单审核.drawio', status: 'modified' },
    { path: 'ontology/diagrams-summary/订单审核.md', status: 'added' },
    { path: 'ontology/testcase/订单审核.md', status: 'added' },
    { path: 'notes/备忘.txt', status: 'deleted' },
  ]);
  assert.deepEqual(
    groups.map((group) => [group.label, group.items.map((item) => item.name)]),
    [
      ['业务逻辑', ['订单审核']],
      ['流程图摘要', ['订单审核']],
      ['流程图', ['订单审核']],
      ['Excel 补充表', ['订单明细']],
      ['测试场景', ['订单审核']],
      ['其他文件', ['备忘']],
    ],
  );
  assert.equal(groups[2].items[0].status, 'modified');
});

test('文件名去掉目录和扩展名，没有扩展名的保持原样', () => {
  assert.equal(displayName('ontology/object/订单.md'), '订单');
  assert.equal(displayName('research/index'), 'index');
  assert.equal(displayName('.gitignore'), '.gitignore');
});
