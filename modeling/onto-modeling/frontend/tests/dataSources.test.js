// 数据源（ADR-0103，契约 3.19a DS-01～DS-04、PL-28、PL-29）：资料页的纯逻辑、插件中心的信任状态、mock 接口
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { SOURCE_CONNECTORS, SOURCE_PATH } from '../src/api/mock/resources-data.js';
import { register as registerMaterials } from '../src/api/mock/routes-materials.js';
import { register as registerResources } from '../src/api/mock/routes-resources.js';
import { cardFoot, trustChip } from '../src/pages/plugins/catalogModel.js';
import { heroActions, untrustConfirm } from '../src/pages/plugins/detailModel.js';
import { fileIcon, hasTextTab, metaText, scaleText, textChip } from '../src/pages/resources/resourcesModel.js';
import {
  connectorOptions, connectorsReady, editTarget, fieldErrorsFrom, initialValues, nameError, secretPlaceholder, sourceActions,
  splitValues, testView, validateSource,
} from '../src/pages/resources/sourceModel.js';

const FIELDS = SOURCE_CONNECTORS[0].fields;
const sourceMaterial = (o = {}) => ({
  path: SOURCE_PATH, name: '门店业务库.datasource', title: '门店业务库', kind: 'source', type_label: '数据源', sheets: 3, size: 512,
  uploaded_at: '2026-11-19T02:05:00Z', text: { state: 'converted', reason: null },
  source: {
    connector: { id: 'db-mysql', label: 'MySQL', version: '0.1.0', available: true },
    config: { host: 'db.example.invalid', port: 3306, database: 'store_ops', user: 'reader', tables: '' },
    fields: FIELDS, secret_fields: ['password'],
  },
  ...o,
});

// ---------- 表单 ----------
test('初值：接入用字段默认值，修改用资料里的配置；密钥一律留空', () => {
  assert.deepEqual(initialValues(FIELDS), { host: '', port: '3306', database: '', user: '', password: '', tables: '' });
  const edit = initialValues(FIELDS, { host: 'db.example.invalid', port: 3307, database: 'shop', user: 'reader', tables: 'orders' });
  assert.deepEqual(edit, { host: 'db.example.invalid', port: '3307', database: 'shop', user: 'reader', password: '', tables: 'orders' });
});

test('校验：名称与必填；端口是 1～65535 的整数；修改时密钥可以空着', () => {
  const values = { host: 'db.example.invalid', port: '3306', database: 'shop', user: 'reader', password: 'x', tables: '' };
  assert.deepEqual(validateSource({ name: '订单库', fields: FIELDS, values }), {});
  assert.deepEqual(validateSource({ name: '', fields: FIELDS, values: { ...values, host: ' ', password: '' } }), { name: 'required', host: 'required', password: 'required' });
  assert.deepEqual(validateSource({ name: 'a/b', fields: FIELDS, values: { ...values, port: '70000' } }), { name: 'invalid', port: 'invalid' });
  assert.deepEqual(validateSource({ name: '', fields: FIELDS, values: { ...values, password: '' }, editing: true }), {});
  assert.equal(nameError('.隐藏'), 'invalid');
  assert.equal(nameError('库 1'), null);
  const sel = [{ key: 'mode', type: 'select', required: true, options: [{ value: 'a', label: 'A' }] }];
  assert.deepEqual(validateSource({ name: 'x', fields: sel, values: { mode: 'b' } }), { mode: 'invalid' });
});

test('拆请求体：非密钥字段全进 config（数字转整数，空数字不放），只带填了的密钥', () => {
  const values = { host: ' db.example.invalid ', port: '3306', database: 'shop', user: 'reader', password: '', tables: '' };
  assert.deepEqual(splitValues(FIELDS, values), {
    config: { host: 'db.example.invalid', port: 3306, database: 'shop', user: 'reader', tables: '' }, secrets: {},
  });
  assert.deepEqual(splitValues(FIELDS, { ...values, port: '', password: 'p w' }).secrets, { password: 'p w' });
  assert.equal('port' in splitValues(FIELDS, { ...values, port: '' }).config, false);
});

