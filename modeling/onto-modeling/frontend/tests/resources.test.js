// 资料区：页面纯逻辑（resourcesModel.js）与 mock（routes-resources.js + routes-materials.js 共用的资料状态）
import assert from 'node:assert/strict';
import test, { mock } from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register as registerMaterials, stages } from '../src/api/mock/routes-materials.js';
import { register as registerResources, taskSeeds } from '../src/api/mock/routes-resources.js';
import { DRAWIO, RES_PATHS } from '../src/api/mock/resources-data.js';
import {
  blockBody, blockLabel, cellPos, checkUpload, columnLetter, coverageChip, csvText, drawioLayout, fmtSize, metaText,
  parseDrawio, parseFileAnchor, parseFragment, pickDefault, purposeView, refCount, refRows, refSentence, refTarget, replaceName,
  routeTarget, scaleText, sheetGrid, locateSheetCell, shortTime, splitHighlights, textChip, textSections, hasTextTab,
} from '../src/pages/resources/resourcesModel.js';

const R02 = 'resource/others/02-门店补货规则.docx';
const R03B = 'resource/others/03-样例补货单-B.docx';

test('行文字：大小、两天内带时刻、表格写规模、被引用（资源模板不标）', () => {
  assert.equal(fmtSize(900), '900 B');
  assert.equal(fmtSize(312 * 1024), '312 KB');
  assert.equal(fmtSize(2.4 * 1024 * 1024), '2.4 MB');
  const now = new Date('2026-11-01T10:00:00').getTime();
  assert.equal(shortTime(new Date('2026-10-31T21:40:00').toISOString(), now), '10-31 21:40');
  assert.equal(shortTime(new Date('2026-10-16T10:20:00').toISOString(), now), '10-16');
  assert.equal(scaleText({ kind: 'xls', sheets: 3, rows: 1204, size: 1 }), '3 个工作表 · 1,204 行');
  assert.equal(scaleText({ kind: 'csv', rows: 412, size: 1 }), '412 行');
  assert.equal(scaleText({ kind: 'xls', size: 22 * 1024 }), '22 KB');
  const at = new Date('2026-10-16T10:20:00').toISOString();
  assert.equal(metaText({ kind: 'drawio', size: 24 * 1024, uploaded_at: at, is_template: true }, now), '24 KB · 10-16 上传');
  assert.equal(metaText({ kind: 'doc', size: 312 * 1024, uploaded_at: at, referenced: 16 }, now), '312 KB · 10-16 上传 · 被引用 16 处');
});

test('用途按钮：「自动识别」只标自动判出来的，「由助手判断」本身不标', () => {
  assert.deepEqual(purposeView({ purpose: 'schema', purpose_auto: true }), { label: '数据 Schema', auto: true });
  assert.deepEqual(purposeView({ purpose: 'auto', purpose_auto: true }), { label: '由助手判断', auto: false });
  assert.deepEqual(purposeView({ purpose: 'goal', purpose_auto: false }), { label: '建模目标依据', auto: false });
});

test('默认选中被引用最多的资料；修订横幅取最近上传的那份', () => {
  const groups = [
    { key: 'excel', items: [{ path: 'a.xlsx', referenced: 0 }] },
    { key: 'others', items: [{ path: 'b.docx', referenced: 3 }, { path: 'c.docx', referenced: 16 }] },
  ];
  assert.equal(pickDefault(groups), 'c.docx');
  assert.equal(pickDefault([{ key: 'excel', items: [{ path: 'a.xlsx' }] }]), 'a.xlsx');
  assert.equal(pickDefault([]), null);
});

