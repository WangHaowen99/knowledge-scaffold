// F4b-paths：界面上不出工作区路径和文件名——变更卡、查看改动、资料列表、出处标签
import assert from 'node:assert/strict';
import test from 'node:test';
import { roundDiffLines } from '../src/pages/workbench/roundState.js';
import { roundLines } from '../src/pages/workbench/incremental/model.js';
import { materialTitle, metaText, sortMaterials } from '../src/pages/resources/resourcesModel.js';
import { anchorChipLabel, resolutionLabel } from '../src/pages/studio/studioModel.js';

const sec = (text) => [{ title: '', lines: [{ kind: 'add', text }] }];

test('查看改动：按条目分段，段首是业务名；认不出的合成最后一段「另有 N 处改动」', () => {
  const lines = roundDiffLines([
    { target: { kind: 'object', name: '门店', label: '对象「门店」' }, sections: sec('+ 名称') },
    { target: { type: 'file', key: 'notes/a.txt', title: '', label: '' }, sections: sec('+ 一') },
    { target: { type: 'file', key: 'research/blueprint.json', title: '建模蓝图', label: '建模蓝图' }, sections: sec('+ 段') },
    { target: { type: 'file', key: 'notes/b.txt', title: '', label: '' }, sections: sec('+ 二') },
  ]);
  assert.deepEqual(lines.filter((l) => l.kind === 'header').map((l) => l.text), ['对象「门店」', '建模蓝图', '另有 2 处改动']);
  assert.equal(lines.length, 7);
  assert.ok(!lines.some((l) => /notes\/|\.txt/.test(l.text)));
});

test('增量那一轮的卡：「另有 N 处改动」不带动词', () => {
  const card = { groups: [{ kind: 'other', items: [
    { change: 'mod', text: '建模蓝图' }, { change: null, text: '另有 2 处改动' },
  ] }] };
  assert.deepEqual(roundLines(card).map((l) => [l.verb, l.text]), [['修改', '建模蓝图'], ['', '另有 2 处改动']]);
});

test('资料列表：不按目录分组，按资料名排；第二行写类型', () => {
  const groups = [
    { key: 'excel', items: [{ path: 'resource/excel/b.xlsx', name: 'b.xlsx', title: '乙', type_label: 'Excel', size: 10 }] },
    { key: 'others', items: [{ path: 'resource/others/a.pdf', name: 'a.pdf', title: '甲', type_label: 'PDF', size: 10 }] },
  ];
  assert.deepEqual(sortMaterials(groups).map(materialTitle), ['甲', '乙']);
  assert.equal(materialTitle({ name: '旧接口.docx' }), '旧接口.docx');
  const at = '2026-10-16T08:00:00Z';
  assert.equal(metaText({ kind: 'pdf', type_label: 'PDF', size: 2048, uploaded_at: at }, Date.parse('2026-11-03T00:00:00Z')), 'PDF · 2 KB · 10-16 上传');
});

test('出处标签：只说资料名，文本版映射回原资料名；解析结果优先用 material.name', () => {
  assert.equal(anchorChipLabel('file:resource/text/others/04-合成手册.pdf.md#L12-L14'), '04-合成手册');
  assert.equal(anchorChipLabel('resource/others/说明.md#L3'), '说明');
  assert.equal(resolutionLabel({ resolved: true, scheme: 'file', source_label: '旧.docx', material: { name: '04-合成手册' }, location_label: '第 3 页' }), '04 合成手册 第 3 页');
});
