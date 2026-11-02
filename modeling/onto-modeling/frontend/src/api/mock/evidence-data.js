// mock · 证据源的合成数据（主示例「门店补货单编制」）：登记的业务系统数据源、提供方、采集任务、快照与对比、
// ERP 补货相关的表结构（分层摘要、ER、记录详情、相邻记录）。全部合成；主机只用保留网段 192.0.2.x。
//
// 数值口径同 scenario.js：品类标准上限 120 箱；09 修订通知把乳制品区域配额上限改为 150 箱，
// 10-30 的 ERP 快照（9c1d，产物出处都指向它）里还是 140；10-31 的快照（5e2b）里 ERP 已改成 150。
import { entryRef } from './scenario.js';

export const SNAP = {
  s5e: '20261031-221800-5e2b',
  s9c: '20261030-221500-9c1d',
  s41: '20261024-223000-41aa',
  s77: '20261018-230200-77b0',
};
// 10-31 及以后的快照里 ERP 已按修订通知改过（新约束、字段注释、配额 150）
export const isNewGen = (snapshotId) => String(snapshotId || '') >= '20261031';

const GB = 1024 ** 3;

// ---------- 提供方（EV-10） ----------
const DB_FIELDS = [
  { name: 'host', label: '主机', secret: false },
  { name: 'port', label: '端口', secret: false },
  { name: 'database', label: '库名', secret: false },
  { name: 'username', label: '用户名', secret: false },
  { name: 'password', label: '密码', secret: true },
];
const provider = (id, version, label, kind, o = {}) => ({
  id, version, label, kind,
  evidence_types: kind === 'db' ? ['entity', 'field', 'relation', 'constraint', 'profile', 'sample'] : kind === 'code' ? ['entity', 'operation', 'rule', 'flow'] : ['entity', 'operation'],
  capabilities: kind === 'db' ? ['discover', 'snapshot', 'sample', 'incremental'] : ['discover', 'snapshot', 'incremental'],
  cred_fields: kind === 'db' ? DB_FIELDS : [],
  readonly_method: kind === 'db' ? '只读账号 · 只读会话 · 语句白名单' : '只读令牌',
  runtime: 'subprocess', installed: true, license_note: null, ...o,
});
export function providers() {
  return {
    gateway: { status: 'ok', checked_at: new Date().toISOString() },
    items: [
      provider('file', '1.0.0', '文件', 'file', { cred_fields: [] }),
      provider('db-mysql', '1.0.0', 'MySQL', 'db'),
      provider('db-postgres', '1.0.0', 'PostgreSQL', 'db'),
      provider('db-oracle', '0.9.0', 'Oracle', 'db'),
      provider('db-dm', '0.8.0', '达梦', 'db'),
      provider('code-repo', '0.2.1', '代码仓库', 'code', { cred_fields: [{ name: 'repo_url', label: '仓库地址', secret: false }, { name: 'ref', label: '分支或提交', secret: false }, { name: 'token', label: '访问令牌', secret: true }] }),
      provider('api-openapi', '0.4.0', 'OpenAPI', 'api', { cred_fields: [{ name: 'spec_url', label: '接口文档地址', secret: false }, { name: 'token', label: '访问令牌', secret: true }] }),
      provider('rea-binary', '0.1.0', 'REA 逆向', 'rea', { installed: false, runtime: 'sidecar', license_note: '逆向分析前要确认软件授权范围' }),
      provider('log-file', '0.1.0', '日志', 'log', { installed: false }),
    ],
  };
}

