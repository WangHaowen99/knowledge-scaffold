// 映射：夹具是真实 REA 6.8.0 对 test/targets 下合成程序的输出（用 test/fixtures/trim.mjs 裁过）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { canonicalJson, recordHash } from '../src/anchors.mjs';
import { mapManagedMembers, mapManagedNative } from '../src/map-dotnet.mjs';
import { mapJavascript, resolveTemplate } from '../src/map-js.mjs';
import { mapPythonPyc, pycModuleName } from '../src/map-python.mjs';
import { RecordSet } from '../src/records.mjs';
import { httpMethod } from '../src/urls.mjs';

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url), 'utf8'));
const S = 'bin:crm-client/';

function build(fill) {
  const records = new RecordSet({ source: 'crm-client', provider: 'rea-static@0.1.0' });
  const info = fill(records);
  const finished = records.finish();
  const byId = new Map(finished.records.map((record) => [record.id, record]));
  const relations = finished.records.filter((record) => record.type === 'relation')
    .map((record) => `${record.from.slice(S.length)} ${record.name} ${record.to?.slice(S.length)}`);
  return { info, finished, byId, relations };
}

function assertWellFormed(finished) {
  const ids = new Set(finished.records.map((record) => record.id));
  assert.equal(ids.size, finished.records.length);
  for (const record of finished.records) {
    assert.ok(record.id.startsWith(S));
    assert.equal(record.hash, recordHash(record));
    assert.doesNotThrow(() => canonicalJson(record));
    assert.ok(['observed', 'inferred'].includes(record.evidence.confidence));
    assert.equal(record.label, null);
    if (record.parent) assert.ok(ids.has(record.parent), `${record.id} 的 parent 悬空`);
    if (record.type === 'relation') {
      assert.ok(ids.has(record.from));
      if (record.to) assert.ok(ids.has(record.to));
    }
  }
}

const pick = (record) => ({ type: record.type, parent: record.parent?.slice(S.length) ?? null, kind: record.attrs.kind, confidence: record.evidence.confidence });