test('服务端错误标到字段：invalid_argument 的 fields、file_exists 标名称；别的整条显示', () => {
  assert.deepEqual(fieldErrorsFrom({ code: 'invalid_argument', details: { fields: { host: 'required', port: 'enum' } } }), { host: 'required', port: 'invalid' });
  assert.deepEqual(fieldErrorsFrom({ code: 'file_exists', details: { path: SOURCE_PATH } }), { name: 'exists' });
  assert.equal(fieldErrorsFrom({ code: 'gateway_unavailable' }), null);
  assert.equal(fieldErrorsFrom({ code: 'invalid_argument', details: null }), null);
});

test('测试连接结果：成功、失败（原因在 error 或没通过的那一项）', () => {
  const ok = testView({ ok: true, checks: [{ name: '连接', ok: true, detail: 'MySQL 8.0' }], error: null });
  assert.deepEqual([ok.ok, ok.title, ok.error, ok.checks.length], [true, '连接成功', null, 1]);
  const bad = testView({ ok: false, checks: [{ name: '只读账号', ok: false, detail: '账号有写权限' }], error: { code: 'account_not_readonly', message: '账号能写，换只读账号' } });
  assert.deepEqual([bad.title, bad.error, bad.checks[0].ok], ['连接失败', '账号能写，换只读账号', false]);
  assert.equal(testView(null), null);
});

test('连接器：网关不可用或没有连接器时入口不出现', () => {
  assert.equal(connectorsReady({ gateway: { status: 'ok' }, items: SOURCE_CONNECTORS }), true);
  assert.equal(connectorsReady({ gateway: { status: 'down' }, items: [] }), false);
  assert.equal(connectorsReady({ gateway: { status: 'ok' }, items: [] }), false);
  assert.equal(connectorsReady(null), false);
  assert.deepEqual(connectorOptions(SOURCE_CONNECTORS), [{ value: 'db-mysql', label: 'MySQL' }, { value: 'db-postgres', label: 'PostgreSQL' }]);
});

// ---------- 资料行与预览 ----------
test('数据源资料：图标、表数、「接入」，状态叫采集中 / 采集失败，没有单独的文本页签', () => {
  const m = sourceMaterial();
  assert.deepEqual(fileIcon('source'), { cls: 'x', icon: 'db' });
  assert.equal(scaleText(m), '3 张表');
  assert.equal(scaleText({ ...m, sheets: 0 }), '');
  assert.match(metaText(m, Date.parse('2026-11-19T03:00:00Z')), /^数据源 · 3 张表 · 11-19 \d\d:05 接入$/);
  assert.equal(metaText({ ...m, sheets: 0 }).split(' · ').length, 2);
  assert.equal(textChip({ ...m, text: { state: 'converting' } }).text, '采集中');
  assert.equal(textChip({ ...m, text: { state: 'waiting' } }).text, '等待采集');
  assert.equal(textChip({ ...m, text: { state: 'failed', reason: '连不上' } }).text, '采集失败');
  assert.equal(textChip({ kind: 'pdf', text: { state: 'failed' } }).text, '转换失败');
  assert.equal(textChip(m), null);
  assert.equal(hasTextTab(m), false);
});

test('数据源能做什么：连接器不在网关里时不能重新采集、测试，只能修改连接', () => {
  assert.deepEqual(sourceActions(sourceMaterial()), { available: true, recollect: true, test: true, edit: true, state: null });
  const gone = sourceMaterial({ source: { ...sourceMaterial().source, connector: { id: 'db-mysql', label: 'MySQL', version: null, available: false } } });
  assert.deepEqual(sourceActions(gone), { available: false, recollect: false, test: false, edit: true, state: '连接器不可用' });
  assert.equal(sourceActions({ kind: 'pdf' }), null);
});