test('跳转参数：查看原文的锚点（含百分号编码）、上传文本版的路径', () => {
  assert.deepEqual(parseFileAnchor(`file:${R02}#p:9.2`), { path: R02, fragment: 'p:9.2' });
  assert.deepEqual(parseFileAnchor(`file:${encodeURIComponent('resource/others/a#b.docx')}#p:1`), { path: 'resource/others/a#b.docx', fragment: 'p:1' });
  assert.equal(parseFileAnchor(`${R02}#p:9.2`), null, '不带 file: 的不是资料锚点');
  assert.deepEqual(routeTarget({ anchor: `file:${R02}#p:9.2` }), { kind: 'file', path: R02, fragment: 'p:9.2', source: true });
  assert.deepEqual(routeTarget({ path: 'resource/others/07-门店C特殊需求.pdf' }), { kind: 'file', path: 'resource/others/07-门店C特殊需求.pdf', fragment: null });
  assert.equal(routeTarget({}), null);
});

test('片段：文档块与表格单元格两种写法；左侧出处编号', () => {
  assert.deepEqual(parseFragment('p:9.2'), { type: 'block', anchor: 'p:9.2' });
  assert.deepEqual(parseFragment('sheet:品类/cell:D1'), { type: 'cell', sheet: '品类', cell: 'D1' });
  assert.deepEqual(parseFragment('sheet:Sheet1!a12'), { type: 'cell', sheet: 'Sheet1', cell: 'A12' });
  assert.equal(parseFragment(''), null);
  assert.equal(blockLabel('p:9.2'), '§9.2');
  assert.equal(blockLabel('L12'), 'L12');
  assert.equal(blockLabel('table:1/row:2/cell:3'), '表 1 · 行 2 · 格 3');
  assert.equal(blockLabel('h:9'), '');
});

test('正文开头和编号重复时去掉，高亮跟着平移；按高亮切段', () => {
  const text = '9.2 门店申请与标准不一致时，门店申请严于标准的按门店执行。';
  const start = text.indexOf('门店申请严于');
  const b = blockBody({ anchor: 'p:9.2', text, highlights: [{ start, end: start + 6 }] });
  assert.ok(b.text.startsWith('门店申请与标准'));
  assert.equal(b.text.slice(b.highlights[0].start, b.highlights[0].end), '门店申请严于');
  assert.equal(blockBody({ anchor: 'p:6', text: '各品类安全库存按适用标准逐类取值。' }).text, '各品类安全库存按适用标准逐类取值。');
  assert.deepEqual(splitHighlights('abcdef', [{ start: 1, end: 3 }]), [{ t: 'a', hit: false }, { t: 'bc', hit: true }, { t: 'def', hit: false }]);
  assert.deepEqual(splitHighlights('abc', []), [{ t: 'abc', hit: false }]);
});

test('读到哪的 chip', () => {
  assert.deepEqual(coverageChip({ read_sections: '第 1–12 章', read_pct: 100, complete: true }), { tone: 'teal', icon: 'check', text: '第 1–12 章已读全' });
  assert.deepEqual(coverageChip({ read_sections: '读到 §10 · 还剩 §10–§12', read_pct: 78, complete: false }), { tone: 'amber', icon: null, text: '读到 §10 · 还剩 §10–§12' });
  assert.equal(coverageChip({ read_sections: '', read_pct: 0, complete: false }), null);
  assert.equal(coverageChip(null), null);
});

test('上传校验：格式白名单、20 MB（文字同 develop）；替换要同类型并改成原名', () => {
  assert.equal(checkUpload('货架布置图.dwg', 10), '不支持文件“货架布置图.dwg”的类型');
  assert.equal(checkUpload('演示.pptx', 10), '不支持文件“演示.pptx”的类型');
  assert.equal(checkUpload('a.xlsx', 21 * 1024 * 1024), '文件“a.xlsx”超过 20 MB，无法上传');
  assert.equal(checkUpload('a.DOCX', 1024), null);
  assert.deepEqual(replaceName({ name: '05-门店清单.xlsx' }, '新版.xlsx'), { name: '05-门店清单.xlsx' });
  assert.match(replaceName({ name: '05-门店清单.xlsx' }, '新版.csv').error, /\.xlsx/);
});