test('JS/Electron', () => {
  const evidence = fixture('js-app.json');
  const { info, finished, byId, relations } = build((records) => mapJavascript(evidence, records));
  assertWellFormed(finished);
  assert.equal(info.detected, 'electron');
  assert.deepEqual(finished.counts, { entity: 13, operation: 18, field: 6, relation: 12 });

  assert.deepEqual(pick(byId.get(`${S}mod:src%2Forders%2Ejs`)), { type: 'entity', parent: null, kind: 'module', confidence: 'observed' });
  assert.deepEqual(pick(byId.get(`${S}sym:src%2Forders%2Ejs::Order`)), { type: 'entity', parent: 'mod:src%2Forders%2Ejs', kind: 'class', confidence: 'observed' });
  // 两个文件各有一个 Order，sym 带模块段，不撞
  assert.ok(byId.has(`${S}sym:src%2Flegacy%2Forder%2Ejs::Order`));
  assert.deepEqual(pick(byId.get(`${S}sym:src%2Forders%2Ejs::OrderService::create`)),
    { type: 'operation', parent: 'sym:src%2Forders%2Ejs::OrderService', kind: 'method', confidence: 'observed' });
  assert.deepEqual(byId.get(`${S}sym:src%2Forders%2Ejs::OrderService::create`).attrs.loc, { file: 'src/orders.js', line: 17 });
  assert.deepEqual(pick(byId.get(`${S}sym:src%2Fcustomers%2Ejs::listCustomers`)),
    { type: 'operation', parent: 'mod:src%2Fcustomers%2Ejs', kind: 'function', confidence: 'observed' });
  assert.deepEqual(pick(byId.get(`${S}sym:src%2Forders%2Ejs::Order::status`)),
    { type: 'field', parent: 'sym:src%2Forders%2Ejs::Order', kind: 'property', confidence: 'inferred' });
  assert.equal([...byId.keys()].some((id) => id.endsWith('::constructor')), false);

  assert.deepEqual(pick(byId.get(`${S}ipc:order:create`)), { type: 'operation', parent: 'mod:main%2Ejs', kind: 'ipc', confidence: 'observed' });
  assert.equal(byId.get(`${S}ipc:app:quit`).attrs.mode, 'listen');
  assert.deepEqual(pick(byId.get(`${S}bridge:api::createOrder`)), { type: 'operation', parent: 'mod:preload%2Ejs', kind: 'bridge', confidence: 'observed' });
  assert.equal(byId.get(`${S}bridge:api::createOrder`).attrs.channel, 'order:create');
  assert.deepEqual(pick(byId.get(`${S}route:%2Forders%2F:id`)), { type: 'operation', parent: 'mod:renderer%2Fapp%2Ejs', kind: 'route', confidence: 'inferred' });

  // `${BASE}/orders` 用同模块的常量补全；axios.get 看得出方法；查询串去掉
  const orders = byId.get(`${S}ext:https:%2F%2Ferp%2Eexample%2Ecom%2Fapi%2Forders`);
  assert.deepEqual(orders.attrs, { kind: 'endpoint', url: 'https://erp.example.com/api/orders', method: null });
  assert.equal(orders.evidence.confidence, 'inferred');
  assert.ok(byId.has(`${S}ext:https:%2F%2Ferp%2Eexample%2Ecom%2Fapi%2Forders%2F{id}%2Fapprove`));
  assert.ok(byId.has(`${S}ext:GET%20https:%2F%2Fcrm%2Eexample%2Ecom%2Fapi%2Fcustomers`));

  assert.deepEqual(relations.sort(), [
    'bridge:api::approveOrder calls ipc:order:approve',
    'bridge:api::createOrder calls ipc:order:create',
    'bridge:api::listCustomers calls ipc:customer:list',
    'bridge:api::quit calls ipc:app:quit',
    'mod:main%2Ejs imports mod:src%2Fcustomers%2Ejs',
    'mod:main%2Ejs imports mod:src%2Forders%2Ejs',
    'mod:renderer%2Fapp%2Ejs calls sym:renderer%2Fapp%2Ejs::navigate',
    'sym:src%2Fcustomers%2Ejs::listCustomers calls sym:src%2Fcustomers%2Ejs::Customer',
    'sym:src%2Fcustomers%2Ejs::listCustomers requests ext:GET%20https:%2F%2Fcrm%2Eexample%2Ecom%2Fapi%2Fcustomers',
    'sym:src%2Forders%2Ejs::OrderService::approve requests ext:https:%2F%2Ferp%2Eexample%2Ecom%2Fapi%2Forders%2F{id}%2Fapprove',
    'sym:src%2Forders%2Ejs::OrderService::create calls sym:src%2Forders%2Ejs::Order',
    'sym:src%2Forders%2Ejs::OrderService::create requests ext:https:%2F%2Ferp%2Eexample%2Ecom%2Fapi%2Forders',
  ]);

  // 同一份输入两次映射结果完全一样（锚点、顺序、哈希）
  const again = build((records) => mapJavascript(evidence, records));
  assert.deepEqual(again.finished.records, finished.records);
});

