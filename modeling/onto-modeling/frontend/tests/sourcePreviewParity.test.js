// 来源预览在本分支的接法（P5b，develop sourceLocation / studioUpdates 本身的行为见 sourceLocation.test.js、studioUpdates.test.js）：
// 带锚点跳来按来源预览打开、早先写文本版行号的出处、章节里的出处芯片识别、报告 id、Excel 合并区、流程图行的时间。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  inRange, isTextFragment, lineRange, materialOfText, mdCells, routeTarget, textPathOf, textView,
} from '../src/pages/resources/resourcesModel.js';
import { reportIdOf } from '../src/pages/reports/reportModel.js';
import { artifactToRow, sourceRefsIn } from '../src/pages/studio/studioModel.js';

test('带锚点跳来按来源预览打开；早先写文本版行号的出处选中原资料并记下是文本版', () => {
  assert.deepEqual(routeTarget({ anchor: 'file:resource/others/合成规则.docx#table:1/row:2/cell:3' }),
    { kind: 'file', path: 'resource/others/合成规则.docx', fragment: 'table:1/row:2/cell:3', source: true });
  assert.deepEqual(routeTarget({ anchor: 'file:resource/text/excel/合成表.xlsx.md#L12-L15' }),
    { kind: 'file', path: 'resource/excel/合成表.xlsx', fragment: 'L12-L15', source: true, textVersion: true });
  assert.deepEqual(routeTarget({ path: 'resource/others/说明.md' }), { kind: 'file', path: 'resource/others/说明.md', fragment: null });
});

test('文本版路径和原资料互换；行号范围只在要转文本的资料上按文本版定位', () => {
  assert.equal(textPathOf('resource/others/合成规则.docx'), 'resource/text/others/合成规则.docx.md');
  assert.equal(materialOfText('resource/text/others/合成规则.docx.md'), 'resource/others/合成规则.docx');
  assert.equal(materialOfText('resource/others/说明.md'), null);
  assert.deepEqual(lineRange('L12-15'), { from: 12, to: 15 });
  assert.ok(inRange('L13', lineRange('L12-L15')) && !inRange('L16', lineRange('L12-L15')));
  assert.ok(isTextFragment({ text: { state: 'converted' } }, 'L12'));
  assert.ok(!isTextFragment({ text: { state: 'native' } }, 'L12'));
  assert.deepEqual(mdCells('| a\\|b | 第一行<br>第二行 |'), ['a|b', '第一行\n第二行']);
  const view = textView([
    { anchor: 'L5', kind: 'table', text: '| 品类 | 周期 |', location: '第 2 页' },
    { anchor: 'L6', kind: 'table', text: '|---|---|', location: '第 2 页' },
    { anchor: 'L7', kind: 'table', text: '| 牛奶 | 隔日 |', location: '第 2 页' },
  ]);
  assert.deepEqual(view.map((v) => v.type), ['loc', 'table']);
  assert.deepEqual(view[1].rows.map((r) => [r.anchor, r.head]), [['L5', true], ['L7', false]]);
});

test('章节里的出处芯片：行内代码与正文里的资料路径（带锚点）、报告小节，去重，外层括号不算进小节', () => {
  const text = '- `resource/others/合成规则.docx#p:12`（规则ID R-01）\n'
    + '- resource/excel/合成表.xlsx#sheet:规则!B3；另见 `resource/others/合成规则.docx#p:12`\n'
    + '- 无（领域知识整理）\n- `research/20260101-订单.md#退款时限`（规则ID R-02）\n'
    + '（见 research/报告.md#退款时限（R-02））';
  assert.deepEqual(sourceRefsIn(text), [
    { type: 'source', path: 'resource/others/合成规则.docx', anchor: 'p:12' },
    { type: 'heading', path: 'research/20260101-订单.md', anchor: '退款时限' },
    { type: 'source', path: 'resource/excel/合成表.xlsx', anchor: 'sheet:规则!B3' },
    { type: 'heading', path: 'research/报告.md', anchor: '退款时限（R-02）' },
  ]);
  assert.deepEqual(sourceRefsIn('普通文字'), []);
});

test('报告路径换报告 id（同后端 reports.id_of）', () => {
  assert.equal(reportIdOf('research/20260101-订单.md'), 'research:20260101-订单');
  assert.equal(reportIdOf('reports/规则检查.md'), '规则检查');
});

test('流程图行的最后更新时间取产物的修改时间', () => {
  const row = artifactToRow({ key: 'f1', title: '合成流程', status: { value: 'confirmed' }, updated_at: '2026-01-02T03:04:05.000Z' });
  assert.equal(row.updated_at, '2026-01-02T03:04:05.000Z');
});