// ---------- 登记的数据源（EV-01，平台全局） ----------
export function sources() {
  return [
    {
      id: 'erp-prod', display_name: 'ERP 生产库', environment: 'production',
      provider: { id: 'db-mysql', version: '1.0.0', label: 'MySQL' },
      connection: { host: '192.0.2.14', port: 3306, database: 'erp', username: 'ro_onto' },
      cred_ref: 'cred://erp-prod/3', secret_fields: [{ name: 'password', set: true, written_at: '2026-10-28T10:20:00+08:00' }],
      scope_allowlist: ['erp.goods_category', 'erp.replenish_std', 'erp.std_catalog', 'erp.promo_plan', 'erp.store*', 'erp.replenish_order*'],
      scope_summary: 'erp.* 42 表',
      egress: [{ host: '192.0.2.14', port: 3306 }],
      access_window: { days: 'daily', start: '22:00', end: '06:00', tz: 'Asia/Shanghai' },
      budget: { max_rows: 2000000, max_bytes: 2 * GB, max_duration_s: 1800 },
      rate_limit: { concurrency: 2, qps: 20, query_timeout_s: 30 },
      masking_policy: 'mask-v3',
      authorization_note: 'ERP 只读账号由信息中心 2026-10-20 开通，用于门店补货单编制本体建模。',
      owner: 'zhangsan', last_job: null, authorized_for_project: null,
    },
    {
      id: 'crm-test', display_name: 'CRM 测试库', environment: 'test',
      provider: { id: 'db-postgres', version: '1.0.0', label: 'PostgreSQL' },
      connection: { host: '192.0.2.20', port: 5432, database: 'crm', username: 'ro_onto' },
      cred_ref: 'cred://crm-test/1', secret_fields: [{ name: 'password', set: true, written_at: '2026-10-12T09:00:00+08:00' }],
      scope_allowlist: ['crm.*'], scope_summary: 'crm.*',
      egress: [{ host: '192.0.2.20', port: 5432 }],
      access_window: null,
      budget: { max_rows: 500000, max_bytes: GB, max_duration_s: 1200 },
      rate_limit: { concurrency: 2, qps: 20, query_timeout_s: 30 },
      masking_policy: 'mask-v3', authorization_note: null, owner: 'modeler01',
      // 别的项目采集过（登记是全局的）
      last_job: { at: '2026-10-24T09:30:00+08:00', state: 'succeeded' }, authorized_for_project: null,
    },
  ];
}

export const GRANTS = { 'erp-prod': { authorized: true, granted_at: '2026-10-20T10:00:00+08:00', granted_by: 'zhangsan' } };

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
  ];
}

// ---------- 快照（EV-18） ----------
const snap = (id, o) => ({
  id, source: ERP, provider: 'db-mysql@1.0.0', complete: true, coverage_pct: 100, referenced: 0, masking_policy: 'mask-v3',
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
  ['promo_plan.category_code', 'goods_category.category_code', 'observed', '取值重合 99%'],
  ['replenish_std.max_qty', 'overage_record.std_qty', 'observed', '取值重合 98%'],
  ['store_request.store_code', 'store.store_code', 'declared', '外键邻居'],
  ['replenish_order.store_code', 'store.store_code', 'declared', '外键邻居'],
  ['replenish_order_item.order_no', 'replenish_order.order_no', 'declared', '外键邻居'],
  ['overage_record.order_no', 'replenish_order.order_no', 'observed', '取值重合 97%'],
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
export function accessLog(sourceId, pid) {
  if (sourceId !== 'erp-prod') {
    return [{ at: '2026-10-24T09:30:12+08:00', job_id: 'job-0071', project_id: 'library-desk-1e8f6a07', operation: 'snapshot', statement_digest: 'SELECT … FROM crm.member', rows: 1200, bytes: 180000, duration_ms: 2100 }];
  }
  return [
    { at: '2026-10-31T22:31:40+08:00', job_id: 'job-0130', project_id: pid, operation: 'snapshot', statement_digest: 'SELECT … FROM erp.replenish_order_item LIMIT 20', rows: 20, bytes: 4100, duration_ms: 310 },
    { at: '2026-10-31T22:24:05+08:00', job_id: 'job-0130', project_id: pid, operation: 'snapshot', statement_digest: 'SHOW INDEX FROM erp.replenish_std', rows: 12, bytes: 2300, duration_ms: 42 },
    { at: '2026-10-31T22:18:10+08:00', job_id: 'job-0130', project_id: pid, operation: 'snapshot', statement_digest: 'SELECT table_name, … FROM information_schema.columns', rows: 621, bytes: 98000, duration_ms: 880 },
    { at: '2026-10-28T10:31:00+08:00', job_id: null, project_id: pid, operation: 'test', statement_digest: 'SELECT 1', rows: 1, bytes: 16, duration_ms: 32 },
    { at: '2026-10-27T22:03:02+08:00', job_id: 'job-0125', project_id: pid, operation: 'discover', statement_digest: '连接被拒绝（401）', rows: 0, bytes: 0, duration_ms: 4000 },
    { at: '2026-10-21T22:30:20+08:00', job_id: 'job-0122', project_id: pid, operation: 'sample', statement_digest: 'SELECT … FROM erp.store LIMIT 20', rows: 0, bytes: 0, duration_ms: 1200 },
  ];
}

export const sourceRef = ref;
