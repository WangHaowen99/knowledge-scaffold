// 原始资料「路径#锚点」解析、位置文案与定位判断（lib/sourceLocation.js）。合成数据。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  anchorLabel,
  findHeadingIndex,
  groupSegments,
  legacyOfficeHint,
  locateSheetCell,
  parseCellRef,
  parseHeadingRef,
  parseSheetAnchor,
  parseSourceRef,
  previewKind,
  sourceChipLabel,
  trimHeadingRef,
} from '../src/lib/sourceLocation.js';

test('只把 resource/ 下的已知格式识别为资料路径，锚点可选', () => {
  assert.deepEqual(parseSourceRef('resource/others/合成规则.docx#p:12'), {
    path: 'resource/others/合成规则.docx',
    anchor: 'p:12',
  });
  assert.deepEqual(parseSourceRef(' resource/excel/合成表.xlsx#sheet:规则!B3 '), {
    path: 'resource/excel/合成表.xlsx',
    anchor: 'sheet:规则!B3',
  });
  assert.deepEqual(parseSourceRef('resource/others/手册.pdf'), { path: 'resource/others/手册.pdf', anchor: '' });
  assert.equal(parseSourceRef('research/20261030-合成.md#业务项清单'), null);
  assert.equal(parseSourceRef('resource/others/无扩展名'), null);
  assert.equal(parseSourceRef('无（领域知识整理）'), null);
});

test('芯片显示文件名和位置', () => {
  assert.equal(sourceChipLabel('resource/others/合成规则.docx', 'p:12'), '合成规则.docx · 第 12 段');
  assert.equal(sourceChipLabel('resource/others/说明.md', 'L12'), '说明.md · 第 12 行');
  assert.equal(
    sourceChipLabel('resource/others/合成规则.docx', 'table:1/row:2/cell:3'),
    '合成规则.docx · 表 1 第 2 行第 3 列',
  );
  assert.equal(sourceChipLabel('resource/excel/合成表.xlsx', 'sheet:规则!B3'), '合成表.xlsx · 规则!B3');
  assert.equal(sourceChipLabel('resource/others/手册.pdf'), '手册.pdf');
  assert.equal(anchorLabel('未知写法'), '未知写法');
});

test('按格式和锚点选择预览方式', () => {
  assert.equal(previewKind('resource/others/合成规则.docx'), 'segments');
  assert.equal(previewKind('resource/others/说明.md', 'L3'), 'segments');
  assert.equal(previewKind('resource/others/说明.md'), 'default');
  assert.equal(previewKind('resource/excel/合成表.xlsx', 'sheet:规则!B3'), 'excel');
  assert.equal(previewKind('resource/others/手册.pdf'), 'pdf');
  assert.equal(previewKind('resource/others/截图.PNG'), 'image');
  for (const name of ['旧版.doc', '旧版.xls', '汇报.ppt', '汇报.pptx']) {
    assert.equal(previewKind(`resource/others/${name}`), 'download');
  }
  assert.equal(previewKind('resource/diagrams/流程.drawio'), 'default');
});

test('解析单元格与工作表锚点，工作表名可含感叹号', () => {
  assert.deepEqual(parseCellRef('B3'), { row: 2, col: 1 });
  assert.deepEqual(parseCellRef('AA10'), { row: 9, col: 26 });
  assert.equal(parseCellRef('3B'), null);
  assert.deepEqual(parseSheetAnchor('sheet:合成!规则!$C$5'), {
    sheet: '合成!规则', cell: '$C$5', row: 4, col: 2,
  });
  assert.equal(parseSheetAnchor('p:3'), null);
});

const workbook = {
  sheet_truncated: false,
  sheets: [
    { name: '说明', rows: [['a']], merges: [], truncated: false },
    { name: '规则', rows: [['编号', '说明'], ['R-01', '合成']], merges: [], truncated: false },
  ],
};

test('Excel 定位：命中给出工作表与格子，失效与超出范围分别说明', () => {
  assert.deepEqual(locateSheetCell(workbook, 'sheet:规则!B2'), { index: 1, cell: { row: 1, col: 1 } });
  assert.equal(locateSheetCell(workbook, ''), null);
  assert.equal(locateSheetCell(workbook, 'sheet:不存在!A1').problem, 'missing');
  assert.equal(locateSheetCell(workbook, 'sheet:规则!B9').problem, 'missing');
  const truncated = { ...workbook, sheets: [workbook.sheets[0], { ...workbook.sheets[1], truncated: true }] };
  const beyond = locateSheetCell(truncated, 'sheet:规则!B600');
  assert.equal(beyond.problem, 'range');
  assert.equal(beyond.index, 1);
  assert.equal(locateSheetCell({ ...workbook, sheet_truncated: true }, 'sheet:第21张!A1').problem, 'range');
});

test('连续的 Word 表格单元格合成一张表，表格之间和段落分开', () => {
  const blocks = groupSegments([
    { anchor: 'p:1', text: '标题' },
    { anchor: 'table:1/row:1/cell:1', text: '编号' },
    { anchor: 'table:1/row:2/cell:3', text: '合成' },
    { anchor: 'table:2/row:1/cell:1', text: '另一张' },
    { anchor: 'p:4', text: '结尾' },
  ]);
  assert.deepEqual(blocks.map((block) => block.type), ['segment', 'table', 'table', 'segment']);
  assert.equal(blocks[1].rows, 2);
  assert.equal(blocks[1].cols, 3);
  assert.equal(blocks[2].table, 2);
});

test('上传 .doc、.xls 时给出另存提示，其他格式不提示', () => {
  assert.match(legacyOfficeHint('旧版规则.doc'), /另存为 \.docx/);
  assert.match(legacyOfficeHint('旧版表.XLS'), /另存为 \.xlsx/);
  assert.equal(legacyOfficeHint('新版规则.docx'), '');
});

test('带小节的报告路径能解析，普通文字和资料路径不算', () => {
  assert.deepEqual(parseHeadingRef('research/20261031-合成报告.md#退款金额上限（R-01）'), {
    path: 'research/20261031-合成报告.md',
    anchor: '退款金额上限（R-01）',
  });
  assert.equal(parseHeadingRef('research/20261031-合成报告.md'), null);
  assert.equal(parseHeadingRef('resource/others/说明.md#L3'), null);
  assert.equal(parseHeadingRef('合成文字#小节'), null);
});

test('小节定位优先全文相同，其次包含，找不到返回 -1', () => {
  const headings = ['业务项清单', ' 退款金额上限（R-01） ', '退款金额'];
  assert.equal(findHeadingIndex(headings, '退款金额'), 2);
  assert.equal(findHeadingIndex(headings, '退款金额上限（R-01）'), 1);
  assert.equal(findHeadingIndex(headings, '上限'), 1);
  assert.equal(findHeadingIndex(headings, '不存在的小节'), -1);
});

test('正文里的报告小节路径：小节名自带的括号保留，外层多出的右括号放回正文', () => {
  assert.deepEqual(trimHeadingRef('research/合成报告.md#退款时限（R-02）'), {
    ref: 'research/合成报告.md#退款时限（R-02）',
    rest: '',
  });
  assert.deepEqual(trimHeadingRef('research/合成报告.md#退款时限）'), {
    ref: 'research/合成报告.md#退款时限',
    rest: '）',
  });
  assert.deepEqual(trimHeadingRef('research/合成报告.md#退款时限(R-02))'), {
    ref: 'research/合成报告.md#退款时限(R-02)',
    rest: ')',
  });
  assert.deepEqual(trimHeadingRef('research/合成报告.md'), { ref: 'research/合成报告.md', rest: '' });
});