test('.NET 程序集', () => {
  const members = fixture('dotnet-members.json');
  const native = fixture('dotnet-native.json');
  const { finished, byId, relations } = build((records) => {
    const mapped = mapManagedMembers(members, records, { file: 'Sample.Orders.dll' });
    mapManagedNative(native, records, { methodAnchors: mapped.methodAnchors });
  });
  assertWellFormed(finished);
  assert.deepEqual(finished.counts, { entity: 11, operation: 12, field: 12, relation: 13 });
  assert.deepEqual(pick(byId.get(`${S}mod:Sample%2EOrders`)), { type: 'entity', parent: null, kind: 'module', confidence: 'observed' });
  const order = byId.get(`${S}sym:Sample::Orders::Order`);
  assert.deepEqual([order.attrs.type_kind, order.attrs.visibility, order.parent], ['class', 'public', `${S}mod:Sample%2EOrders`]);
  assert.equal(byId.get(`${S}sym:Sample::Orders::OrderStatus`).attrs.type_kind, 'enum');
  assert.equal(byId.get(`${S}sym:Sample::Orders::IOrderRepository`).attrs.type_kind, 'interface');
  assert.equal(byId.get(`${S}sym:Sample::Orders::NativeMethods`).attrs.visibility, 'internal');

  // 重载按声明顺序加 ~2
  assert.equal(byId.get(`${S}sym:Sample::Orders::OrderService::Cancel`).attrs.signature, 'Cancel(int): void');
  assert.equal(byId.get(`${S}sym:Sample::Orders::OrderService::Cancel~2`).attrs.signature, 'Cancel(int, string): void');
  assert.equal(byId.get(`${S}sym:Sample::Orders::IOrderRepository::Find`).attrs.signature, 'Find(int): Sample.Orders.Order');
  // 访问器收成属性，构造函数不单列
  assert.deepEqual(pick(byId.get(`${S}sym:Sample::Orders::Order::Status`)),
    { type: 'field', parent: 'sym:Sample::Orders::Order', kind: 'property', confidence: 'observed' });
  assert.equal(byId.get(`${S}sym:Sample::Orders::Order::Status`).attrs.signature, 'Sample.Orders.OrderStatus');
  assert.equal(byId.get(`${S}sym:Sample::Orders::OrderStatus::Approved`).attrs.kind, 'enum_value');
  assert.equal(byId.get(`${S}sym:Sample::Orders::OrderService::BaseUrl`).attrs.kind, 'constant');
  assert.equal([...byId.keys()].some((id) => /get_|set_|%2Ector/.test(id)), false);

  assert.deepEqual(pick(byId.get(`${S}native:kernel32%2Edll::Beep`)), { type: 'entity', parent: null, kind: 'native', confidence: 'observed' });
  // P/Invoke 按真实入口名：ShowMessage 声明的是 MessageBoxW
  assert.ok(byId.has(`${S}native:user32%2Edll::MessageBoxW`));
  assert.deepEqual(relations.sort(), [
    'sym:Sample::Orders::NativeMethods::Beep invokes_native native:kernel32%2Edll::Beep',
    'sym:Sample::Orders::NativeMethods::CreateFileW invokes_native native:kernel32%2Edll::CreateFileW',
    'sym:Sample::Orders::NativeMethods::ShowMessage invokes_native native:user32%2Edll::MessageBoxW',
    'sym:Sample::Orders::OrderService::ApproveOrder calls sym:Sample::Orders::IOrderRepository::Find',
    'sym:Sample::Orders::OrderService::ApproveOrder calls sym:Sample::Orders::NativeMethods::Beep',
    'sym:Sample::Orders::OrderService::ApproveOrder calls sym:Sample::Orders::Order::Approve',
    'sym:Sample::Orders::OrderService::ApproveUrl requests ext:https:%2F%2Ferp%2Eexample%2Ecom%2Fapi%2Forders%2F',
    'sym:Sample::Orders::OrderService::Cancel calls sym:Sample::Orders::OrderService::Cancel~2',
    'sym:Sample::Orders::OrderService::Cancel~2 calls sym:Sample::Orders::IOrderRepository::Find',
    'sym:Sample::Orders::OrderService::Cancel~2 calls sym:Sample::Orders::Order::Cancel',
    'sym:Sample::Orders::OrderService::CreateOrder calls sym:Sample::Orders::IOrderRepository::Save',
    'sym:Sample::Orders::OrderService::CreateOrder calls sym:Sample::Orders::Order',
    'sym:Sample::Orders::UrgentOrder inherits sym:Sample::Orders::Order',
  ]);
});

test('.NET：两份结果的 token 对不上时不连 P/Invoke', () => {
  const members = fixture('dotnet-members.json');
  const native = structuredClone(fixture('dotnet-native.json'));
  for (const item of native.normalized_result.pinvoke_imports) item.member_name = 'Other';
  const { relations } = build((records) => {
    const mapped = mapManagedMembers(members, records, { file: 'Sample.Orders.dll' });
    mapManagedNative(native, records, { methodAnchors: mapped.methodAnchors });
  });
  assert.equal(relations.some((item) => item.includes('invokes_native')), false);
});

