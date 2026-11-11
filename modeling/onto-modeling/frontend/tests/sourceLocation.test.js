// 规则出处打开资料并定位（P5b，develop sourceLocation / studioUpdates 的同类行为）：文本版锚点映射回原资料、
// 行号范围、文本版块的画法、章节里的出处识别；Studio 条目表「最后更新时间」的显示与排序。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  inRange, isTextFragment, lineRange, materialOfText, mdCells, routeTarget, textPathOf, textView,
} from '../src/pages/resources/resourcesModel.js';
import {
  anchorChipLabel, artifactToRow, fmtUpdated, nextUpdatedOrder, sectionAnchors, sortByUpdated,
} from '../src/pages/studio/studioModel.js';

test('文本版路径和原资料互换', () => {
  assert.equal(textPathOf('resource/others/合成规则.docx'), 'resource/text/others/合成规则.docx.md');
  assert.equal(materialOfText('resource/text/others/合成规则.docx.md'), 'resource/others/合成规则.docx');
  assert.equal(materialOfText('resource/others/说明.md'), null);
  assert.equal(materialOfText('resource/text/others/合成规则.docx'), null);
});

test('出处写的是文本版：资料区选中原资料，片段照旧', () => {
  assert.deepEqual(routeTarget({ anchor: 'file:resource/text/excel/合成表.xlsx.md#L12-L15' }),
    { kind: 'file', path: 'resource/excel/合成表.xlsx', fragment: 'L12-L15' });
  assert.deepEqual(routeTarget({ anchor: 'file:resource/others/说明.md#L3' }),
    { kind: 'file', path: 'resource/others/说明.md', fragment: 'L3' });
});

test('行号片段与范围；只有要转文本的资料才按文本版定位', () => {
  assert.deepEqual(lineRange('L12'), { from: 12, to: 12 });
  assert.deepEqual(lineRange('L12-L15'), { from: 12, to: 15 });
  assert.deepEqual(lineRange('L12-15'), { from: 12, to: 15 });
  assert.equal(lineRange('p:3'), null);
  assert.equal(lineRange('sheet:规则!B3'), null);
  assert.ok(inRange('L13', lineRange('L12-L15')));
  assert.ok(!inRange('L16', lineRange('L12-L15')));
  const docx = { path: 'resource/others/合成规则.docx', text: { state: 'converted' } };
  const md = { path: 'resource/others/说明.md', text: { state: 'native' } };
  assert.ok(isTextFragment(docx, 'L12'));
  assert.ok(!isTextFragment(docx, 'p:3'));
  assert.ok(!isTextFragment(md, 'L12'));
});

test('表格行按竖线拆格，转义的竖线和 <br> 还原', () => {
  assert.deepEqual(mdCells('| 品类 | 周期 |'), ['品类', '周期']);
  assert.deepEqual(mdCells('| a\\|b | 第一行<br>第二行 |'), ['a|b', '第一行\n第二行']);
  assert.deepEqual(mdCells('| x |  |'), ['x', '']);
});

test('文本版的块：位置变了出分隔，连续表格行合成一张表，紧跟分隔行的是表头', () => {
  const view = textView([
    { anchor: 'L3', kind: 'paragraph', text: '第一页正文', location: '第 1 页' },
    { anchor: 'L5', kind: 'table', text: '| 品类 | 周期 |', location: '第 2 页' },
    { anchor: 'L6', kind: 'table', text: '|---|---|', location: '第 2 页' },
    { anchor: 'L7', kind: 'table', text: '| 牛奶 | 隔日 |', location: '第 2 页' },
    { anchor: 'L8', kind: 'paragraph', text: '冷链品单独配送', location: '第 2 页' },
  ]);
  assert.deepEqual(view.map((v) => v.type), ['loc', 'block', 'loc', 'table', 'block']);
  assert.deepEqual(view[3].rows.map((r) => [r.anchor, r.head, r.cells]), [['L5', true, ['品类', '周期']], ['L7', false, ['牛奶', '隔日']]]);
  // 窗口从表格中间开始：没有分隔行就都是数据行；没有位置标记的（Word）不出分隔
  const mid = textView([{ anchor: 'L9', kind: 'table', text: '| 酸奶 | 每日 |' }, { anchor: 'L10', kind: 'paragraph', text: '完' }]);
  assert.deepEqual(mid.map((v) => v.type), ['table', 'block']);
  assert.equal(mid[0].rows[0].head, false);
});