test('被引用：行文字、出处数、删除弹窗的那句话、点了去哪', () => {
  const items = [
    { target: { kind: 'operation', id: 'operation:overage_and_hint', key: 'overage_and_hint', name: '超量与审批提示' }, location: '决策依据', anchors: ['p:4'] },
    { target: { type: 'trace.scenario', key: 's-05', title: '区域统一配额取值' }, location: '', anchors: ['p:4'] },
  ];
  const rows = refRows(items);
  assert.equal(rows[0].sub, '操作 · 决策依据');
  assert.equal(rows[0].chip, '§4');
  assert.equal(rows[1].sub, '场景');
  assert.equal(refCount(items), 2);
  assert.equal(refSentence(items), '2 处出处引用了它：操作「超量与审批提示」的决策依据、场景「区域统一配额取值」。删除后这些出处会变成「来源无法核验」。');
  assert.equal(refSentence([]), '');
  assert.deepEqual(refTarget(items[0].target), { page: 'studio-table', query: { entry: 'operation:overage_and_hint' } });
  assert.deepEqual(refTarget({ type: 'diagram.flowchart', key: 'overage-flow' }), { page: 'studio-table', query: { entry: 'flowchart:overage-flow' } });
  assert.deepEqual(refTarget(items[1].target), { page: 'scenarios', query: { key: 's-05' } });
});

test('表格：列字母表头、行号从 1 起，合并单元格与整块高亮（同 develop ExcelView）；单元格位置；CSV 原样', () => {
  assert.equal(columnLetter(0), 'A');
  assert.equal(columnLetter(26), 'AA');
  const v = sheetGrid({ rows: [['商品', '品类'], ['牛奶', '乳制品'], ['酸奶']] });
  assert.deepEqual(v.letters, ['A', 'B']);
  assert.deepEqual(v.rows.map((r) => [r.n, r.cells.map((c) => c.value)]), [[1, ['商品', '品类']], [2, ['牛奶', '乳制品']], [3, ['酸奶', '']]]);
  const merged = sheetGrid({ rows: [['标题', ''], ['1', '2']], merges: [{ row: 0, col: 0, rowspan: 1, colspan: 2 }] }, { row: 0, col: 1 });
  assert.deepEqual(merged.rows[0].cells.map((c) => [c.value, c.colSpan, c.hit]), [['标题', 2, true]]);
  assert.equal(merged.rows[1].cells[0].numeric, true);
  const data = { sheets: [{ name: '规则', rows: [['a', 'b']], truncated: false }], sheet_truncated: false };
  assert.deepEqual(locateSheetCell(data, 'sheet:规则!B1'), { index: 0, cell: { row: 0, col: 1 } });
  assert.equal(locateSheetCell(data, 'sheet:规则!C9').message, '规则!C9 在当前文件中已不存在（资料可能已被替换）');
  assert.equal(locateSheetCell(data, 'sheet:别的!A1').message, '工作表「别的」已不存在（资料可能已被替换）');
  assert.equal(locateSheetCell({ ...data, sheets: [{ ...data.sheets[0], truncated: true }] }, 'sheet:规则!C9').problem, 'range');
  assert.deepEqual(cellPos('D1'), { row: 1, col: 3 });
  assert.deepEqual(cellPos('AA10'), { row: 10, col: 26 });
  assert.equal(csvText([['a', 'b,c'], ['"q"', '']]), 'a,"b,c"\n"""q""",');
});