test('PyInstaller 里的 pyc', () => {
  const { finished, byId, relations } = build((records) => {
    mapPythonPyc(fixture('pyc-orders-app.json'), records, { module: 'orders_app', file: 'orders-app/orders_app' });
    mapPythonPyc(fixture('pyc-orders-lib.json'), records, { module: 'orders_lib', file: 'orders-app/PYZ-00.pyz/orders_lib.pyc' });
  });
  assertWellFormed(finished);
  assert.deepEqual(finished.counts, { entity: 6, operation: 5, field: 5, relation: 1 });
  assert.deepEqual(pick(byId.get(`${S}mod:orders_app`)), { type: 'entity', parent: null, kind: 'module', confidence: 'observed' });
  assert.deepEqual(pick(byId.get(`${S}sym:orders_app::OrderService::create`)),
    { type: 'operation', parent: 'sym:orders_app::OrderService', kind: 'method', confidence: 'observed' });
  assert.deepEqual(byId.get(`${S}sym:orders_app::OrderService::create`).attrs.loc, { file: 'orders_app.py', line: 21 });
  assert.deepEqual(pick(byId.get(`${S}sym:orders_app::main`)), { type: 'operation', parent: 'mod:orders_app', kind: 'function', confidence: 'observed' });
  assert.deepEqual(pick(byId.get(`${S}sym:orders_app::Order::status`)),
    { type: 'field', parent: 'sym:orders_app::Order', kind: 'property', confidence: 'inferred' });
  assert.ok(byId.has(`${S}sym:orders_lib::Customer::name`));
  assert.equal([...byId.keys()].some((id) => id.includes('__init__')), false);
  assert.deepEqual(relations, ['mod:orders_app requests ext:https:%2F%2Ferp%2Eexample%2Ecom%2Fapi']);
});

test('记录集：撞名加 ~n、同名去重、截断保留上级、清洗字符串', () => {
  const records = new RecordSet({ source: 's', provider: 'rea-static@0.1.0' });
  const mod = records.add({ path: 'mod', segments: ['a.js'], type: 'entity', name: 'a.js', method: 'm' });
  const first = records.add({ path: 'sym', segments: ['a.js', 'f'], type: 'operation', parent: mod, name: 'f', method: 'm' });
  const second = records.add({ path: 'sym', segments: ['a.js', 'f'], type: 'operation', parent: mod, name: 'f', method: 'm' });
  assert.deepEqual([first, second], ['bin:s/sym:a%2Ejs::f', 'bin:s/sym:a%2Ejs::f~2']);
  const ext = records.add({ path: 'ext', segments: ['https://x.example.com'], type: 'entity', name: 'x', method: 'm', dedupe: true });
  assert.equal(records.add({ path: 'ext', segments: ['https://x.example.com'], type: 'entity', name: 'x', method: 'm', dedupe: true }), ext);
  assert.ok(records.relate({ from: first, name: 'requests', to: ext, method: 'm' }));
  assert.equal(records.relate({ from: first, name: 'requests', to: ext, method: 'm' }), null);
  const odd = records.add({
    path: 'sym', segments: ['a.js', 'bad\nname\uD800'], type: 'field', parent: first, name: 'bad\nname\uD800', method: 'm',
    attrs: { ratio: 0.5, text: 'x\uDC00' },
  });
  assert.equal(odd, 'bin:s/sym:a%2Ejs::bad?name�');

  const all = records.finish();
  assert.equal(all.records.find((record) => record.id === odd).attrs.ratio, '0.5');
  assert.equal(all.records.find((record) => record.id === odd).attrs.text, 'x�');
  assert.deepEqual(all.records.map((record) => record.type), ['entity', 'entity', 'operation', 'operation', 'field', 'relation']);

  const cut = records.finish(3);
  assert.equal(cut.truncated, true);
  assert.deepEqual(cut.records.map((record) => record.id), ['bin:s/mod:a%2Ejs', ext, first]);
  assert.deepEqual(cut.counts, { entity: 2, operation: 1 });
});

test('URL 模板、HTTP 方法、pyc 模块名', () => {
  const literals = new Map([['BASE', 'https://erp.example.com/api']]);
  assert.deepEqual(resolveTemplate('`${BASE}/orders/${id}?page=1`', literals), { url: 'https://erp.example.com/api/orders/{id}', substituted: true });
  assert.deepEqual(resolveTemplate("'https://crm.example.com/a#x'"), { url: 'https://crm.example.com/a', substituted: false });
  assert.equal(httpMethod('call:axios.get'), 'GET');
  assert.equal(httpMethod('call:router.post'), 'POST');
  assert.equal(httpMethod('call:fetch'), null);
  assert.equal(pycModuleName('app/__pycache__/orders.cpython-311.pyc'), 'app.orders');
  assert.equal(pycModuleName('app/__init__.pyc'), 'app');
});