test('章节正文里的资料出处：去重、带片段、收尾标点不算进锚点', () => {
  const text = '- `resource/text/others/合成规则.docx.md#L12`（规则ID R-01）\n'
    + '- resource/others/说明.md#L3-L5；另见 resource/others/说明.md#L3-L5。\n'
    + '- 无（领域知识整理）\n- `research/20260101-订单.md#退款时限`';
  assert.deepEqual(sectionAnchors(text), [
    'file:resource/text/others/合成规则.docx.md#L12',
    'file:resource/others/说明.md#L3-L5',
  ]);
  assert.deepEqual(sectionAnchors('file:resource/others/说明.md'), ['file:resource/others/说明.md']);
  assert.equal(anchorChipLabel('file:resource/text/others/合成规则.docx.md#L12'), '合成规则');
});

test('最后更新时间：本地时区精确到秒，取不到写破折号', () => {
  const iso = new Date(2026, 0, 2, 3, 4, 5).toISOString();
  assert.equal(fmtUpdated(iso), '2026-01-02 03:04:05');
  assert.equal(fmtUpdated(null), '—');
  assert.equal(fmtUpdated('bad'), '—');
});

test('按最后更新时间排：两个方向都按时间，取不到时间的始终在最后，同一时间保持原顺序', () => {
  const at = (s) => new Date(s * 1000).toISOString();
  const rows = [
    { id: 'missing', updated_at: null },
    { id: 'old', updated_at: at(100) },
    { id: 'same-1', updated_at: at(200) },
    { id: 'same-2', updated_at: at(200) },
    { id: 'new', updated_at: at(300) },
  ];
  assert.deepEqual(sortByUpdated(rows, 'desc').map((r) => r.id), ['new', 'same-1', 'same-2', 'old', 'missing']);
  assert.deepEqual(sortByUpdated(rows, 'asc').map((r) => r.id), ['old', 'same-1', 'same-2', 'new', 'missing']);
  assert.equal(sortByUpdated(rows, null), rows);
  assert.equal(nextUpdatedOrder(null), 'desc');
  assert.equal(nextUpdatedOrder('desc'), 'asc');
  assert.equal(nextUpdatedOrder('asc'), 'desc');
});

test('流程图行的最后更新时间取产物的修改时间', () => {
  const row = artifactToRow({ key: 'f1', title: '合成流程', status: { value: 'confirmed' }, updated_at: '2026-01-02T03:04:05.000Z' });
  assert.equal(row.updated_at, '2026-01-02T03:04:05.000Z');
});

test('规则「来源」里的报告小节：小节名自带的括号保留，外层多出的右括号去掉；报告 id 同后端', async () => {
  const { headingLine, reportRefs, researchReportId } = await import('../src/pages/reports/reportModel.js');
  assert.deepEqual(reportRefs('`research/20260101-订单.md#退款时限`（规则ID R-02）'), [{ path: 'research/20260101-订单.md', section: '退款时限' }]);
  assert.deepEqual(reportRefs('（见 research/报告.md#退款时限（R-02））'), [{ path: 'research/报告.md', section: '退款时限（R-02）' }]);
  assert.deepEqual(reportRefs('resource/others/说明.md#L3 与 research/报告.md'), []);
  assert.equal(researchReportId('research/20260101-订单.md'), 'research:20260101-订单');
  const lines = ['# 详细信息', '## 退款时限（R-02）', '正文', '## 退款时限'];
  assert.equal(headingLine(lines, '退款时限'), 3);
  assert.equal(headingLine(lines, 'R-02'), 1);
  assert.equal(headingLine(lines, '不存在'), -1);
});