test('drawio：读出节点和连线，连线裁到节点边上（菱形到顶点），压缩存储交给官方查看器', () => {
  const model = parseDrawio(DRAWIO[RES_PATHS.flow]);
  assert.equal(model.compressed, false);
  assert.equal(model.nodes.length, 6);
  assert.equal(model.edges.length, 5);
  assert.equal(model.nodes.find((n) => n.id === 'n3').label, '在受理范围?');
  const lay = drawioLayout(model);
  const no = lay.edges.find((e) => e.id === 'e4');
  assert.equal(no.d, 'M224.0 130.0 L288.0 130.0 L288.0 174.0');
  assert.equal(no.label.text, '否');
  assert.equal(lay.nodes.find((n) => n.id === 'n1').rx, 13);
  assert.equal(lay.nodes.find((n) => n.id === 'n3').shape, 'diamond');
  assert.equal(parseDrawio('<mxfile><diagram id="a">7ZZNb9swDIZ/</diagram></mxfile>').compressed, true);
  assert.equal(drawioLayout(parseDrawio('<mxfile/>')).nodes.length, 0);
});

// ---------- mock ----------
function fakeServer(stage = 'l2', sc = {}) {
  const routes = [];
  const events = [];
  const tasks = [];
  let n = 0;
  const flow = stages[stage]?.flow ? stages[stage].flow(F.flow()) : F.flow();
  const card = { id: F.MAIN_PID, version_label: 'v0.12' };
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => ({ status, body }),
    err: (status, code, message, details) => ({ status, body: { code, message, details } }),
    db: { uploads: new Map() },
    sc,
    uid: () => String((n += 1)),
    cardOf: () => card,
    convsOf: () => [{ id: 'c7a1e2f03b44' }],
    flowOf: () => flow,
    stageOf: () => stage,
    emitConv: (pid, cid, type, data) => events.push({ type, data }),
    emitProj: (pid, type, data) => events.push({ type, data }),
    runReply: () => {},
    putTask: (pid, t) => tasks.push(t),
    commandHooks: [],
  };
  registerMaterials(ctx);
  registerResources(ctx);
  const call = (method, path, { body = {}, query = '' } = {}) => {
    const full = `/api/v1/projects/${F.MAIN_PID}${path}`;
    for (const r of routes.filter((x) => x.method === method && x.re.test(full))) {
      const res = r.handler({ m: full.match(r.re), body, query: new URLSearchParams(query), form: null });
      if (res !== undefined) return res;
    }
    return { status: 404 };
  };
  return { call, events, tasks, ctx, card };
}
const all = (r) => r.body.groups.flatMap((g) => g.items);

test('R-01：按流程图 / 表格 / 其他资料分组，含修订通知和扫描件', () => {
  const s = fakeServer();
  const r = s.call('GET', '/resources');
  assert.deepEqual(r.body.groups.map((g) => [g.key, g.items.length]), [['diagrams', 2], ['excel', 5], ['others', 8]]); // excel 含一份数据源资料（ADR-0104）
  const names = all(r).map((x) => x.name);
  assert.ok(names.includes('09-修订通知.docx') && names.includes('07-门店C特殊需求.pdf'));
  const rev = all(r).find((x) => x.name === '09-修订通知.docx');
  assert.equal(rev.is_revision, false, '同后端：is_revision 恒为 false');
  assert.equal(rev.revision_task, null, '后端从不给 revision_task');
  assert.ok(!('scanned' in rev), '不把 mock 自用字段漏给页面');
  assert.equal(all(r).find((x) => x.path === R02).referenced, 13);
  assert.equal(r.body.purposes.length, 5);
  assert.equal('feedback_group' in r.body, false);
});

test('读资料中只有最早那 7 份、没有引用；还没传资料时为空', () => {
  const s = fakeServer('materials');
  const r = s.call('GET', '/resources');
  assert.equal(all(r).length, 7);
  assert.equal(all(r).find((x) => x.path === R02).coverage.label, '读到 §10 · 还剩 §10–§12');
  assert.equal(s.call('GET', '/resources/references', { query: `path=${R02}` }).body.total, 0);
  assert.equal(all(fakeServer('materials-empty').call('GET', '/resources')).length, 0);
});

