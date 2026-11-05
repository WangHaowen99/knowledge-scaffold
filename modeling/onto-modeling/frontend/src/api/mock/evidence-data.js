// mock · 证据源的合成数据（主示例「门店补货单编制」）：登记的业务系统数据源、提供方、采集任务、快照与对比、
// ERP 补货相关的表结构（分层摘要、ER、记录详情、相邻记录）；程序逆向的合成 Electron 订货客户端（模块、类、IPC、外部接口）；
// 代码仓库的合成 Java 补货单服务（实体、字段、路由、服务方法、校验规则）。
// 全部合成；主机只用保留网段 192.0.2.x，域名只用 example.com。提供方清单与网关 conn/catalog.py 一致。
//
// 数值口径同 scenario.js：品类标准上限 120 箱；09 修订通知把乳制品区域配额上限改为 150 箱，
// 10-30 的 ERP 快照（9c1d，产物出处都指向它）里还是 140；10-31 的快照（5e2b）里 ERP 已改成 150。
import { entryRef } from './scenario.js';

export const SNAP = {
  s5e: '20261031-221800-5e2b',
  s9c: '20261030-221500-9c1d',
  s41: '20261024-223000-41aa',
  s77: '20261018-230200-77b0',
  rea: '20261029-101500-a3f0',
  code: '20261028-140200-c0d3',
};
// 10-31 及以后的快照里 ERP 已按修订通知改过（新约束、字段注释、配额 150）
export const isNewGen = (snapshotId) => String(snapshotId || '') >= '20261031';

const GB = 1024 ** 3;
const MB = 1024 ** 2;

