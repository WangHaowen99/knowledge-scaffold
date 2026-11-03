// F4d：Studio 填证据改成「先选资料、再选位置」——纯逻辑（studioModel.js）与 mock 的 R-15
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register as registerMaterials, stages } from '../src/api/mock/routes-materials.js';
import { register as registerResources } from '../src/api/mock/routes-resources.js';
import { evidenceMaterials, evidencePickLabel, positionGroups, resolutionLabel } from '../src/pages/studio/studioModel.js';

const mat = (name, text, extra = {}) => ({ path: `resource/others/${name}`, name, title: name.replace(/\.[^.]+$/, ''), type_label: 'Word', text, ...extra });
const TEXT = (state, has_text = state === 'converted' || state === 'native') => ({ state, has_text });

test('能引用的资料：有文本的按名字排在前，正在转的排后面点不了；暂不支持、转换失败、流程图不列；按名字搜', () => {
  const groups = [
    { key: 'diagrams', items: [{ path: 'resource/diagrams/流程.drawio', name: '流程.drawio', text: null }] },
    { key: 'others', items: [
      mat('03-规则.docx', TEXT('converted')), mat('01-说明.md', TEXT('native'), { type_label: 'Markdown' }),
      mat('05-新规.pdf', TEXT('converting')), mat('04-等转.pdf', TEXT('waiting')),
      mat('06-扫描件.pdf', TEXT('unsupported')), mat('07-坏文件.docx', TEXT('failed')),
    ] },
  ];
  assert.deepEqual(evidenceMaterials(groups).map((m) => [m.title, m.ready]), [
    ['01-说明', true], ['03-规则', true], ['04-等转', false], ['05-新规', false],
  ]);
  assert.equal(evidenceMaterials(groups)[0].type, 'Markdown');
  assert.deepEqual(evidenceMaterials(groups, '规').map((m) => m.title), ['03-规则', '05-新规']);
  assert.deepEqual(evidenceMaterials([]), []);
});

test('位置按连续的页、小节分组；选中后的说法和 EV-29 解析出来的一样', () => {
  const items = [
    { anchor: 'a', location: '第 1 页', text: '一' }, { anchor: 'b', location: '第 1 页', text: '二' },
    { anchor: 'c', location: '第 2 页', text: '三' }, { anchor: 'd', location: '', text: '四' },
  ];
  assert.deepEqual(positionGroups(items).map((g) => [g.location, g.items.map((x) => x.anchor)]), [
    ['第 1 页', ['a', 'b']], ['第 2 页', ['c']], ['', ['d']],
  ]);
  assert.equal(evidencePickLabel('03-退款规范', '第 2 页'), '03 退款规范 第 2 页');
  assert.equal(evidencePickLabel('03-退款规范', '第 2 页'),
    resolutionLabel({ resolved: true, scheme: 'file', material: { name: '03-退款规范', path: 'resource/others/03-退款规范.pdf' }, location_label: '第 2 页' }));
  assert.equal(evidencePickLabel('门店清单', ''), '门店清单');
});

function fakeServer(stage = 'l2') {
  const routes = [];
  const flow = stages[stage]?.flow ? stages[stage].flow(F.flow()) : F.flow();
  let n = 0;
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => ({ status, body }),
    err: (status, code, message, details) => ({ status, body: { code, message, details } }),
    db: { uploads: new Map() },
    sc: {},
    uid: () => String((n += 1)),
    cardOf: () => ({ id: F.MAIN_PID, version_label: 'v0.12' }),
    convsOf: () => [{ id: 'c7a1e2f03b44' }],
    flowOf: () => flow,
    stageOf: () => stage,
    emitConv: () => {},
    emitProj: () => {},
    runReply: () => {},
    putTask: () => {},
    commandHooks: [],
  };
  registerMaterials(ctx);
  registerResources(ctx);
  return (path, query = '') => {
    const full = `/api/v1/projects/${F.MAIN_PID}${path}`;
    const route = routes.find((x) => x.method === 'GET' && x.re.test(full));
    return route ? route.handler({ m: full.match(route.re), body: {}, query: new URLSearchParams(query), form: null }) : { status: 404 };
  };
}

test('mock R-15：PDF 读文本版、按页列行；锚点指向文本版的行号，悬停能解析出同一句', () => {
  const get = fakeServer();
  const path = 'resource/others/04-商品陈列说明.pdf';
  const r = get('/resources/positions', `path=${encodeURIComponent(path)}`);
  assert.equal(r.status, 200);
  assert.equal(r.body.name, '04-商品陈列说明');
  assert.ok(r.body.readable && r.body.items.length > 0);
  const first = r.body.items[0];
  assert.match(first.anchor, /^file:resource\/text\/others\/04-商品陈列说明\.pdf\.md#L\d+$/);
  assert.equal(first.location, '第 1 页');
  assert.ok(!r.body.items.some((x) => /^<!--|^#|^\|?\s*-{3}/.test(x.text)));
  const resolved = F.ANCHOR_RESOLUTIONS[first.anchor];
  assert.equal(resolutionLabel(resolved), evidencePickLabel(r.body.name, first.location));
  // 搜索、分页
  const word = first.text.slice(0, 2);
  assert.ok(get('/resources/positions', `path=${encodeURIComponent(path)}&q=${encodeURIComponent(word)}`).body.items.every((x) => x.text.includes(word) || x.location.includes(word)));
  const page = get('/resources/positions', `path=${encodeURIComponent(path)}&limit=1`).body;
  assert.equal(page.items.length, 1);
  assert.equal(get('/resources/positions', `path=${encodeURIComponent(path)}&limit=1&cursor=${page.next_cursor}`).body.items[0].anchor, r.body.items[1].anchor);
});

test('mock R-15：扫描件没有文本版不可读；流程图 409；不存在 404', () => {
  const get = fakeServer();
  const scanned = get('/resources/positions', `path=${encodeURIComponent('resource/others/07-门店C特殊需求.pdf')}`);
  assert.deepEqual([scanned.status, scanned.body.readable, scanned.body.items], [200, false, []]);
  const csv = get('/resources/positions', `path=${encodeURIComponent('resource/excel/10-补货台账导出.csv')}`);
  assert.ok(csv.body.readable && csv.body.items.every((x) => x.anchor.startsWith('file:resource/excel/10-补货台账导出.csv#L')));
  const flow = get('/resources/positions', `path=${encodeURIComponent('resource/diagrams/门店补货流程.drawio')}`);
  assert.equal(flow.status, 409);
  assert.equal(get('/resources/positions', 'path=resource/others/none.md').status, 404);
});