test('R-02：修订资料收下不处理（同后端：不另外发增量更新命令、不挂变更点）；同名替换保留原路径和用途；格式不对 415', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const s = fakeServer();
    s.ctx.db.uploads.set('up-1', { filename: '13-配额调整通知.docx', size: 2048 });
    s.ctx.db.uploads.set('up-2', { filename: '14-补充说明.docx', size: 1024 });
    const a = s.call('POST', '/resources', { body: { upload_id: 'up-1', is_revision: true, batch_id: 'b1' } });
    const b = s.call('POST', '/resources', { body: { upload_id: 'up-2', is_revision: true, batch_id: 'b1' } });
    assert.equal(a.status, 201);
    assert.equal(a.body.material.path, 'resource/others/13-配额调整通知.docx');
    assert.equal(a.body.version_label, 'v0.13');
    assert.equal(a.body.command_task, null);
    assert.equal(b.body.command_task, null);
    assert.ok(s.events.some((e) => e.type === 'workspace.changed' && e.data.source === 'web'));
    assert.ok(!s.events.some((e) => e.type === 'message.user'), '不往对话里发命令');
    mock.timers.tick(3000);
    assert.equal(s.tasks.length, 0);
    const rev = all(s.call('GET', '/resources')).find((x) => x.name === '13-配额调整通知.docx');
    assert.equal(rev.revision_task, null);
    assert.ok(s.call('GET', '/materials/map').body.items.some((x) => x.name === '13-配额调整通知.docx'), '资料地图同步加一份');

    s.ctx.db.uploads.set('up-3', { filename: '06-商品品类表.xlsx', size: 4096 });
    const c = s.call('POST', '/resources', { body: { upload_id: 'up-3' } });
    assert.equal(c.body.replaced, true);
    assert.equal(c.body.material.path, 'resource/schema/06-商品品类表.xlsx');
    assert.equal(c.body.material.purpose, 'schema');
    s.ctx.db.uploads.set('up-4', { filename: '货架布置图.dwg', size: 10 });
    assert.equal(s.call('POST', '/resources', { body: { upload_id: 'up-4' } }).body.code, 'unsupported_media_type');
    assert.equal(s.call('POST', '/resources', { body: { upload_id: 'gone' } }).body.code, 'upload_expired');
  } finally {
    mock.timers.reset();
  }
});

test('R-03：资料地图里没有的资料（流程图）也能改用途；分析开始过就带重读任务', () => {
  const s = fakeServer();
  const r = s.call('PATCH', '/resources/purpose', { body: { path: RES_PATHS.flow, purpose: 'goal' } });
  assert.equal(r.status, 200);
  assert.equal(r.body.material.purpose, 'goal');
  assert.equal(r.body.material.purpose_auto, false);
  assert.ok(r.body.reread_task);
  assert.equal(all(s.call('GET', '/resources')).find((x) => x.path === RES_PATHS.flow).purpose, 'goal');
  const m = s.call('PATCH', '/resources/purpose', { body: { path: R03B, purpose: 'term' } });
  assert.equal(s.call('GET', '/materials/map').body.items.find((x) => x.path === R03B).purpose, 'term');
  assert.equal(m.body.version_label, 'v0.14');
});

test('R-04 / R-05：删除前查引用，删除保存为新版本、出处变为无法核验，资料地图同步去掉', () => {
  const s = fakeServer();
  const refs = s.call('GET', '/resources/references', { query: `path=${R03B}` }).body;
  assert.equal(refs.total, 2);
  assert.ok(refSentence(refs.items).startsWith('2 处出处引用了它'));
  const r = s.call('DELETE', '/resources', { query: `path=${R03B}` });
  assert.equal(r.body.broken_references, 2);
  assert.equal(r.body.version_label, 'v0.13');
  assert.ok(!all(s.call('GET', '/resources')).some((x) => x.path === R03B));
  assert.ok(!s.call('GET', '/materials/map').body.items.some((x) => x.path === R03B));
  assert.equal(s.call('DELETE', '/resources', { query: `path=${R03B}` }).body.code, 'file_not_found');
  assert.equal(s.call('DELETE', '/resources', { query: 'path=resource/systems/x.json' }).body.code, 'path_forbidden');
  const two = s.call('GET', '/resources/references', { query: `path=${R02}` }).body;
  assert.equal(refCount(two.items), all(s.call('GET', '/resources')).find((x) => x.path === R02).referenced, '被引用次数 = 出处数');
});