// ---------- 程序逆向（REA，F13）：合成的 Electron 门店订货客户端 ----------
// 锚点按 evidence-collection.md §2：bin:<源>/<路径类型>:<名字>，名字里 % / . # @ | [ ] 和空格百分号编码，限定名各段用 :: 连接
export const REA = { id: 'sample-orders', display_name: '门店订货客户端', environment: 'test' };
export const REA_ARTIFACT = { sha256: '6b523daca442c454df04281f8018381b8b8d32049b0e458af69ffa3b710db5bf', size: 48 * MB + 211000, filename: 'sample-orders-1.4.2.zip' };
const binSeg = (s) => String(s).replace(/[%/.#@|[\] ]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0')}`);
export const binAnchor = (source, ptype, ...segs) => `bin:${source}/${ptype}:${segs.map(binSeg).join('::')}`;

// [路径类型, 名字各段, 记录类型, kind, 模块, 行号, 置信度, 其他 attrs, 显示名, 上级（同表里的下标）]
const REA_ROWS = [
  ['mod', ['main.js'], 'entity', 'module', 'main.js', 1],
  ['mod', ['preload.js'], 'entity', 'module', 'preload.js', 1],
  ['mod', ['orders.js'], 'entity', 'module', 'orders.js', 1],
  ['mod', ['customers.js'], 'entity', 'module', 'customers.js', 1],
  ['mod', ['erp-client.js'], 'entity', 'module', 'erp-client.js', 1],
  ['ipc', ['order:create'], 'operation', 'ipc', 'main.js', 24, 'observed', { channel: 'order:create' }],
  ['ipc', ['order:cancel'], 'operation', 'ipc', 'main.js', 31, 'observed', { channel: 'order:cancel' }],
  ['ipc', ['customer:lookup'], 'operation', 'ipc', 'main.js', 38, 'observed', { channel: 'customer:lookup' }],
  ['bridge', ['api', 'createOrder'], 'operation', 'bridge', 'preload.js', 6, 'observed', { signature: 'createOrder(draft)' }, 'api.createOrder'],
  ['bridge', ['api', 'cancelOrder'], 'operation', 'bridge', 'preload.js', 7, 'observed', { signature: 'cancelOrder(orderNo, reason)' }, 'api.cancelOrder'],
  ['sym', ['orders.js', 'Order'], 'entity', 'class', 'orders.js', 8, 'observed', {}, 'Order'],
  ['sym', ['orders.js', 'Order', 'orderNo'], 'field', 'property', 'orders.js', 10, 'observed', {}, 'orderNo', 10],
  ['sym', ['orders.js', 'Order', 'qty'], 'field', 'property', 'orders.js', 11, 'observed', {}, 'qty', 10],
  ['sym', ['orders.js', 'Order', 'create'], 'operation', 'method', 'orders.js', 21, 'observed', { signature: 'create(draft)' }, 'Order.create', 10],
  ['sym', ['orders.js', 'Order', 'cancel'], 'operation', 'method', 'orders.js', 47, 'observed', { signature: 'cancel(reason)' }, 'Order.cancel', 10],
  ['sym', ['customers.js', 'Customer'], 'entity', 'class', 'customers.js', 5, 'observed', {}, 'Customer'],
  ['sym', ['customers.js', 'Customer', 'lookup'], 'operation', 'method', 'customers.js', 14, 'observed', { signature: 'lookup(storeCode)' }, 'Customer.lookup', 15],
  ['sym', ['erp-client.js', 'submitOrder'], 'operation', 'function', 'erp-client.js', 18, 'observed', { signature: 'submitOrder(order)' }, 'submitOrder'],
  ['sym', ['erp-client.js', 'fetchCustomer'], 'operation', 'function', 'erp-client.js', 33, 'inferred', { signature: 'fetchCustomer(code)' }, 'fetchCustomer'],
  ['ext', ['POST https://erp.example.com/api/orders'], 'entity', 'endpoint', 'erp-client.js', 20, 'observed', { method: 'POST', url: 'https://erp.example.com/api/orders' }],
  // URL 由变量拼出来，REA 标成推断
  ['ext', ['GET https://erp.example.com/api/customers'], 'entity', 'endpoint', 'erp-client.js', 35, 'inferred', { method: 'GET', url: 'https://erp.example.com/api/customers' }],
];
// 关系：[来源下标, 关系名, 目标下标, 置信度]
const REA_RELS = [
  [1, 'exposes', 8], [1, 'exposes', 9], [8, 'calls', 5], [9, 'calls', 6],
  [13, 'handles', 5], [14, 'handles', 6], [16, 'handles', 7],
  [13, 'calls', 17], [16, 'calls', 18], [17, 'requests', 19], [18, 'requests', 20, 'inferred'],
];

/** 一个 REA 数据源的全部记录（锚点带源 ID，新登记的自助源也能用同一份合成结果） */
export function reaRecords(source = REA.id) {
  const anchors = REA_ROWS.map(([pt, segs]) => binAnchor(source, pt, ...segs));
  const recs = REA_ROWS.map(([, segs, type, kind, module, line, confidence = 'observed', attrs = {}, name, parent], i) => ({
    id: anchors[i], type, parent: parent == null ? null : anchors[parent], name: name || segs.at(-1), label: null,
    attrs: { kind, module, loc: { file: module, line }, ...attrs },
    evidence: { provider: 'rea-static@0.1.0', method: 'rea analyze-javascript-application', confidence },
  }));
  // 关系锚点本来是 sha256(from|name|to) 的前 16 位；mock 用序号凑 16 位
  REA_RELS.forEach(([from, name, to, confidence = 'observed'], i) => {
    recs.push({
      id: binAnchor(source, 'rel', `${String(i).padStart(4, '0')}a3f0c2d1e5b7`), type: 'relation', parent: null, name, label: null,
      attrs: { from: anchors[from], to: anchors[to], module: recs[from].attrs.module, loc: recs[from].attrs.loc },
      evidence: { provider: 'rea-static@0.1.0', method: 'rea analyze-javascript-application', confidence },
    });
  });
  return recs;
}
const countBy = (recs) => recs.reduce((m, r) => ({ ...m, [r.type]: (m[r.type] || 0) + 1 }), {});
export const REA_COUNTS = countBy(reaRecords());

// EV-19 的 manifest（去掉哈希和凭据引用）：artifact、工具、局限、不可信标记
export function reaManifest(source = REA.id, artifact = REA_ARTIFACT, jobId = 'job-0128') {
  return {
    format: 'evidence-snapshot/1', source: { id: source, provider: 'rea-static' }, provider: 'rea-static@0.1.0', tool: { name: 'rea', version: '6.8.0' },
    job_id: jobId, scope: [], complete: true, coverage_pct: 100, untrusted_content: true, masking_policy: 'none',
    artifact: { ...artifact, detected: 'electron' },
    analyzers: [{ command: 'analyze-javascript-application', provider: 'rea-javascript-application@1', ms: 4578 }],
    records: countBy(reaRecords(source)), budget_used: { records: reaRecords(source).length, bytes: 18000, duration_s: 5 },
    limitations: ['渲染进程里动态拼接的 IPC 通道名没有展开（2 处）', '压缩过的第三方库没有逐个分析'], unknowns: 7, masked_hits: 0,
  };
}

// ---------- 代码仓库（code-repo，F14）：合成的 Java 补货单服务 ----------
// 锚点按 evidence-collection.md §10：Java 包名的点拆成段（code:<源>/sym:com::example::restock::order::Order）；
// 路由 route:<方法> <路径>（attrs.handler 指向方法锚点）；校验规则 rule:<符号段>::<规则名>，上级是字段。
// 字段的 data_type 放在 attrs 里（EV-25 只透传 attrs）。数量上限 @Max(120) 是按旧口径写的，供三方对账演示矛盾。
export const CODE = { id: 'restock-svc', display_name: '补货单服务代码', environment: 'test' };
export const CODE_REPO = { repo_url: 'https://git.example.com/demo/restock-service.git', ref: 'main', commit: '3f9a1c2e8b7d4c6a9e0f1b2c3d4e5f6a7b8c9d0e' };
const codeAnchor = (source, ptype, ...segs) => `code:${binAnchor(source, ptype, ...segs).slice('bin:'.length)}`;
const PKG = ['com', 'example', 'restock', 'order'];
const MOD = 'com.example.restock.order';
const JAVA = 'src/main/java/com/example/restock/order';
const SNIP = {
  order: [
    '@Entity',
    '@Table(name = "replenish_order")',
    'public class Order {',
    '    @Id',
    '    @NotNull',
    '    @Size(max = 20)',
    '    @Column(name = "order_no")',
    '    private String orderNo;',
    '',
    '    @NotBlank',
    '    @Column(name = "store_code")',
    '    private String storeCode;',
    '',
    '    @Column(name = "category_code")',
    '    private String categoryCode;',
    '',
    '    @Enumerated(EnumType.STRING)',
    '    private OrderStatus status;',
    '',
    '    @OneToMany(mappedBy = "order")',
    '    private List<OrderItem> items;',
  ],
  item: [
    '@Entity',
    '@Table(name = "replenish_order_item")',
    'public class OrderItem {',
    '    @Id',
    '    @Column(name = "item_no")',
    '    private String itemNo;',
    '',
    '    @ManyToOne',
    '    @JoinColumn(name = "order_no")',
    '    private Order order;',
    '',
    '    @Column(name = "goods_code")',
    '    private String goodsCode;',
    '',
    '    @Min(1)',
    '    @Max(120)',
    '    @Column(name = "request_qty")',
    '    private Integer requestQty;',
  ],
  create: [
    '@PostMapping("/api/orders")',
    'public OrderView create(@Valid @RequestBody OrderDraft draft) {',
    '    Order order = draft.toOrder();',
    '    return OrderView.of(orderService.submit(order));',
    '}',
  ],
  approve: [
    '@PostMapping("/api/orders/{orderNo}/approve")',
    'public OrderView approve(@PathVariable String orderNo) {',
    '    return OrderView.of(orderService.approve(orderNo));',
    '}',
  ],
  submit: [
    '@Transactional',
    'public Order submit(Order order) {',
    '    if (!repository.findPendingByStore(order.getStoreCode()).isEmpty()) {',
    '        throw new DuplicateOrderException(order.getStoreCode());',
    '    }',
    '    order.setStatus(OrderStatus.SUBMITTED);',
    '    return repository.save(order);',
    '}',
  ],
  svcApprove: [
    '@Transactional',
    'public Order approve(String orderNo) {',
    '    Order order = repository.findById(orderNo).orElseThrow();',
    '    order.setStatus(OrderStatus.APPROVED);',
    '    return order;',
    '}',
  ],
};
// [键, 路径类型, 名字各段, 记录类型, attrs, 置信度, 上级的键, 显示名]
const CODE_ROWS = [
  ['order', 'sym', [...PKG, 'Order'], 'entity', { kind: 'class', orm: 'jpa', table: 'replenish_order', module: MOD, loc: { file: `${JAVA}/Order.java`, line: 12, end_line: 40 }, snippet: SNIP.order }, 'declared'],
  ['orderNo', 'sym', [...PKG, 'Order', 'orderNo'], 'field', { kind: 'field', data_type: 'String', column: 'order_no', nullable: false, validations: ['@NotNull', '@Size(max = 20)'], module: MOD, loc: { file: `${JAVA}/Order.java`, line: 19 } }, 'declared', 'order'],
  ['storeCode', 'sym', [...PKG, 'Order', 'storeCode'], 'field', { kind: 'field', data_type: 'String', column: 'store_code', nullable: false, validations: ['@NotBlank'], module: MOD, loc: { file: `${JAVA}/Order.java`, line: 23 } }, 'declared', 'order'],
  ['categoryCode', 'sym', [...PKG, 'Order', 'categoryCode'], 'field', { kind: 'field', data_type: 'String', column: 'category_code', module: MOD, loc: { file: `${JAVA}/Order.java`, line: 26 } }, 'declared', 'order'],
  ['status', 'sym', [...PKG, 'Order', 'status'], 'field', { kind: 'field', data_type: 'OrderStatus', column: 'status', module: MOD, loc: { file: `${JAVA}/Order.java`, line: 29 } }, 'declared', 'order'],
  ['item', 'sym', [...PKG, 'OrderItem'], 'entity', { kind: 'class', orm: 'jpa', table: 'replenish_order_item', module: MOD, loc: { file: `${JAVA}/OrderItem.java`, line: 10, end_line: 31 }, snippet: SNIP.item }, 'declared'],
  ['itemOrder', 'sym', [...PKG, 'OrderItem', 'order'], 'field', { kind: 'field', data_type: 'Order', column: 'order_no', module: MOD, loc: { file: `${JAVA}/OrderItem.java`, line: 19 } }, 'declared', 'item'],
  ['goodsCode', 'sym', [...PKG, 'OrderItem', 'goodsCode'], 'field', { kind: 'field', data_type: 'String', column: 'goods_code', module: MOD, loc: { file: `${JAVA}/OrderItem.java`, line: 22 } }, 'declared', 'item'],
  ['requestQty', 'sym', [...PKG, 'OrderItem', 'requestQty'], 'field', { kind: 'field', data_type: 'Integer', column: 'request_qty', validations: ['@Min(1)', '@Max(120)'], module: MOD, loc: { file: `${JAVA}/OrderItem.java`, line: 27 } }, 'declared', 'item'],
  ['statusEnum', 'sym', [...PKG, 'OrderStatus'], 'entity', { kind: 'enum', values: ['DRAFT', 'SUBMITTED', 'APPROVED', 'REJECTED'], module: MOD, loc: { file: `${JAVA}/OrderStatus.java`, line: 3, end_line: 8 } }, 'observed'],
  ['ruleOrderNoNotNull', 'rule', [...PKG, 'Order', 'orderNo', 'NotNull'], 'rule', { kind: 'validation', annotation: '@NotNull', module: MOD, loc: { file: `${JAVA}/Order.java`, line: 16 } }, 'declared', 'orderNo', 'NotNull'],
  ['ruleOrderNoSize', 'rule', [...PKG, 'Order', 'orderNo', 'Size'], 'rule', { kind: 'validation', annotation: '@Size', expression: 'max = 20', module: MOD, loc: { file: `${JAVA}/Order.java`, line: 17 } }, 'declared', 'orderNo', 'Size'],
  ['ruleStoreNotBlank', 'rule', [...PKG, 'Order', 'storeCode', 'NotBlank'], 'rule', { kind: 'validation', annotation: '@NotBlank', module: MOD, loc: { file: `${JAVA}/Order.java`, line: 21 } }, 'declared', 'storeCode', 'NotBlank'],
  ['ruleQtyMin', 'rule', [...PKG, 'OrderItem', 'requestQty', 'Min'], 'rule', { kind: 'validation', annotation: '@Min', expression: '1', module: MOD, loc: { file: `${JAVA}/OrderItem.java`, line: 24 } }, 'declared', 'requestQty', 'Min'],
  ['ruleQtyMax', 'rule', [...PKG, 'OrderItem', 'requestQty', 'Max'], 'rule', { kind: 'validation', annotation: '@Max', expression: '120', module: MOD, loc: { file: `${JAVA}/OrderItem.java`, line: 25 } }, 'declared', 'requestQty', 'Max'],
  ['ctlCreate', 'sym', [...PKG, 'web', 'OrderController', 'create'], 'operation', { kind: 'method', signature: 'create(OrderDraft draft)', module: `${MOD}.web`, loc: { file: `${JAVA}/web/OrderController.java`, line: 31, end_line: 35 }, snippet: SNIP.create }, 'observed', null, 'OrderController.create'],
  ['ctlApprove', 'sym', [...PKG, 'web', 'OrderController', 'approve'], 'operation', { kind: 'method', signature: 'approve(String orderNo)', module: `${MOD}.web`, loc: { file: `${JAVA}/web/OrderController.java`, line: 38, end_line: 41 }, snippet: SNIP.approve }, 'observed', null, 'OrderController.approve'],
  ['routeCreate', 'route', ['POST /api/orders'], 'operation', { kind: 'route', method: 'POST', path: '/api/orders', handler: 'ctlCreate', module: `${MOD}.web`, loc: { file: `${JAVA}/web/OrderController.java`, line: 31 } }, 'observed'],
  ['routeApprove', 'route', ['POST /api/orders/{orderNo}/approve'], 'operation', { kind: 'route', method: 'POST', path: '/api/orders/{orderNo}/approve', handler: 'ctlApprove', module: `${MOD}.web`, loc: { file: `${JAVA}/web/OrderController.java`, line: 38 } }, 'observed'],
  ['svcSubmit', 'sym', [...PKG, 'service', 'OrderService', 'submit'], 'operation', { kind: 'method', signature: 'submit(Order order)', module: `${MOD}.service`, loc: { file: `${JAVA}/service/OrderService.java`, line: 27, end_line: 34 }, snippet: SNIP.submit }, 'observed', null, 'OrderService.submit'],
  ['svcApprove', 'sym', [...PKG, 'service', 'OrderService', 'approve'], 'operation', { kind: 'method', signature: 'approve(String orderNo)', module: `${MOD}.service`, loc: { file: `${JAVA}/service/OrderService.java`, line: 37, end_line: 42 }, snippet: SNIP.svcApprove }, 'observed', null, 'OrderService.approve'],
  ['repoPending', 'sym', [...PKG, 'repo', 'OrderRepository', 'findPendingByStore'], 'operation', {
    kind: 'method', signature: 'findPendingByStore(String storeCode)', module: `${MOD}.repo`, loc: { file: `${JAVA}/repo/OrderRepository.java`, line: 14 },
    sql: "SELECT o FROM Order o WHERE o.storeCode = :storeCode AND o.status = 'SUBMITTED'", tables: ['replenish_order'],
  }, 'observed', null, 'OrderRepository.findPendingByStore'],
];
// 关系：[来源键, 关系名, 目标键, 置信度]；JPA 关联是声明的，方法之间的调用是推断的
const CODE_RELS = [
  ['order', 'one_to_many', 'item', 'declared'], ['item', 'many_to_one', 'order', 'declared'],
  ['ctlCreate', 'calls', 'svcSubmit', 'inferred'], ['ctlApprove', 'calls', 'svcApprove', 'inferred'], ['svcSubmit', 'calls', 'repoPending', 'inferred'],
];

/** 一个代码仓库数据源的全部记录 */
export function codeRecords(source = CODE.id) {
  const anchors = Object.fromEntries(CODE_ROWS.map(([k, pt, segs]) => [k, codeAnchor(source, pt, ...segs)]));
  const recs = CODE_ROWS.map(([k, , segs, type, attrs, confidence, parent, name]) => {
    const a = { ...attrs, ...(attrs.snippet ? { snippet: attrs.snippet.join('\n') } : {}), ...(attrs.handler ? { handler: anchors[attrs.handler] } : {}) };
    return {
      id: anchors[k], type, parent: parent ? anchors[parent] : null, name: name || segs.at(-1), label: null, attrs: a,
      evidence: { provider: 'code-repo@0.1.0', method: 'javalang', confidence },
    };
  });
  CODE_RELS.forEach(([from, name, to, confidence], i) => {
    const src = recs.find((r) => r.id === anchors[from]);
    recs.push({
      id: codeAnchor(source, 'rel', `${String(i).padStart(4, '0')}c0d3e1f2a3b4`), type: 'relation', parent: null, name, label: null,
      attrs: { from: anchors[from], to: anchors[to], module: src.attrs.module, loc: src.attrs.loc },
      evidence: { provider: 'code-repo@0.1.0', method: 'javalang', confidence },
    });
  });
  return recs;
}
export const CODE_COUNTS = countBy(codeRecords());
export function codeManifest(source = CODE.id, jobId = 'job-0126') {
  return {
    format: 'evidence-snapshot/1', source: { id: source, provider: 'code-repo' }, provider: 'code-repo@0.1.0',
    job_id: jobId, scope: ['src/main/java/*'], complete: true, coverage_pct: 100, untrusted_content: true, masking_policy: 'none',
    artifact: { ...CODE_REPO }, records: { ...CODE_COUNTS }, budget_used: { records: codeRecords(source).length, bytes: 61000, duration_s: 12 },
    limitations: ['Kotlin 源文件没有解析（3 个）', 'MyBatis XML 里的 SQL 没有关联到方法'], masked_hits: 0,
    access_log: [{ statement_digest: 'clone:9d4e1a7c3b2f6e08', rows: 0, bytes: 2400000, duration_ms: 8300 }],
  };
}

// ---------- 提供方（EV-10）：和网关 conn/catalog.py 的清单一致，加上旁路容器的程序逆向 ----------
const DB_FIELDS = [
  { name: 'host', label: '主机', secret: false },
  { name: 'port', label: '端口', secret: false },
  { name: 'database', label: '库名', secret: false },
  { name: 'username', label: '用户名', secret: false },
  { name: 'password', label: '密码', secret: true },
];
const provider = (id, version, label, kind, o = {}) => ({
  id, version, label, kind, evidence_types: [], capabilities: [], cred_fields: [], readonly_method: '',
  runtime: 'subprocess', installed: true, license_note: '', self_service: false, input: 'connection', max_artifact_bytes: null, ...o,
});
const DB_TYPES = ['entity', 'field', 'relation', 'constraint', 'operation', 'rule', 'profile', 'sample'];
export function providers() {
  return {
    gateway: { status: 'ok', checked_at: new Date().toISOString() },
    items: [
      provider('db-mysql', '0.1.0', 'MySQL', 'db', {
        evidence_types: DB_TYPES, capabilities: ['discover', 'snapshot', 'sample'], cred_fields: DB_FIELDS, readonly_method: '只读账号 · 只读会话 · 语句白名单', license_note: '驱动 PyMySQL（MIT）',
      }),
      provider('db-postgres', '0.1.0', 'PostgreSQL', 'db', {
        evidence_types: DB_TYPES, capabilities: ['discover', 'snapshot', 'sample'], cred_fields: DB_FIELDS, readonly_method: '只读账号 · 只读会话 · 语句白名单', license_note: '驱动 pg8000（BSD-3-Clause）',
      }),
      provider('code-repo', '0.1.0', '代码仓库', 'code', {
        evidence_types: ['entity', 'field', 'relation', 'operation', 'rule'], capabilities: ['snapshot'],
        cred_fields: [{ name: 'repo_url', label: '仓库地址', secret: false }, { name: 'ref', label: '分支或提交', secret: false }, { name: 'token', label: '访问令牌', secret: true }],
        readonly_method: '只读令牌 · 按提交浅克隆 · 不执行代码', license_note: 'Java 解析用 javalang（MIT）',
      }),
      provider('rea-static', '0.1.0', '程序逆向', 'rea', {
        evidence_types: ['entity', 'field', 'operation', 'relation'], capabilities: ['snapshot'], readonly_method: '静态分析，不运行目标程序，旁路容器断网',
        runtime: 'sidecar', license_note: '逆向分析前要确认软件授权范围', self_service: true, input: 'artifact', max_artifact_bytes: 2048 * MB,
      }),
    ],
  };
}

// 内核 sources.scope_summary 的写法：前三项，多了写「等 N 项」；没写范围是「全部」
export const scopeSummary = (scope = []) => (scope.length ? `${scope.slice(0, 3).join('、')}${scope.length > 3 ? ` 等 ${scope.length} 项` : ''}` : '全部');

// ---------- 登记的数据源（EV-01，平台全局） ----------
export function sources() {
  return [
    {
      id: 'erp-prod', display_name: 'ERP 生产库', environment: 'production',
      provider: { id: 'db-mysql', version: '0.1.0', label: 'MySQL' },
      connection: { host: '192.0.2.14', port: 3306, database: 'erp', username: 'ro_onto' },
      cred_ref: 'cred://erp-prod/3', secret_fields: [{ name: 'password', set: true, written_at: '2026-10-28T10:20:00+08:00' }],
      scope_allowlist: ['erp.goods_category', 'erp.replenish_std', 'erp.std_catalog', 'erp.promo_plan', 'erp.store*', 'erp.replenish_order*'],
      scope_summary: 'erp.goods_category、erp.replenish_std、erp.std_catalog 等 6 项',
      egress: [{ host: '192.0.2.14', port: 3306 }],
      access_window: { days: 'daily', start: '22:00', end: '06:00', tz: 'Asia/Shanghai' },
      budget: { max_rows: 2000000, max_bytes: 2 * GB, max_duration_s: 1800 },
      rate_limit: { concurrency: 2, qps: 20, query_timeout_s: 30 },
      masking_policy: 'mask-v1',
      authorization_note: 'ERP 只读账号由信息中心 2026-10-20 开通，用于门店补货单编制本体建模。',
      owner: 'zhangsan', last_job: null, authorized_for_project: null,
    },
    {
      id: 'crm-test', display_name: 'CRM 测试库', environment: 'test',
      provider: { id: 'db-postgres', version: '0.1.0', label: 'PostgreSQL' },
      connection: { host: '192.0.2.20', port: 5432, database: 'crm', username: 'ro_onto' },
      cred_ref: 'cred://crm-test/1', secret_fields: [{ name: 'password', set: true, written_at: '2026-10-12T09:00:00+08:00' }],
      scope_allowlist: ['crm.*'], scope_summary: 'crm.*',
      egress: [{ host: '192.0.2.20', port: 5432 }],
      access_window: null,
      budget: { max_rows: 500000, max_bytes: GB, max_duration_s: 1200 },
      rate_limit: { concurrency: 2, qps: 20, query_timeout_s: 30 },
      masking_policy: 'mask-v1', authorization_note: null, owner: 'modeler01',
      // 别的项目采集过（登记是全局的）
      last_job: { at: '2026-10-24T09:30:00+08:00', state: 'succeeded' }, authorized_for_project: null,
    },
    {
      id: CODE.id, display_name: CODE.display_name, environment: 'test',
      provider: { id: 'code-repo', version: '0.1.0', label: '代码仓库' },
      connection: { repo_url: CODE_REPO.repo_url, ref: CODE_REPO.ref },
      cred_ref: 'cred://restock-svc/2', secret_fields: [{ name: 'token', set: true, written_at: '2026-10-26T15:40:00+08:00' }],
      scope_allowlist: ['src/main/java/*'], scope_summary: 'src/main/java/*',
      egress: [{ host: 'git.example.com', port: 443 }],
      access_window: null,
      budget: { max_rows: 200000, max_bytes: 512 * MB, max_duration_s: 900 },
      rate_limit: { concurrency: 1, qps: 5, query_timeout_s: 60 },
      masking_policy: 'mask-v1', authorization_note: '只读令牌由研发组开通，用于补货单服务的取证。', owner: 'zhangsan',
      last_job: null, authorized_for_project: null,
    },
    // 自助登记的程序逆向数据源（F13）：只有登记人和管理员看得到
    {
      id: REA.id, display_name: '门店订货客户端', environment: 'test',
      provider: { id: 'rea-static', version: '0.1.0', label: '程序逆向' },
      connection: { artifact_sha256: REA_ARTIFACT.sha256, artifact_filename: REA_ARTIFACT.filename, artifact_size: REA_ARTIFACT.size },
      cred_ref: null, secret_fields: [], scope_allowlist: [], scope_summary: '', egress: [], access_window: null,
      budget: { max_rows: 200000, max_bytes: 256 * MB, max_duration_s: 1800 }, rate_limit: null, masking_policy: 'none',
      authorization_note: '合成示例程序，供应商许可允许为对接做静态分析。', owner: 'zhangsan', last_job: null, authorized_for_project: null,
    },
  ];
}

export const GRANTS = {
  'erp-prod': { authorized: true, granted_at: '2026-10-20T10:00:00+08:00', granted_by: 'zhangsan' },
  [REA.id]: { authorized: true, granted_at: '2026-10-29T10:10:00+08:00', granted_by: 'zhangsan' },
  [CODE.id]: { authorized: true, granted_at: '2026-10-28T13:50:00+08:00', granted_by: 'zhangsan' },
};

const ref = (s) => ({ id: s.id, display_name: s.display_name, environment: s.environment });
const ERP = { id: 'erp-prod', display_name: 'ERP 生产库', environment: 'production' };

// ---------- 采集任务（EV-15）：运行中、排队的按「现在」算 ----------
const iso = (ms) => new Date(ms).toISOString();
function job(o) {
  return {
    project_id: null, source: ERP, operation: 'snapshot', scope_text: '', incremental_base: null, state: 'succeeded', phase: null, progress: null,
    queue_reason: null, started_at: null, ended_at: null, duration_ms: null, result: null, error: null, resumed_from: null, checkpoint: false,
    initiated_by: 'zhangsan', consent: null, actions: [], ...o,
  };
}
export function seedJobs(pid, now = Date.now()) {
  const consent = (at) => ({ by: 'zhangsan', at });
  return [
    job({
      id: 'job-0131', project_id: pid, scope_text: '42 表（增量）', incremental_base: SNAP.s5e, state: 'running', phase: '采集字段与约束',
      progress: { done: 28, total: 42, unit: '表', pct: 66 }, started_at: iso(now - 372000), consent: consent(iso(now - 380000)), actions: ['cancel'],
    }),
    job({
      id: 'job-0132', project_id: pid, operation: 'sample', scope_text: 'std_catalog 20 行', state: 'queued',
      queue_reason: '等待访问窗口 22:00，前面还有 1 个任务', consent: consent(iso(now - 120000)), actions: ['cancel'],
    }),
    job({
      id: 'job-0130', project_id: pid, scope_text: '42 表', started_at: '2026-10-31T22:18:00+08:00', ended_at: '2026-10-31T22:32:03+08:00', duration_ms: 843000,
      result: { snapshot_id: SNAP.s5e, message: `生成快照 ${SNAP.s5e}` }, consent: consent('2026-10-31T22:17:40+08:00'), actions: ['browse'],
    }),
    job({
      id: 'job-0127', project_id: pid, scope_text: '42 表', state: 'partial', started_at: '2026-10-18T23:02:00+08:00', ended_at: '2026-10-18T23:32:00+08:00', duration_ms: 1800000,
      result: { snapshot_id: SNAP.s77, message: '预算用尽：已采 33 / 42 张表，断点已记录' }, checkpoint: true, consent: consent('2026-10-18T23:01:30+08:00'), actions: ['resume'],
    }),
    job({
      id: 'job-0125', project_id: pid, operation: 'discover', state: 'failed', started_at: '2026-10-27T22:03:00+08:00', ended_at: '2026-10-27T22:03:04+08:00', duration_ms: 4000,
      error: { code: 'credential_expired', message: '401：凭据已过期' }, consent: consent('2026-10-27T22:02:50+08:00'), actions: ['notify_admin'],
    }),
    job({
      id: 'job-0122', project_id: pid, operation: 'sample', scope_text: 'store 20 行', state: 'cancelled', started_at: '2026-10-21T22:30:00+08:00', ended_at: '2026-10-21T22:31:40+08:00', duration_ms: 100000,
      result: { message: '你在 10-21 取消' }, consent: consent('2026-10-21T22:29:50+08:00'), actions: ['retry'],
    }),
    job({
      id: 'job-0128', project_id: pid, source: REA, scope_text: REA_ARTIFACT.filename, started_at: '2026-10-29T10:15:00+08:00', ended_at: '2026-10-29T10:15:05+08:00', duration_ms: 4600,
      result: { snapshot_id: SNAP.rea, message: `生成快照 ${SNAP.rea}` }, actions: ['browse'],
    }),
    job({
      id: 'job-0126', project_id: pid, source: CODE, scope_text: 'src/main/java/*', started_at: '2026-10-28T14:02:00+08:00', ended_at: '2026-10-28T14:02:12+08:00', duration_ms: 12400,
      result: { snapshot_id: SNAP.code, message: `生成快照 ${SNAP.code}` }, actions: ['browse'],
    }),
  ];
}

// ---------- 快照（EV-18） ----------
const snap = (id, o) => ({
  id, source: ERP, provider: 'db-mysql@0.1.0', complete: true, coverage_pct: 100, referenced: 0, masking_policy: 'mask-v1',
  previous: null, job_id: null, git_bytes: 0, ...o,
});
export function seedSnapshots() {
  return [
    snap(SNAP.s5e, {
      records: { entity: 43, field: 621, relation: 57, constraint: 89, profile: 516, sample: 860 },
      started_at: '2026-10-31T22:18:00+08:00', ended_at: '2026-10-31T22:32:03+08:00', previous: SNAP.s9c, job_id: 'job-0130', git_bytes: 412000,
    }),
    snap(SNAP.s9c, {
      records: { entity: 42, field: 618, relation: 57, constraint: 88, profile: 512, sample: 840 }, referenced: 17,
      started_at: '2026-10-30T22:15:00+08:00', ended_at: '2026-10-30T22:29:03+08:00', previous: SNAP.s41, job_id: 'job-0129', git_bytes: 408000,
    }),
    snap(SNAP.rea, {
      source: REA, provider: 'rea-static@0.1.0', masking_policy: 'none', records: { ...REA_COUNTS },
      started_at: '2026-10-29T10:15:00+08:00', ended_at: '2026-10-29T10:15:05+08:00', job_id: 'job-0128', git_bytes: 18000,
    }),
    snap(SNAP.code, {
      source: CODE, provider: 'code-repo@0.1.0', masking_policy: 'none', records: { ...CODE_COUNTS },
      started_at: '2026-10-28T14:02:00+08:00', ended_at: '2026-10-28T14:02:12+08:00', job_id: 'job-0126', git_bytes: 61000,
    }),
    snap(SNAP.s41, {
      records: { entity: 40, field: 597, relation: 55, constraint: 84, profile: 498, sample: 800 }, referenced: 6,
      started_at: '2026-10-24T22:30:00+08:00', ended_at: '2026-10-24T22:43:10+08:00', previous: SNAP.s77, job_id: 'job-0124', git_bytes: 396000,
    }),
    snap(SNAP.s77, {
      complete: false, coverage_pct: 79, records: { entity: 33, field: 470, relation: 41, constraint: 66 },
      started_at: '2026-10-18T23:02:00+08:00', ended_at: '2026-10-18T23:32:00+08:00', job_id: 'job-0127', git_bytes: 251000,
    }),
  ];
}

// ---------- 快照对比（EV-21）：只有两组相邻快照有内容 ----------
const er = (key) => entryRef(key);
export const DIFFS = {
  [`${SNAP.s9c}>${SNAP.s5e}`]: [
    { change: 'added', type: 'entity', anchor: 'db:erp-prod/erp.quota_change_log', summary: '新表「配额调整记录」，4 个字段', affected: [] },
    { change: 'modified', type: 'field', anchor: 'db:erp-prod/erp.replenish_std.max_qty', summary: '注释「标准上限」→「标准上限（含区域配额）」', affected: [er('display_qty_std'), er('overage_and_hint')] },
    { change: 'added', type: 'constraint', anchor: 'db:erp-prod/erp.replenish_std.check:chk_qty_range', summary: 'CHECK min_qty ≤ max_qty', affected: [er('r09_store_stricter')] },
    { change: 'removed', type: 'field', anchor: 'db:erp-prod/erp.goods_category.old_code', summary: '字段已删除（10-31 迁移）', affected: [er('goods_category')] },
    { change: 'modified', type: 'profile', anchor: 'db:erp-prod/erp.replenish_std.region_quota_max', summary: '乳制品区域配额上限 140 → 150', affected: [er('display_qty_std'), er('r12_region_quota')] },
    { change: 'modified', type: 'profile', anchor: 'db:erp-prod/erp.replenish_order.status', summary: '取值新增「临时加量」', affected: [er('replenish_order')] },
  ],
  [`${SNAP.s41}>${SNAP.s9c}`]: [
    { change: 'added', type: 'entity', anchor: 'db:erp-prod/erp.promo_plan', summary: '新表「促销备货」，3 个字段', affected: [] },
    { change: 'added', type: 'relation', anchor: 'db:erp-prod/erp.promo_plan.category_code', summary: '取值和 goods_category.category_code 重合 99%', affected: [] },
    { change: 'modified', type: 'field', anchor: 'db:erp-prod/erp.store_request.req_qty', summary: '类型 int → decimal(8,2)', affected: [er('store_request')] },
  ],
};

// ---------- 表结构 ----------
// 字段：[名, 类型, 说明, 置信度, 标记]；标记 pk / fk / masked，gen 'old' 只在 10-31 之前的快照里，'new' 反之
const f = (name, type, note, confidence = 'declared', o = {}) => ({ name, type, note, confidence, pk: false, fk: false, masked: false, ...o });
export const TABLES = {
  goods_category: {
    label: '商品品类', domain: '基础数据', rows: 214, mapped: 'goods_category',
    fields: [f('category_code', 'varchar(16)', 'PK', 'declared', { pk: true }), f('category_name', 'varchar(64)', '品类名称'), f('dept', 'varchar(16)', '所属部门', 'observed'), f('std_ref', 'varchar(32)', '≈ std_catalog.std_code', 'inferred'), f('old_code', 'varchar(16)', '旧编码', 'declared', { gen: 'old' })],
  },
  replenish_std: {
    label: '补货标准表', domain: '基础数据', rows: 1862, mapped: 'display_qty_std',
    fields: [
      f('id', 'bigint', 'PK', 'declared', { pk: true }),
      f('category_code', 'varchar(16)', '外键 → goods_category', 'declared', { fk: true }),
      f('delivery_cycle', 'varchar(8)', '到货周期', 'observed'),
      f('min_qty', 'decimal(8,2)', '标准下限'),
      f('max_qty', 'decimal(8,2)', '注释「标准上限（含区域配额）」', 'declared', { oldNote: '注释「标准上限」' }),
      f('region_quota_max', 'decimal(8,2)', '区域配额上限'),
      f('std_ref', 'varchar(32)', '≈ std_catalog.std_code', 'inferred'),
      f('updated_by', 'varchar(32)', '加盐哈希脱敏', 'observed', { masked: true }),
    ],
  },
  std_catalog: { label: '标准目录', domain: '基础数据', rows: 36, mapped: 'applicable_std', fields: [f('std_code', 'varchar(32)', 'PK', 'declared', { pk: true }), f('std_name', 'varchar(64)', '标准名称'), f('version', 'varchar(16)', '版本')] },
  promo_plan: { label: '促销备货', domain: '基础数据', rows: 128, mapped: null, fields: [f('category_code', 'varchar(16)', '≈ goods_category.category_code', 'observed'), f('store_level', 'varchar(8)', '门店等级', 'observed'), f('extra_qty', 'decimal(8,2)', '加量')] },
  store: { label: '门店', domain: '门店与要货', rows: 86, mapped: 'store', fields: [f('store_code', 'varchar(16)', 'PK', 'declared', { pk: true }), f('store_name', 'varchar(64)', '门店名称（脱敏）', 'declared', { masked: true }), f('level', 'varchar(8)', '门店等级')] },
  store_request: { label: '门店需求', domain: '门店与要货', rows: 412, mapped: 'store_request', fields: [f('req_id', 'bigint', 'PK', 'declared', { pk: true }), f('store_code', 'varchar(16)', '外键 → store', 'declared', { fk: true }), f('category_code', 'varchar(16)', '品类'), f('req_qty', 'decimal(8,2)', '申请量')] },
  supply_contract: { label: '供货合同', domain: '门店与要货', rows: 58, mapped: null, fields: [f('contract_no', 'varchar(20)', 'PK', 'declared', { pk: true }), f('store_code', 'varchar(16)', '≈ store.store_code', 'inferred'), f('valid_to', 'date', '有效期至')] },
  replenish_order: { label: '补货单', domain: '补货单', rows: 3240, mapped: 'replenish_order', fields: [f('order_no', 'varchar(20)', 'PK', 'declared', { pk: true }), f('store_code', 'varchar(16)', '外键 → store', 'declared', { fk: true }), f('category_code', 'varchar(16)', '品类'), f('status', 'varchar(16)', '状态', 'observed')] },
  replenish_order_item: { label: '补货单明细', domain: '补货单', rows: 15872, mapped: 'replenish_order', fields: [f('item_no', 'varchar(20)', 'PK', 'declared', { pk: true }), f('order_no', 'varchar(20)', '外键 → replenish_order', 'declared', { fk: true }), f('goods_code', 'varchar(16)', '商品'), f('request_qty', 'decimal(8,2)', '申请量')] },
  overage_record: { label: '超量记录', domain: '补货单', rows: 296, mapped: 'overage_list', fields: [f('overage_id', 'bigint', 'PK', 'declared', { pk: true }), f('order_no', 'varchar(20)', '≈ replenish_order.order_no', 'observed'), f('item_no', 'varchar(20)', '≈ replenish_order_item.item_no', 'inferred'), f('std_qty', 'decimal(8,2)', '标准值'), f('request_qty', 'decimal(8,2)', '申请值')] },
  arrival_record: { label: '到货记录', domain: '到货与检查', rows: 9214, mapped: null, fields: [f('arrival_no', 'varchar(20)', 'PK', 'declared', { pk: true }), f('order_no', 'varchar(20)', '外键 → replenish_order', 'declared', { fk: true }), f('arrived_at', 'datetime', '到货时间'), f('qty', 'decimal(8,2)', '到货量')] },
  expiry_check: { label: '临期检查', domain: '到货与检查', rows: 5120, mapped: null, fields: [f('check_id', 'bigint', 'PK', 'declared', { pk: true }), f('arrival_no', 'varchar(20)', '外键 → arrival_record', 'declared', { fk: true }), f('remain_days', 'int', '剩余天数'), f('result', 'varchar(8)', '结论', 'observed')] },
};

// 分层摘要（summary.md）：业务域 → 实体；count 是这个域里的实体总数，entities 只列摘要里的重点表
export const DOMAINS = [
  ['基础数据', 8, ['goods_category', 'replenish_std', 'std_catalog', 'promo_plan']],
  ['门店与要货', 11, ['store', 'store_request', 'supply_contract']],
  ['补货单', 9, ['replenish_order', 'replenish_order_item', 'overage_record']],
  ['到货与检查', 14, ['arrival_record', 'expiry_check']],
];

// 关系：[来源 表.字段, 目标 表.字段, 置信度, 依据]
export const RELATIONS = [
  ['replenish_std.category_code', 'goods_category.category_code', 'declared', '外键邻居'],
  ['replenish_std.std_ref', 'std_catalog.std_code', 'inferred', '命名 + 取值重合 96%'],
  ['goods_category.std_ref', 'std_catalog.std_code', 'inferred', '命名推断'],
  ['promo_plan.category_code', 'goods_category.category_code', 'inferred', '取值重合 99%'],
  ['replenish_std.max_qty', 'overage_record.std_qty', 'inferred', '取值重合 98%'],
  ['store_request.store_code', 'store.store_code', 'declared', '外键邻居'],
  ['replenish_order.store_code', 'store.store_code', 'declared', '外键邻居'],
  ['replenish_order_item.order_no', 'replenish_order.order_no', 'declared', '外键邻居'],
  ['overage_record.order_no', 'replenish_order.order_no', 'inferred', '取值重合 97%'],
  ['overage_record.item_no', 'replenish_order_item.item_no', 'inferred', '命名推断'],
  ['supply_contract.store_code', 'store.store_code', 'inferred', '命名推断'],
  ['arrival_record.order_no', 'replenish_order.order_no', 'declared', '外键邻居'],
  ['expiry_check.arrival_no', 'arrival_record.arrival_no', 'declared', '外键邻居'],
];

// 补货相关的 9 张表（ER 视图默认展示的一组，顺序就是排版顺序）
export const ER_GROUP = ['goods_category', 'std_catalog', 'promo_plan', 'replenish_std', 'overage_record', 'store', 'store_request', 'replenish_order', 'replenish_order_item'];

// 补货标准表的手写详情：DDL、脱敏样本、数据画像
export function replenishStdExtras(snapshotId) {
  const gen2 = isNewGen(snapshotId);
  return {
    ddl: [
      'CREATE TABLE replenish_std (',
      '  id bigint PRIMARY KEY,',
      '  category_code varchar(16) NOT NULL',
      '    REFERENCES goods_category(category_code),',
      "  delivery_cycle varchar(8) COMMENT '到货周期',",
      `  max_qty decimal(8,2) COMMENT '${gen2 ? '标准上限（含区域配额）' : '标准上限'}',`,
      "  region_quota_max decimal(8,2) COMMENT '区域配额上限',",
      gen2 ? '  CHECK (min_qty <= max_qty) …);' : '  …);',
    ].join('\n'),
    samples: {
      columns: ['category_code', 'delivery_cycle', 'max_qty', 'updated_by'],
      rows: [['dairy-milk', '每日', '120.00', 'h:7c1e…'], ['dairy-yogurt', '隔日', '80.00', 'h:7c1e…'], ['bakery-bread', '每日', '60.00', 'h:a90b…']],
      shown: 3, max: 20,
    },
    profile: [
      { column: 'delivery_cycle', top_k: [{ value: '每日', pct: 62 }, { value: '隔日', pct: 21 }, { value: '每周', pct: 17 }] },
      { column: 'max_qty', stats: { min: 1, max: 600, null_pct: 0 } },
      { column: 'region_quota_max', stats: { min: 60, max: gen2 ? 150 : 140, null_pct: 63 } },
    ],
  };
}

// ---------- 访问审计（EV-09） ----------
// 语句摘要的写法同网关 conn/context.py：语句类型 + sha256 前 16 位，不记原文；程序逆向是 artifact:<哈希前 12 位>
export function accessLog(sourceId, pid) {
  if (sourceId === REA.id) {
    return [{ at: '2026-10-29T10:15:01+08:00', job_id: 'job-0128', project_id: pid, operation: 'snapshot', statement_digest: `artifact:${REA_ARTIFACT.sha256.slice(0, 12)}`, rows: 25, bytes: 18000, duration_ms: 4578 }];
  }
  if (sourceId === CODE.id) {
    return [{ at: '2026-10-28T14:02:01+08:00', job_id: 'job-0126', project_id: pid, operation: 'snapshot', statement_digest: 'clone:9d4e1a7c3b2f6e08', rows: 0, bytes: 2400000, duration_ms: 8300 }];
  }
  if (sourceId !== 'erp-prod') {
    return [{ at: '2026-10-24T09:30:12+08:00', job_id: 'job-0071', project_id: 'library-desk-1e8f6a07', operation: 'snapshot', statement_digest: 'select:b71c09e4d2a35f86', rows: 1200, bytes: 180000, duration_ms: 2100 }];
  }
  return [
    { at: '2026-10-31T22:31:40+08:00', job_id: 'job-0130', project_id: pid, operation: 'snapshot', statement_digest: 'select:3f2a9c1d0b7e4a65', rows: 20, bytes: 4100, duration_ms: 310 },
    { at: '2026-10-31T22:24:05+08:00', job_id: 'job-0130', project_id: pid, operation: 'snapshot', statement_digest: 'show:8e04b6c2f19d7a35', rows: 12, bytes: 2300, duration_ms: 42 },
    { at: '2026-10-31T22:18:10+08:00', job_id: 'job-0130', project_id: pid, operation: 'snapshot', statement_digest: 'select:c5d1e7a90f3b2648', rows: 621, bytes: 98000, duration_ms: 880 },
    { at: '2026-10-21T22:30:20+08:00', job_id: 'job-0122', project_id: pid, operation: 'sample', statement_digest: 'select:0a6f3e9b5c71d284', rows: 0, bytes: 0, duration_ms: 1200 },
  ];
}

export const sourceRef = ref;