test('修改连接：预填配置，密钥留空，已存的密钥占位写「已保存」', () => {
  const t = editTarget(sourceMaterial());
  assert.equal(t.path, SOURCE_PATH);
  assert.equal(t.name, '门店业务库');
  assert.equal(t.values.port, '3306');
  assert.equal(t.values.password, '');
  const pwd = FIELDS.find((f) => f.key === 'password');
  assert.equal(secretPlaceholder(pwd, t.savedSecrets), '已保存');
  assert.equal(secretPlaceholder(pwd, []), '');
  assert.equal(editTarget({ kind: 'source', source: null }), null);
});

// ---------- 插件中心：信任 ----------
const provider = (trust, o = {}) => ({ name: 'src-x', label: '示例连接器', category: 'provider', version: '0.1.0', project_status: 'platform_installed', trust, ...o });

test('信任状态：内置不标；未信任、已信任、信任了没装上', () => {
  assert.equal(trustChip(provider({ state: 'builtin', installed: true })), null);
  assert.equal(trustChip(provider(null)), null);
  assert.equal(trustChip(provider({ state: 'untrusted', installed: null })).text, '未信任');
  assert.equal(trustChip(provider({ state: 'trusted', installed: true })).text, '已信任');
  assert.equal(trustChip(provider({ state: 'trusted', installed: null })).text, '已信任');
  assert.equal(trustChip(provider({ state: 'trusted', installed: false })).text, '未装上');
  assert.equal(cardFoot(provider({ state: 'untrusted', installed: null })).left.text, '未信任');
  assert.equal(cardFoot(provider({ state: 'builtin', installed: true })).left.text, '平台已安装');
});

test('详情页：管理员信任 / 撤销信任，别人只看状态；撤销前确认', () => {
  assert.equal(heroActions(provider({ state: 'untrusted', installed: null }), { admin: true }).primary.action, 'trust');
  assert.equal(heroActions(provider({ state: 'trusted', installed: true }), { admin: true }).primary.action, 'untrust');
  assert.deepEqual(heroActions(provider({ state: 'untrusted', installed: null }), { admin: false }), {});
  assert.equal(heroActions(provider({ state: 'builtin', installed: true }), { admin: true }).primary.label, '平台已安装');
  assert.match(untrustConfirm(provider(null)).title, /^撤销信任示例连接器？$/);
});

// ---------- mock：DS-01～DS-04、R-14 ----------
function fakeServer(sc = {}) {
  const routes = [];
  const events = [];
  let n = 0;
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
    flowOf: () => F.flow(),
    stageOf: () => 'l2',
    emitConv: () => {},
    emitProj: (pid, type, data) => events.push({ type, data }),
    runReply: () => {},
    putTask: () => {},
    commandHooks: [],
  };
  registerMaterials(ctx);
  registerResources(ctx);
  const call = (method, path, body = {}) => {
    const full = `/api/v1${path}`;
    for (const r of routes.filter((x) => x.method === method && x.re.test(full))) {
      const res = r.handler({ m: full.match(r.re), body, query: new URLSearchParams(), form: null });
      if (res !== undefined) return res;
    }
    return { status: 404 };
  };
  return { call, events };
}
const PP = `/projects/${F.MAIN_PID}`;
const CONFIG = { host: 'db.example.invalid', port: 3306, database: 'shop', user: 'reader', tables: '' };

test('mock DS-01：网关正常时列两个连接器，?mock=sources:down 时为空', () => {
  assert.deepEqual(fakeServer().call('GET', '/source-connectors').body.items.map((c) => c.id), ['db-mysql', 'db-postgres']);
  assert.deepEqual(fakeServer({ sources: 'down' }).call('GET', '/source-connectors').body, { gateway: { status: 'down' }, items: [] });
});