test('R-06：不带 around 时高亮所有出处，带 around 只高亮那一段；扫描件没有文字', () => {
  const s = fakeServer();
  const full = s.call('GET', '/resources/preview', { query: `path=${R02}` }).body;
  assert.ok(full.blocks.filter((b) => b.highlights).length > 3);
  assert.deepEqual(full.coverage, { read_sections: '第 1–12 章', read_pct: 100, complete: true });
  const at = s.call('GET', '/resources/preview', { query: `path=${R02}&around=p:9.2` }).body;
  const hit = at.blocks.filter((b) => b.highlights);
  assert.deepEqual(hit.map((b) => b.anchor), ['p:9.2']);
  assert.equal(hit[0].text.slice(hit[0].highlights[0].start, hit[0].highlights[0].end), '门店申请严于标准的按门店执行');
  assert.ok(!at.blocks.some((b) => b.anchor === 'p:7.3'), '§7.3 原文里没有（核验「来源无法核验」那条）');
  const scan = s.call('GET', '/resources/preview', { query: 'path=resource/others/07-门店C特殊需求.pdf' }).body;
  assert.deepEqual(scan.blocks, []);
  assert.equal(scan.coverage.complete, false);
});

test('F-05 表格预览、F-04 原文件（HTML 带沙箱头，下载带文件名）；不是资料区的路径交给别人', async () => {
  const s = fakeServer();
  const t = s.call('GET', '/files/table-preview', { query: 'path=resource/schema/06-商品品类表.xlsx' }).body;
  assert.deepEqual(t.sheets.map((x) => x.name), ['品类', '到货周期', '适用标准']);
  assert.equal(t.sheets[0].rows[0][3], '到货周期');
  assert.equal(t.sheets[0].truncated, true);
  assert.equal(s.call('GET', '/files/table-preview', { query: 'path=resource/excel/10-补货台账导出.csv' }).body.format, 'csv');
  assert.equal(s.call('GET', '/files/table-preview', { query: `path=${R02}` }).status, 415);
  const html = s.call('GET', '/files/raw', { query: `path=${RES_PATHS.report}&download=true` });
  assert.match(html.headers.get('Content-Security-Policy'), /^sandbox/);
  assert.match(html.headers.get('Content-Disposition'), /^attachment; filename="/);
  assert.match(await html.text(), /补货周报/);
  assert.equal(s.call('GET', '/files/raw', { query: 'path=ontology/objects/store.md' }).status, 404);
  assert.ok(s.ctx.db.files.get(RES_PATHS.flow).content.startsWith('<mxfile'), 'drawio 放进 db.files，F-02 / F-03 读写');
});

test('页面命令：让助手读完的命令跑完后资料标为读完；?mock=task:resources 有一个进行中的重读', () => {
  const s = fakeServer('materials');
  const hook = s.ctx.commandHooks.find((h) => h.match({ origin: { page: 'resources' }, command: '/ontology-research', args: { path: R02 } }));
  assert.ok(hook);
  hook.finish(F.MAIN_PID, { args: { path: R02 } });
  assert.equal(s.call('GET', '/materials/map').body.items.find((x) => x.path === R02).read_pct, 100);
  assert.equal(taskSeeds.resources().origin.page, 'resources');
});

test('md / txt 资料（R-01 preview=text）按文档预览：R-06 的块、锚点定位', async () => {
  const { isDocPreview } = await import('../src/pages/resources/resourcesModel.js');
  assert.equal(isDocPreview({ preview: 'text' }), true);
  assert.equal(isDocPreview({ preview: 'doc' }), true);
  assert.equal(isDocPreview({ preview: 'xls' }), false);
  assert.equal(isDocPreview(null), false);
});

// ---------- 文本版（ADR-0073） ----------

test('文本版：列表行只标没转好或转不了的；PDF 的预览就是文本，Word、Excel 多一个「文本」页签', () => {
  assert.equal(textChip({ text: { state: 'converted' } }), null);
  assert.equal(textChip({ text: { state: 'native' } }), null);
  assert.equal(textChip({ text: null }), null);
  assert.deepEqual(textChip({ text: { state: 'waiting' } }), { text: '等待转换', tone: 'gray', icon: 'clock', title: undefined });
  assert.deepEqual(textChip({ text: { state: 'failed', reason: '转换超时' } }), { text: '转换失败', tone: 'red', icon: 'alert', title: '转换超时' });
  assert.equal(textChip({ text: { state: 'unsupported' } }).text, '暂不支持');
  assert.equal(textChip({ text: { state: 'converting' } }).text, '转换中');
  assert.equal(hasTextTab({ kind: 'doc', text: { state: 'converted' } }), true);
  assert.equal(hasTextTab({ kind: 'xls', text: { state: 'failed' } }), true);
  assert.equal(hasTextTab({ kind: 'pdf', text: { state: 'converted' } }), false);
  assert.equal(hasTextTab({ kind: 'text', text: { state: 'native' } }), false);
  assert.equal(hasTextTab({ kind: 'drawio', text: null }), false);
});

test('文本版按位置标记切段：页码、工作表各成一段，标记前的空白不出空段', () => {
  const md = '<!-- 第 1 页 -->\n甲\n\n<!-- 第 2 页 -->\n乙\n';
  assert.deepEqual(textSections(md), [{ label: '第 1 页', body: '甲' }, { label: '第 2 页', body: '乙' }]);
  assert.deepEqual(textSections('<!-- 工作表：客户 -->\n## 客户\n\n| a |\n|---|'), [{ label: '工作表 客户', body: '## 客户\n\n| a |\n|---|' }]);
  assert.deepEqual(textSections('# 标题\n\n正文'), [{ label: null, body: '# 标题\n\n正文' }]);
  assert.deepEqual(textSections(''), []);
});

test('mock R-02 → 后台转换 → EP-25；R-13 查看文本；R-14 重新转换；扫描件暂不支持', async () => {
  const s = fakeServer();
  const r = s.call('GET', '/resources');
  const scan = all(r).find((x) => x.name === '07-门店C特殊需求.pdf');
  assert.equal(scan.text.state, 'unsupported');
  assert.equal(scan.text.reason, '扫描件或图片型 PDF，暂不支持');
  const word = all(r).find((x) => x.name === '09-修订通知.docx');
  assert.equal(word.text.state, 'converted');
  assert.equal(all(r).find((x) => x.kind === 'drawio').text, null);
  const text = s.call('GET', '/resources/text', { query: `path=${encodeURIComponent(word.path)}` });
  assert.equal(text.status, 200);
  assert.ok(text.body.markdown.length > 0 && text.body.truncated === false);
  const re = s.call('POST', '/resources/convert', { body: { path: word.path } });
  assert.equal(re.status, 202);
  assert.equal(re.body.material.text.state, 'converting');
  const native = all(r).find((x) => x.text?.state === 'native');
  assert.equal(s.call('POST', '/resources/convert', { body: { path: native.path } }).status, 409);
  await new Promise((done) => setTimeout(done, 1300));
  const pushed = s.events.filter((e) => e.type === 'materials.text.updated' && e.data.path === word.path);
  assert.deepEqual(pushed.map((e) => e.data.text.state), ['converting', 'converted']);
});