test('mock DS-02：接入后资料里多一份数据源（不回凭据），重名、缺字段、网关不可用分别报错', () => {
  const s = fakeServer();
  const r = s.call('POST', `${PP}/sources`, { name: '订单库', connector: 'db-mysql', config: CONFIG, secrets: { password: 'x' } });
  assert.equal(r.status, 201);
  const m = r.body.material;
  assert.deepEqual([m.path, m.kind, m.type_label, m.group, m.text.state], ['resource/excel/订单库.datasource', 'source', '数据源', 'excel', 'converting']);
  assert.deepEqual(m.source.secret_fields, ['password']);
  assert.ok(!JSON.stringify(m).includes('"x"'), '不回凭据');
  assert.ok(s.call('GET', `${PP}/resources`).body.groups.flatMap((g) => g.items).some((x) => x.path === m.path));
  const again = s.call('POST', `${PP}/sources`, { name: '订单库', connector: 'db-mysql', config: CONFIG, secrets: { password: 'x' } });
  assert.deepEqual([again.status, again.body.code], [409, 'file_exists']);
  const missing = s.call('POST', `${PP}/sources`, { name: '库2', connector: 'db-mysql', config: { ...CONFIG, host: '' }, secrets: {} });
  assert.deepEqual([missing.status, missing.body.details.fields], [400, { host: 'required', password: 'required' }]);
  assert.equal(s.call('POST', `${PP}/sources`, { name: '库3', connector: 'nope', config: CONFIG, secrets: {} }).body.code, 'connector_not_found');
  assert.equal(fakeServer({ sources: 'down' }).call('POST', `${PP}/sources`, { name: '库4', connector: 'db-mysql', config: CONFIG, secrets: { password: 'x' } }).status, 503);
});

test('mock DS-03：修改连接整份换 config、密钥可不填，改完重新采集；不是数据源报 not_a_source', () => {
  const s = fakeServer();
  const r = s.call('PATCH', `${PP}/sources`, { path: SOURCE_PATH, config: { ...CONFIG, database: 'store_v2' } });
  assert.equal(r.status, 200);
  assert.equal(r.body.material.source.config.database, 'store_v2');
  assert.equal(r.body.material.text.state, 'converting');
  assert.ok(s.events.some((e) => e.type === 'materials.text.updated' && e.data.path === SOURCE_PATH));
  const doc = s.call('GET', `${PP}/resources`).body.groups.flatMap((g) => g.items).find((x) => x.kind === 'doc');
  assert.equal(s.call('PATCH', `${PP}/sources`, { path: doc.path, config: CONFIG }).body.code, 'not_a_source');
});

test('mock DS-04：成功、连不上、账号能写；带 path 时密钥可以不填', () => {
  const s = fakeServer();
  const ok = s.call('POST', `${PP}/sources/test`, { connector: 'db-mysql', config: CONFIG, secrets: { password: 'x' } });
  assert.deepEqual([ok.status, ok.body.ok, ok.body.error], [200, true, null]);
  const down = s.call('POST', `${PP}/sources/test`, { connector: 'db-mysql', config: { ...CONFIG, host: 'bad.example.invalid' }, secrets: { password: 'x' } });
  assert.deepEqual([down.body.ok, down.body.error.code], [false, 'connect_failed']);
  const writer = s.call('POST', `${PP}/sources/test`, { connector: 'db-mysql', config: { ...CONFIG, user: 'writer' }, secrets: { password: 'x' } });
  assert.deepEqual([writer.body.ok, writer.body.error.code], [false, 'account_not_readonly']);
  assert.equal(s.call('POST', `${PP}/sources/test`, { connector: 'db-mysql', config: CONFIG, path: SOURCE_PATH }).body.ok, true);
});

test('mock R-14：数据源资料重新采集', () => {
  const s = fakeServer();
  const r = s.call('POST', `${PP}/resources/convert`, { path: SOURCE_PATH });
  assert.deepEqual([r.status, r.body.material.text.state], [202, 'converting']);
});
