// mock 数据：版式与状态取自设计稿第五版，示例内容是合成的通用场景（门店补货、共享单车、骑手排班、图书借阅、物业报修）
// 只在开发 / 测试构建里加载（见 src/api/transport.js），生产包不含本目录。

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
export const iso = (t) => new Date(t).toISOString().replace(/\.\d{3}Z$/, (m) => m);
const ago = (ms) => iso(Date.now() - ms);
const yesterdayAt = (h, m) => {
  const d = new Date(Date.now() - 24 * HOUR);
  d.setHours(h, m, 0, 0);
  return iso(d.getTime());
};
const dayOf = (month, day, h = 10) => {
  const d = new Date();
  d.setMonth(month - 1, day);
  d.setHours(h, 0, 0, 0);
  if (d.getTime() > Date.now()) d.setFullYear(d.getFullYear() - 1);
  return iso(d.getTime());
};
const todayAt = (h, m) => {
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return iso(d.getTime());
};

export const USERS = {
  zhangsan: { username: 'zhangsan', display_name: '张工', role: 'admin', role_label: '管理员', auth_source: 'auth-server' },
  modeler01: { username: 'modeler01', display_name: '建模者', role: 'user', role_label: '建模者', auth_source: 'auth-server' },
  ops01: { username: 'ops01', display_name: '运维', role: 'ops', role_label: '运维', auth_source: 'auth-server' },
  local: { username: 'local', display_name: '本机用户', role: 'admin', role_label: '管理员', auth_source: 'local' },
};

export function clientConfig({ authEnabled = true, registration = true, maintenance = null } = {}) {
  return {
    api_version: '1',
    server_version: '2.0.0',
    page_api: '1.0',
    auth: { enabled: authEnabled, registration_enabled: registration, local_user: authEnabled ? null : 'local' },
    // 运维管理面独立监听（契约 1.1）；mock 里也走这个地址，验证运维接口按 ops_base_url 拼
    ops_base_url: 'http://127.0.0.1:8101',
    maintenance,
    limits: {
      json_body_bytes: 1048576, file_write_bytes: 4194304, material_upload_bytes: 20971520, attachment_bytes: 20971520,
      attachments_per_message: 10, project_import_bytes: 209715200, pack_upload_bytes: 209715200, page_size_max: 200,
    },
    material_types: ['.md', '.markdown', '.txt', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.csv', '.pdf', '.drawio', '.html', '.htm', '.png', '.jpg', '.jpeg'],
    features: { drawio: true },
    billing: { currency: 'CNY' },
  };
}

export function maintenanceState(mode) {
  return {
    active: true,
    mode,
    message: mode === 'read_only' ? '平台正在迁移数据盘，预计 20 分钟，期间只能查看。' : '平台将在 15:30 升级，预计 20 分钟，期间暂停新会话。',
    started_at: ago(5 * MIN),
    started_by: 'ops01',
    target: { kind: 'core_upgrade', pack: 'core', version: '1.4.3' },
    steps: [
      { id: 'restrict', label: '限制写入', state: 'done', detail: '', progress_pct: null },
      { id: 'drain', label: '排空会话', state: 'current', detail: '还剩 3 个会话', progress_pct: 60 },
      { id: 'upgrade', label: '升级', state: 'pending', detail: '', progress_pct: null },
      { id: 'resume', label: '恢复', state: 'pending', detail: '', progress_pct: null },
    ],
  };
}

export const MAIN_PID = 'retail-restock-3f9a1c2d';
const GOAL = '店长一句话说明门店、品类、库存和到货周期，智能体查基础数据，按补货规则生成补货单';

function card(base) {
  return {
    package_name: base.package_name,
    owner: 'zhangsan',
    created_at: dayOf(10, 1),
    archived: false,
    archived_at: null,
    engine: 'claude',
    engine_label: '稳定引擎',
    research_mode: 'local',
    head: 'a71e03c9',
    last_release: null,
    created_from: 'wizard',
    autobuild: null,
    publishable: null,
    ...base,
  };
}

export function projectCards() {
  return [
    card({
      id: MAIN_PID, name: '门店补货单编制', package_name: 'retail-restock', goal: GOAL, build_mode: 'L2', version_label: 'v0.12',
      industry_label: '零售连锁', last_activity_at: ago(5 * MIN), inbox_count: 18, status: ['active'], landing_page: 'wb-l2',
      slices: { total: 5, confirmed: 1, cells: ['confirmed', 'pending', 'stale', 'not_ready', 'running'] },
    }),
    card({
      id: 'bike-fleet-7b20e4a1', name: '共享单车故障诊断', package_name: 'bike-fleet', goal: '根据骑行上报、锁况与巡检记录判断单车故障类型，并给出处置建议',
      build_mode: 'L4', version_label: 'v0.4', industry_label: '出行服务', last_activity_at: ago(2 * MIN), inbox_count: 0,
      status: ['active', 'autobuild'], landing_page: 'l4-run',
      slices: { total: 5, confirmed: 2, cells: ['confirmed', 'confirmed', 'running', 'not_ready', 'not_ready'] },
      autobuild: {
        run_id: 'run-3', run_no: 3, state: 'running', slice_done: 2, slice_total: 5, progress_pct: 58, eta_ms: 25 * MIN,
        decisions: 9, suspended: 1, budget_used_pct: 41, needs_review: 0, duration_ms: 38 * MIN,
      },
    }),
    card({
      id: 'rider-shifts-52c9d0f3', name: '外卖骑手排班求解', package_name: 'rider-shifts', goal: '按月度订单预测与站点约束求解排班方案，输出每班骑手数、时段和天数',
      build_mode: 'L4', version_label: 'v0.9', industry_label: '即时配送', last_activity_at: ago(HOUR), inbox_count: 4,
      status: ['active', 'autobuild_done'], landing_page: 'inbox',
      slices: { total: 6, confirmed: 5, cells: ['confirmed', 'confirmed', 'needs_review', 'confirmed', 'needs_review', 'not_ready'] },
      autobuild: {
        run_id: 'run-2', run_no: 2, state: 'completed', slice_done: 5, slice_total: 6, progress_pct: 100, eta_ms: null,
        decisions: 13, suspended: 2, budget_used_pct: 76, needs_review: 4, duration_ms: 112 * MIN,
      },
    }),
    card({
      id: 'library-desk-1e8f6a07', name: '图书借阅管理', package_name: 'library-desk', goal: '馆员一句话描述读者情况，判断能不能续借或预约并推荐合适的图书',
      build_mode: 'L2', version_label: 'v1.3', industry_label: '图书馆', last_activity_at: yesterdayAt(18, 20), inbox_count: 0,
      status: ['publishable'], landing_page: 'wb-l2',
      slices: { total: 4, confirmed: 4, cells: ['confirmed', 'confirmed', 'confirmed', 'confirmed'] },
      last_release: { version: 'v1.2', at: dayOf(10, 28) },
      publishable: { gates_passed: true, last_release: { version: 'v1.2', at: dayOf(10, 28) } },
    }),
    card({
      id: 'estate-repair-c4d2b915', name: '物业报修工单处理', package_name: 'estate-repair', goal: '根据业主报修判断故障类别与紧急程度，生成维修工单并跟踪闭环',
      build_mode: 'L2', version_label: 'v0.1', industry_label: '物业服务', last_activity_at: ago(3 * 24 * HOUR), inbox_count: 2,
      status: ['active'], landing_page: 'wb-materials', slices: null,
    }),
    card({
      id: 'demo-takeout-0a9e3f66', name: '外卖订单（示例）', package_name: 'demo-takeout', goal: '示例项目：从下单、接单到配送完成的完整建模流程',
      build_mode: 'L2', version_label: 'v1.0', industry_label: '示例', last_activity_at: dayOf(10, 12), inbox_count: 0,
      archived: true, archived_at: dayOf(10, 12), status: ['archived'], landing_page: 'wb-l2', created_from: 'example',
      slices: { total: 3, confirmed: 3, cells: ['confirmed', 'confirmed', 'confirmed'] },
    }),
  ];
}

export function notifications() {
  return [
    { id: 'n1', kind: 'autobuild_finished', title: '自主构建完成，需复核 4 项', body: '外卖骑手排班求解 · 1 小时 52 分 · 5/6 片完成，挂起 2 个问题', project_id: 'rider-shifts-52c9d0f3', project_name: '外卖骑手排班求解', action: { label: '去复核', page: 'inbox' }, created_at: ago(HOUR), read: false },
    { id: 'n2', kind: 'evidence_conflict', title: '需要你处理：证据冲突', body: '门店补货单编制 · 牛奶陈列上限：文档写 150 箱，数据库快照是 140', project_id: MAIN_PID, project_name: '门店补货单编制', action: { label: '去处理', page: 'inbox' }, created_at: ago(26 * MIN), read: false },
    { id: 'n3', kind: 'autobuild_progress', title: '自主构建中', body: '共享单车故障诊断 · 第 3/5 片 · 预计 25 分钟后完成', project_id: 'bike-fleet-7b20e4a1', project_name: '共享单车故障诊断', action: { label: '查看', page: 'l4-run' }, created_at: ago(10 * MIN), read: true },
    { id: 'n4', kind: 'publish_done', title: '发布完成', body: '图书借阅管理 v1.3 已发布，包清单已交给评测服务', project_id: 'library-desk-1e8f6a07', project_name: '图书借阅管理', action: { label: '查看', page: 'publish' }, created_at: yesterdayAt(18, 20), read: true },
  ];
}

export function engines({ betaInstalled = true } = {}) {
  return {
    items: [
      { id: 'claude', channel: 'stable', label: '稳定引擎', installed: true, unavailable_reason: null, resume: 'native' },
      { id: 'jiuwen', channel: 'beta', label: '内测引擎', installed: betaInstalled, unavailable_reason: betaInstalled ? null : '当前部署未安装', resume: 'replay' },
    ],
    default: 'claude',
  };
}

export function modelSettings() {
  const ctx = { window: 200000, max_output: 32000, compact_at_pct: 80 };
  return {
    personal_allowed: true,
    engines: {
      claude: { source: 'admin', admin_summary: { gateway_host: 'gateway.example.com', model: 'model-standard' }, base_url: '', model: '', api_key_set: false, context: ctx },
      jiuwen: { source: 'admin', admin_summary: { gateway_host: 'gateway.example.com', model: 'model-standard' }, base_url: '', model: '', api_key_set: false, context: ctx },
    },
  };
}

export function evidenceSources() {
  return {
    items: [
      { id: 'erp-prod', display_name: 'ERP 生产库', provider: 'db-mysql', environment: 'production' },
      { id: 'crm-test', display_name: 'CRM 测试库', provider: 'db-postgres', environment: 'test' },
    ],
    total: 2,
    authorized_count: 0,
  };
}

const ui = {
  kernel: { kind: 'kernel', note: '内核：工作台、Studio、资料、发布' },
  page: (id, title) => ({ kind: 'page', pages: [{ id, title }], note: `启用后左栏出现「${title}」` }),
  subpages: { kind: 'subpages', subpages: [{ parent: 'resources', id: 'sources', title: '业务系统数据源' }, { parent: 'resources', id: 'snapshots', title: '采集与快照' }, { parent: 'resources', id: 'schema', title: '证据浏览器' }], note: '资料页多出数据源、快照、证据 3 个子页' },
  none: (note) => ({ kind: 'none', note }),
};
const pack = (o) => ({
  version: o.latest || '1.0.0', latest: o.latest || '1.0.0', channel: 'stable', publisher: { kind: 'official', label: '官方' },
  ui: null, batch: 'first', badges: [], meta: null, project_status: null, locked_version: null, usage_note: null, default_on: false, ...o,
});

export function packs({ offline = true } = {}) {
  return [
    pack({ name: 'industry-retail', label: '零售连锁', category: 'industry', latest: '1.0.3', description: '术语 312 · 规则 48 · 参考对象 36 · 示例 2 · 带「合规检查」关卡', default_on: true }),
    pack({ name: 'industry-property', label: '物业服务', category: 'industry', latest: '1.2.0', description: '术语 268 · 规则 31 · 参考对象 22' }),
    pack({ name: 'industry-workorder', label: '通用工单', category: 'industry', latest: '0.8.1', description: '术语 96 · 参考对象 9' }),
    pack({ name: 'sys-erp-generic', label: 'ERP 通用表结构', category: 'system', latest: '0.3.0', description: 'ERP 常见表结构与字段口径', default_on: true }),
    pack({ name: 'sys-wms-generic', label: 'WMS（仓储）', category: 'system', latest: '0.4.0', description: '库位与库存表结构' }),
    pack({ name: 'sys-pos-generic', label: 'POS 收银', category: 'system', latest: '0.2.1', description: '常用表结构' }),
    pack({ name: 'core', label: '基础建模', category: 'capability', latest: '1.4.2', description: '资料分析、对象 / 操作 / 规则抽取、校验、增量更新、版本', ui: ui.kernel, project_status: 'required', default_on: true }),
    pack({ name: 'trace', label: '场景走查', category: 'capability', latest: '0.9.1', description: '每片多出「场景」一步；全量回归串在「校验与走查」里', ui: ui.page('scenarios', '场景走查'), badges: ['recommended'], default_on: true }),
    pack({ name: 'knowledge', label: '知识沉淀', category: 'capability', latest: '0.6.0', description: '把对话里的纠错、惯例沉淀成条目，可提名进行业包；已有知识不装也会加载', ui: ui.page('knowledge', '知识沉淀'), default_on: true }),
    pack({ name: 'codegen', label: '代码生成与求解', category: 'capability', latest: '0.9.2', description: '脚本生成、沙箱验证、求解契约', ui: ui.page('scripts', '代码生成'), default_on: true }),
    pack({ name: 'reverse', label: '系统逆向建模', category: 'capability', latest: '0.3.0', description: '连接数据源、采集快照、三方对账', ui: ui.subpages, default_on: true }),
    pack({ name: 'diagram', label: '图示', category: 'capability', latest: '0.5.0', description: '流程图生成与校验；不启用就没有流程图卡和流程图条目', ui: ui.none('无独立页面 · 对话里出现流程图卡，Studio 多出「流程图」类型'), default_on: true }),
    pack({ name: 'websearch', label: '联网调研', category: 'capability', latest: '0.2.0', description: '离线部署已禁用', ui: ui.none('无独立页面，在对话里生效'), batch: 'second', project_status: offline ? 'offline' : null }),
    pack({ name: 'migrate', label: '迁移复用', category: 'capability', latest: '0.2.0', description: '复用旧项目里的对象', ui: ui.none('无独立页面，在对话里生效'), batch: 'second' }),
    pack({ name: 'evo', label: '反馈演进', category: 'capability', latest: '0.3.0', description: '按外部反馈修订本体', ui: ui.none('无独立页面，在对话里生效'), batch: 'second' }),
  ];
}

// ---------- 项目内 ----------
export function projectMeta(cardItem) {
  const { industry_label, slices, inbox_count, status, autobuild, publishable, landing_page, ...meta } = cardItem;
  return meta;
}

export function extensions(enabled = ['trace', 'knowledge', 'codegen', 'reverse', 'diagram']) {
  const nav = [
    { id: 'wb-l2', title: '工作台', icon: 'layout', kind: 'kernel', badge: { source: 'inbox', count: 18 }, order: 0, overflow: false },
    { id: 'studio', title: 'Studio', icon: 'graph', kind: 'kernel', order: 1, overflow: false },
    { id: 'resources', title: '资料', icon: 'folder', kind: 'kernel', order: 2, overflow: false },
    { id: 'publish', title: '发布', icon: 'upload', kind: 'kernel', order: 3, overflow: false },
  ];
  const plug = [
    ['trace', 'scenarios', '场景走查', 'flask'],
    ['knowledge', 'knowledge', '知识沉淀', 'book'],
    ['codegen', 'scripts', '代码生成', 'code'],
  ].filter(([p]) => enabled.includes(p));
  plug.forEach(([p, id, title, icon], i) => nav.push({ id, title, icon, kind: 'plugin', pack: p, order: 10 + i, overflow: false }));
  return {
    active_hash: 'b41e09c2',
    pending_hash: null,
    page_api: '1.0',
    nav,
    subpages: enabled.includes('reverse') ? [
      { parent: 'resources', id: 'sources', title: '业务系统数据源', pack: 'reverse' },
      { parent: 'resources', id: 'snapshots', title: '采集与快照', pack: 'reverse' },
      { parent: 'resources', id: 'schema', title: '证据浏览器', pack: 'reverse' },
    ] : [],
    pages: plug.map(([p, id]) => ({ id, pack: p, page_api: '>=1.0 <2.0', data: { artifacts: [], reports: [], nodes: [] }, actions: [], pending: false })),
    // 包声明的界面入口（B5c）：快捷项放输入框「全部命令」旁，附加操作放对应插件页的操作区（R22）
    entries: [
      ...(enabled.includes('trace') ? [{ id: 'trace.walk', kind: 'quick_action', label: '交互走查', where: 'composer', command: '/ontology-trace-interactive', pack: 'trace' }] : []),
      ...(enabled.includes('knowledge') ? [{ id: 'knowledge.export', kind: 'extra_action', label: '导出知识清单', where: 'knowledge', command: '/ontology-knowledge-export', pack: 'knowledge' }] : []),
    ],
    artifact_types: enabled.includes('diagram') ? ['diagram.flowchart'] : [],
    views: [],
    // 包选用的内核视图（B5c）：ER 视图放进 Studio 的视图切换（R23）；依赖证据源的两种本期不下发
    view_choices: enabled.includes('diagram') ? [{ id: 'er_view', pack: 'diagram' }] : [],
    gates: [],
    config: [],
    disabled_pages: [],
  };
}

export const PACK_LABEL = { trace: '场景走查', knowledge: '知识沉淀', solver: '代码生成与求解', reverse: '系统逆向建模', diagram: '图示' };

export function inboxCounts() {
  return {
    items: [],
    next_cursor: null,
    counts: {
      total: 18,
      by_filter: { all: 18, autobuild_review: 8, decision: 2, evidence_conflict: 2, change_list: 1, stale: 3, resumable: 1, feedback: 1 },
      by_pack: { trace: 3 },
    },
    today: { handled: 0, total: 18, eta_minutes: 40, remaining_by_filter: {} },
    autobuild_summary: null,
  };
}

const node = (o) => ({
  kind: 'agent', placement: 'spine', section: 'required', state: 'not_ready', display_state: 'not_ready',
  flags: { needs_review: false, disabled: false, backfill: false }, progress: null, version: 'core@0.3.0', per_slice: false,
  note: null, blocks: [], actions: [], plugin: null, ...o,
});

export function flow({ trace = true } = {}) {
  const nodes = [
    node({ id: 'core.research', title: '资料分析', state: 'confirmed', display_state: 'confirmed' }),
    node({ id: 'rev.evidence', title: '系统取证', placement: 'step', state: 'confirmed', display_state: 'confirmed', version: 'rev@0.3.0', note: '登记了业务系统数据源才有这一步', plugin: { name: 'reverse', label: '系统逆向建模' } }),
    node({ id: 'core.model', title: '建模', state: 'running', display_state: 'running', progress: { done: 3, total: 5, label: '3/5 片' }, per_slice: true }),
    node({ id: 'core.validate', title: trace ? '校验与走查' : '校验', note: trace ? '场景走查插件把全量回归串在这一步里' : null }),
    // 全量回归随 core 拆出、串在校验之后（spine，contract-changes B4d 合并第 4 条）
    ...(trace ? [node({ id: 'trace.trace', title: '全量回归', state: 'stale', display_state: 'stale', version: 'trace@0.10.0', note: '场景走查 · 09 修订通知改了陈列量标准，3 个场景过期', actions: [{ kind: 'refresh', label: '刷新', target: 'scenarios' }], plugin: { name: 'trace', label: '场景走查' } })] : []),
    node({ id: 'core.consistency', title: '全局一致性' }),
    node({ id: 'core.publish', title: '发布', kind: 'human', actions: [{ kind: 'start', label: '去发布', target: 'publish' }] }),
    node({ id: 'retail.compliance', title: '合规检查', placement: 'gate', section: 'pre_publish', state: 'blocked', display_state: 'blocked', version: 'industry-retail@1.0.3', note: `零售行业包带来；${trace ? '校验与走查' : '校验'}确认后才能运行` }),
    node({ id: 'diagram.flowchart', title: '流程图', placement: 'branch', section: 'optional', state: 'confirmed', display_state: 'confirmed', version: 'diagram@0.6.0', plugin: { name: 'diagram', label: '图示' } }),
    // 节点包的脚本节点（B5c）：最近一次运行结果在建模步骤抽屉里展开看（R24）；last_run 是待后端补的 FL-01 字段（gaps「UI 文案」）
    node({ id: 'stock-check.run', title: '库存口径检查', kind: 'script', placement: 'branch', section: 'optional', state: 'confirmed', display_state: 'confirmed', version: 'stock-check@0.1.0', actions: [{ kind: 'start', label: '重新运行' }], plugin: { name: 'stock-check', label: '库存口径检查' }, last_run: { state: 'passed', summary: '15 个对象的库存口径一致', at: '2026-10-31T07:12:00Z' } }),
    node({ id: 'core.metric', title: '指标', placement: 'branch', section: 'optional', state: 'pending_confirm', display_state: 'pending', actions: [{ kind: 'confirm', label: '去确认', target: 'inbox' }] }),
    node({ id: 'codegen.codegen', title: '代码生成', placement: 'branch', section: 'optional', state: 'ready', display_state: 'ready', version: 'codegen@0.9.2', actions: [{ kind: 'start', label: '开始' }, { kind: 'skip', label: '跳过' }], plugin: { name: 'codegen', label: '代码生成与求解' } }),
    node({ id: 'codegen.solver', title: '求解适配', placement: 'branch', section: 'optional', state: 'skipped', display_state: 'skipped', version: 'codegen@0.9.2', actions: [{ kind: 'restore', label: '恢复' }], plugin: { name: 'codegen', label: '代码生成与求解' } }),
    node({ id: 'core.update', title: '增量更新', placement: 'on_demand', section: 'on_demand', state: 'ready', display_state: 'ready', note: '资料区有 1 个新文件：09 修订通知', actions: [{ kind: 'start', label: '开始', target: 'incremental' }] }),
    node({ id: 'evo.evolution', title: '反馈演进', placement: 'on_demand', section: 'on_demand', note: '收到外部反馈后出现', version: 'evo@0.3.0', plugin: { name: 'evo', label: '反馈演进' } }),
  ];
  const st = (a) => {
    const keys = ['object', 'operation', 'rule', 'check', 'scenario'];
    const out = {};
    (trace ? keys : keys.slice(0, 4)).forEach((k, i) => { out[k] = a[i]; });
    return out;
  };
  return {
    flow_hash: 'b41e09c2a7',
    nodes,
    edges: [
      { from: 'core.research', to: 'rev.evidence' }, { from: 'rev.evidence', to: 'core.model' }, { from: 'core.model', to: 'core.validate' },
      ...(trace ? [{ from: 'core.validate', to: 'trace.trace' }, { from: 'trace.trace', to: 'core.consistency' }] : [{ from: 'core.validate', to: 'core.consistency' }]), { from: 'core.consistency', to: 'retail.compliance' }, { from: 'retail.compliance', to: 'core.publish' },
    ],
    slice_stages: [
      { id: 'object', label: '对象' }, { id: 'operation', label: '操作' }, { id: 'rule', label: '规则' }, { id: 'check', label: '写入即查' },
      ...(trace ? [{ id: 'scenario', label: '场景', plugin: 'trace' }] : []),
    ],
    slices: [
      { id: 's1', name: '补货申请与门店需求', core_flow: '受理三道校验', group: 'done', stages: st(['confirmed', 'confirmed', 'skipped', 'confirmed', 'confirmed']), note: '已确认生效 · 资料里没有规则，已跳过', no_rules: true },
      { id: 's2', name: '安全库存测算', core_flow: '按类取标准再取严', group: 'wait', stages: st(['confirmed', 'confirmed', 'pending_confirm', 'confirmed', 'ready']), note: '2 条规则待确认', no_rules: false },
      { id: 's3', name: '陈列量核定', core_flow: '到货周期 × 陈列上限', group: 'wait', stages: st(['confirmed', 'stale', 'stale', 'not_ready', 'stale']), note: '3 项过期 · 来自 09 修订通知', no_rules: false },
      { id: 's4', name: '临期与损耗检查', core_flow: '临期损耗限值', group: 'todo', stages: st(['ready', 'not_ready', 'not_ready', 'not_ready', 'not_ready']), note: '未开始 · 推荐下一个做', no_rules: false },
      { id: 's5', name: '超量与审批提示', core_flow: '超量清单与审批提示', group: 'now', stages: st(['confirmed', 'pending_confirm', 'draft', 'running', 'not_ready']), note: '正在做 · 停在「操作」', no_rules: false },
    ],
    blueprint: { state: 'confirmed' },
    suggestions: null,
    notices: [{ kind: 'new_materials', count: 1, files: ['resource/others/09-修订通知.docx'], text: '资料区有 1 个新文件（09 修订通知），建议走增量更新', action: { type: 'navigate', page: 'incremental' } }],
    // 以下按 P1 的 /flow 补齐（contract-changes B3a 第 1 条）：抽屉分组、命令归属、项目进度、旧版阶段名
    sections: [['required', '必经步骤'], ['pre_publish', '发布前检查'], ['optional', '可选步骤'], ['on_demand', '有需要时']]
      .map(([id, title]) => ({ id, title, nodes: nodes.filter((n) => n.section === id).map((n) => n.id) })),
    commands: {
      research: 'core.research', 'extract-object': 'core.model', 'extract-operation': 'core.model', 'extract-rule-metric': 'core.metric',
      validate: 'core.validate', consistency: 'core.consistency', 'validate-diagrams': 'diagram.flowchart', flowchart: 'diagram.flowchart', update: 'core.update',
      codegen: 'codegen.codegen', 'codegen-scan': 'codegen.codegen', 'solver-evaluator': 'codegen.solver', evolution: 'evo.evolution', ...(trace ? { trace: 'trace.trace' } : {}),
    },
    progress: { done: 2, total: 6 },
    stage: '逻辑已提取',
  };
}

// P1 后端（B3a）的 /flow 原样形状：flow.yaml 的 10 个阶段加按需的增量更新，状态只有 4 种，
// actions 带命令文本，分组在顶层 sections。?mock=flow:p1 用它核对前端的归一处理。
export function flowP1() {
  const n = (id, title, placement, section, state, actions, extra = {}) => ({
    id, title, kind: 'agent', placement, section, state, flags: { needs_review: false, disabled: false, backfill: false },
    display_state: state, progress: null, version: '', per_slice: false, note: null, blocks: [],
    actions: actions.map(([label, target]) => ({ kind: 'start', label, target })), plugin: null,
    optional: section === 'optional', panel: false, counts_in_progress: true, ...extra,
  });
  const nodes = [
    n('core.research', '资料分析', 'spine', 'required', 'confirmed', [['联网资料分析', '/ontology-research '], ['仅本地资料', '/ontology-research 仅基于本地文档 '], ['追加分析', '/ontology-research 追加']]),
    n('core.flowchart', '流程图', 'branch', 'optional', 'confirmed', [['创建/打磨流程图', '/ontology-flowchart'], ['全量刷新', '/ontology-flowchart 全量刷新'], ['校验流程图', '/ontology-validate-diagrams']]),
    n('core.object', '对象提取', 'spine', 'required', 'confirmed', [['提取对象', '/ontology-extract-object']]),
    n('core.operation', '操作提取', 'spine', 'required', 'confirmed', [['提取操作', '/ontology-extract-operation']]),
    n('core.rule_metric', '规则与指标', 'branch', 'optional', 'ready', [['提取规则与指标', '/ontology-extract-rule-metric']]),
    n('core.validate', '质量检查', 'spine', 'required', 'ready', [['运行质量检查', '/ontology-validate']]),
    n('core.mock', '示例数据', 'spine', 'required', 'not_ready', [['生成示例数据', '/ontology-mock']]),
    n('core.trace', '场景验证', 'spine', 'required', 'not_ready', [['批量验证', '/ontology-trace 全部'], ['交互走查', '/ontology-trace 交互']]),
    n('core.codegen', '代码生成', 'branch', 'optional', 'not_ready', [['扫描候选', '/ontology-codegen-scan'], ['生成脚本', '/ontology-codegen ']]),
    n('core.solver', '求解适配', 'branch', 'optional', 'ready', [], { panel: true }),
    n('core.update', '增量更新', 'on_demand', 'on_demand', 'ready', [['增量更新', '/ontology-update ']], { counts_in_progress: false }),
  ];
  const after = [['core.research', 'core.flowchart'], ['core.research', 'core.object'], ['core.object', 'core.operation'], ['core.operation', 'core.rule_metric'],
    ['core.operation', 'core.validate'], ['core.validate', 'core.mock'], ['core.mock', 'core.trace'], ['core.trace', 'core.codegen'], ['core.operation', 'core.solver'], ['core.operation', 'core.update']];
  const send = (id, label, text, primary) => ({ id, label, primary, disabled: false, reason: '主线下一步', priority: 4, action: { type: 'send', text, command: text.split(' ')[0] } });
  return {
    flow_hash: 'p1e07a3c',
    nodes,
    edges: after.map(([from, to]) => ({ from, to })),
    sections: [['required', '必经步骤'], ['pre_publish', '发布前检查'], ['optional', '可选步骤'], ['on_demand', '有需要时']]
      .map(([id, title]) => ({ id, title, nodes: nodes.filter((x) => x.section === id).map((x) => x.id) })),
    slice_stages: [],
    slices: [],
    suggestions: { mode: 'normal', primary: send('core.validate#0', '运行质量检查', '/ontology-validate', true), secondary: [], all_commands: true },
    notices: [],
    stage: '逻辑已提取',
    progress: { done: 4, total: 10 },
    commands: {
      research: 'core.research', flowchart: 'core.flowchart', 'validate-diagrams': 'core.flowchart', 'extract-object': 'core.object',
      'extract-operation': 'core.operation', 'extract-rule-metric': 'core.rule_metric', validate: 'core.validate', mock: 'core.mock',
      trace: 'core.trace', codegen: 'core.codegen', 'codegen-scan': 'core.codegen', 'solver-evaluator': 'core.solver', update: 'core.update',
    },
  };
}

export function conversations() {
  return [
    { id: 'c7a1e2f03b44', title: '超量与审批提示', title_manual: true, created_at: todayAt(9, 30), updated_at: ago(MIN), live: true, state: 'idle', kind: 'chat', run_id: null },
    { id: 'b2d94c11e0a7', title: '陈列量核定返工', title_manual: true, created_at: dayOf(10, 30), updated_at: ago(2 * HOUR), live: false, state: 'closed', kind: 'chat', run_id: null },
    { id: '9e0f3a7c6d21', title: '资料分析答疑', title_manual: true, created_at: dayOf(10, 29), updated_at: ago(26 * HOUR), live: false, state: 'closed', kind: 'chat', run_id: null },
    { id: '4c8b1d2e9f30', title: '默认会话', title_manual: false, created_at: dayOf(10, 28), updated_at: dayOf(10, 28), live: false, state: 'closed', kind: 'chat', run_id: null },
  ];
}

// ---------- 出处 ----------
const A = {
  ea: 'file:resource/others/03-样例补货单-A门店.docx#p:5',
  e92: 'file:resource/others/02-门店补货规则.docx#p:9.2',
  edb: 'db:erp-prod/erp.replenish_std.max_qty',
  e114: 'file:resource/others/02-门店补货规则.docx#p:11.4',
  eb: 'file:resource/others/03-样例补货单-B.docx#p:4',
};
export const ANCHORS = A;

// 把带 <mark> 的原文拆成 text + highlights
function res(anchor, source_label, location_label, marked, confidence = null) {
  let text = '';
  const highlights = [];
  marked.split(/(<mark>.*?<\/mark>)/).forEach((part) => {
    const m = part.match(/^<mark>(.*)<\/mark>$/);
    if (m) {
      highlights.push({ start: text.length, end: text.length + m[1].length });
      text += m[1];
    } else text += part;
  });
  const scheme = anchor.split(':')[0];
  return {
    anchor, scheme, resolved: true, source_label, location_label, text: text.replace(/<br>/g, '\n'), highlights, confidence,
    snapshot_id: scheme === 'db' ? '20261030-221500-9c1d' : null, page: scheme === 'db' ? 'schema' : 'resources',
  };
}

export const ANCHOR_RESOLUTIONS = {
  [A.ea]: res(A.ea, '03-样例补货单-A门店.docx', '第 5 节 超量说明', '牛奶：品类标准上限 120 箱，<mark>门店申请 96 箱（严于标准，按门店执行）</mark>。'),
  [A.e92]: res(A.e92, '02-门店补货规则.docx', '§9.2 超量处理', '门店申请与标准不一致时，应在补货单中逐条列出超量项，注明标准值与申请值，<mark>门店申请严于标准的按门店执行</mark>，超出标准的须经区域审批。'),
  [A.edb]: res(A.edb, '业务系统 ERP', '补货标准表 · 标准上限', '字段「标准上限」，关联到商品品类表。<br>样本（合成）：乳制品 / 牛奶 / <mark>120.00</mark>', 'declared'),
  [A.e114]: res(A.e114, '02-门店补货规则.docx', '§11.4 临时加量', '对临时加量的商品，<mark>店长应生成提示并提交区域审批</mark>，审批通过前不得下单。'),
  [A.eb]: res(A.eb, '03-样例补货单-B.docx', '第 4 节', '牛奶按<mark>区域统一配额 ≤100 箱</mark> 执行。'),
};

// ---------- 工作台对话：wb-l2 设计稿那一屏 ----------
const ref = (kind, key, name) => ({ kind, id: `${kind}:${key}`, key, name });
export const REFS = {
  order: ref('object', 'replenish_order', '补货单'),
  request: ref('object', 'store_request', '门店特殊需求'),
  category: ref('object', 'goods_category', '商品品类'),
  safety: ref('object', 'safety_stock_std', '安全库存标准'),
  display: ref('object', 'display_qty_std', '陈列量标准'),
  approval: ref('object', 'approval_hint', '审批提示'),
  ledger: ref('object', 'replenish_ledger', '补货台账'),
  overage: ref('object', 'overage_list', '超量清单'),
  op: ref('operation', 'overage_and_hint', '超量与审批提示'),
  r11: ref('rule', 'r11_temp_increase', 'R-11 临时加量须审批'),
  r09: ref('rule', 'r09_store_stricter', 'R-09 门店申请取严'),
};

export const QUESTION_ID = 'q-7f3a';
export const STEP_CARD_ID = 'card-step-op';
export const FLOW_CARD_ID = 'card-flow-dev';
export const ROUND_CARD_ID = 'card-round-cs31';
export const CHANGESET_ID = 'cs-31';

export function stepOutputCard(confirmed = false) {
  return {
    card_id: STEP_CARD_ID,
    card_type: 'step_output',
    eyebrow: '操作 · 本轮新抽出',
    title: '超量与审批提示',
    subtitle: '',
    status: confirmed ? { key: 'confirmed', label: '已确认' } : { key: 'pending', label: '待确认' },
    plugin: null,
    open: { kind: 'page', target: 'studio-table', ref: REFS.op },
    entry: REFS.op,
    state: confirmed ? 'confirmed' : 'pending',
    inputs: [REFS.order, { text: '（草稿）' }, REFS.request, REFS.category, REFS.safety, REFS.display],
    steps: [
      { n: 1, text_md: '读取补货单草稿中各商品的申请量[^1]', anchors: [{ n: 1, anchor: A.ea }], awaiting_question_id: null },
      confirmed
        ? { n: 2, text_md: '按 [[商品品类]] 的「适用标准」逐类取上限，同一商品在多类出现时取最严值（规则 R-09）', anchors: [], awaiting_question_id: null }
        : { n: 2, text_md: '按 [[商品品类]] 的适用标准取标准上限', anchors: [], awaiting_question_id: QUESTION_ID },
      { n: 3, text_md: '逐项比较：严于标准记为「按门店执行」，超出标准记为「需审批」[^2][^3]', anchors: [{ n: 2, anchor: A.e92 }, { n: 3, anchor: A.edb }], awaiting_question_id: null },
      { n: 4, text_md: '对「需审批」的项生成 [[审批提示]]，挂到 [[补货台账]][^4]', anchors: [{ n: 4, anchor: A.e114 }], awaiting_question_id: null },
    ],
    outputs: [REFS.overage, REFS.approval],
    rules: [{ ref: REFS.r11, state: 'draft' }],
    sources: [
      { n: 1, anchor: A.ea, label: '样例补货单 A §5', scheme: 'file' },
      { n: 2, anchor: A.e92, label: '补货规则 §9.2', scheme: 'file' },
      { n: 3, anchor: A.edb, label: 'ERP 补货标准表', scheme: 'db' },
      { n: 4, anchor: A.e114, label: '补货规则 §11.4', scheme: 'file' },
    ],
    entry_links: [REFS.order, REFS.request, REFS.category, REFS.safety, REFS.display, REFS.approval, REFS.ledger, REFS.overage],
  };
}

export function flowchartCard(locked = false) {
  return {
    card_id: FLOW_CARD_ID,
    card_type: 'flowchart',
    eyebrow: '流程图 · 草稿',
    title: '超量处理流程',
    subtitle: '按补货规则第 9 章画出 · 8 个节点、1 个判断',
    status: null,
    plugin: { name: 'diagram', label: '图示' },
    open: { kind: 'dialog', target: 'flowchart', ref: { type: 'diagram.flowchart', key: 'overage-flow', title: '超量处理流程' } },
    artifact: { type: 'diagram.flowchart', key: 'overage-flow', title: '超量处理流程' },
    state: 'draft',
    summary: '8 个节点、1 个判断',
    locked,
    thumbnail: {
      nodes: [
        { id: 'n1', type: 'start', label: '受理申请', pending: false },
        { id: 'n2', type: 'step', label: '读取申请量', pending: false },
        { id: 'n3', type: 'step', label: '取标准上限 ?', pending: true },
        { id: 'n4', type: 'decision', label: '严于标准?', pending: false },
        { id: 'n5', type: 'step', label: '按门店执行', pending: false },
        { id: 'n6', type: 'step', label: '生成审批提示', pending: false },
      ],
      edges: [
        { from: 'n1', to: 'n2', label: '' }, { from: 'n2', to: 'n3', label: '' }, { from: 'n3', to: 'n4', label: '' },
        { from: 'n4', to: 'n5', label: '是' }, { from: 'n4', to: 'n6', label: '否' },
      ],
    },
  };
}

const diff = (kind, text) => ({ kind, text });
export const OPTION_PREVIEWS = {
  A: [
    diff('header', '操作「超量与审批提示」· 步骤'),
    diff('del', '2. 按 [[商品品类]] 的适用标准取标准上限'),
    diff('add', '2. 按 [[商品品类]] 的「适用标准」逐类取上限：安全库存查 [[安全库存标准]]，陈列量查 [[陈列量标准]]'),
    diff('add', '   同一商品在多类出现时取最严值（规则 R-09）'),
    diff('header', '规则引用'),
    diff('add', '+ [[R-09 门店申请取严]]'),
    diff('header', '场景（新增 2 个，来自样例补货单 A、B）'),
    diff('add', '+ 场景：门店牛奶申请严于标准 → 按门店执行'),
    diff('add', '+ 场景：门店零食申请超出标准 → 生成审批提示'),
  ],
  B: [
    diff('header', '操作「超量与审批提示」· 步骤'),
    diff('del', '2. 按 [[商品品类]] 的适用标准取标准上限'),
    diff('add', '2. 读取 [[区域统一配额]] 作为比较基准'),
    diff('header', '新增对象'),
    diff('add', '+ 对象「区域统一配额」'),
    diff('header', '影响'),
    diff('add', '  安全库存测算、陈列量核定、临期与损耗检查 3 个切片需要补数据来源'),
  ],
  C: [
    diff('header', '操作「超量与审批提示」· 输入'),
    diff('add', '+ 标准值（用户在补货时提供，按商品逐项填写）'),
    diff('header', '步骤'),
    diff('del', '2. 按 [[商品品类]] 的适用标准取标准上限'),
    diff('add', '2. 使用用户提供的标准值'),
    diff('header', '影响'),
    diff('add', '  场景需要人工输入，不能自动推演'),
  ],
  D: [diff('header', '自定义方向'), diff('add', '  助手会先复述你的方向，再给出改动预览')],
};

export function decisionQuestion() {
  const opt = (id, label, consequence, extra = {}) => ({
    id, key: id, label, consequence, recommended: false, preview: OPTION_PREVIEWS[id], custom: false, ...extra,
  });
  return {
    question_id: QUESTION_ID,
    kind: 'decision',
    conversation_id: 'c7a1e2f03b44',
    exec_id: 'ex-0412',
    step_label: '步骤 2',
    eyebrow: '需要你决定 · 步骤 2',
    title: '补货标准从哪里来？',
    background_md: '补货规则 §9.2 写的是按适用标准取值后再和门店申请比较[^2]；样例补货单 B 直接用了区域统一配额[^5]。',
    anchors: [{ n: 2, anchor: A.e92 }, { n: 5, anchor: A.eb }],
    options: [
      opt('A', '按适用标准逐类取上限，再和门店申请取严', '复用已确认的「安全库存标准」「陈列量标准」，不新增对象', { recommended: true }),
      opt('B', '一律用区域统一配额', '要新增对象「区域统一配额」，另外 3 个切片要补数据来源'),
      opt('C', '每次补货时由店长提供', '操作多一个输入；场景要人工填值，不能自动推演'),
      opt('D', '其他，我来说', '先复述你的方向，再给改动预览', { custom: true }),
    ],
    multi: false,
    allow_custom: true,
    custom_placeholder: '说说你的方向…',
    state: 'open',
    answer: null,
    inbox_item_id: `decision:${QUESTION_ID}`,
    affected: [REFS.op, REFS.safety, REFS.display],
  };
}

// 一轮结束就合入成版本（F3）：state merged | reverted
export function roundChangesCard(optionKey, state = 'merged') {
  const op = { kind: 'operation', label: '操作', items: [{ change: 'mod', target: REFS.op, text: '操作 [[超量与审批提示]] 的步骤 2', comparable: true }] };
  const rule = { kind: 'rule', label: '规则', items: [{ change: 'add', target: REFS.r09, text: '规则引用 [[R-09 门店申请取严]]', comparable: true }] };
  const sc = { kind: 'scenario', label: '场景', items: [{ change: 'test', target: { type: 'trace.scenario', key: 's-21', title: '门店更严 → 按门店执行' }, text: '新增 2 个验收场景，推演 2 / 2 通过', comparable: true }] };
  return {
    card_id: ROUND_CARD_ID,
    card_type: 'round_changes',
    eyebrow: '这一轮改了什么',
    title: '改了 3 处',
    subtitle: '',
    status: state === 'reverted' ? { key: 'skipped', label: '已撤回' } : { key: 'confirmed', label: 'v0.13' },
    plugin: null,
    open: null,
    changeset_id: CHANGESET_ID,
    option_key: optionKey,
    checks: { errors: 0, warnings: 0, pending: 1 },
    hints: [{ tone: 'info', text: '1 处指向第 4 段待建的内容' }],
    groups: [op, rule, sc],
    state,
    merged_version_label: 'v0.13',
    reverted_version_label: state === 'reverted' ? 'v0.14' : null,
  };
}

export function wbHistory() {
  const t0 = todayAt(15, 18);
  const t1 = todayAt(15, 21);
  let seq = 1500;
  const e = (type, data, ts = t1) => ({ id: String(++seq), type, ts, project_id: MAIN_PID, conversation_id: 'c7a1e2f03b44', data });
  return [
    e('day.marker', { label: '今天' }, t0),
    e('notice', { kind: 'merged', text: '切片「临期与损耗检查」已确认生效 · 版本 v0.12', version_label: 'v0.12', changeset_id: 'cs-30', align: 'center' }, t0),
    e('message.user', { message_id: 'm-u-41', text: '继续做「超量与审批提示」', attachments: [], references: [], origin: null, delivery: 'immediate' }, todayAt(15, 20)),
    e('message.assistant', { message_id: 'm-a-42', text_md: '好。这一片要把门店申请和标准逐项比较，生成超量清单和审批提示。', anchors: [], entry_links: [], continued: false }),
    e('progress', {
      group_id: 'pg-42', title: '已完成 3 步', desc: '读全资料 · 抽出操作 · 写入即查', state: 'done', started_at: t1, duration_ms: 62000, collapsed: true,
      steps: [
        { id: 's1', text: '读完补货规则第 9、11 章和样例补货单 A、B，覆盖 100%', meta: '18 秒', state: 'done', duration_ms: 18000 },
        { id: 's2', text: '抽出 1 个操作「超量与审批提示」，4 个步骤都带出处', meta: '41 秒', state: 'done', duration_ms: 41000 },
        { id: 's3', text: '写入即查：0 个错误，1 个提醒（审批提示交给谁还没定义）', meta: '3 秒', state: 'done', duration_ms: 3000 },
      ],
    }),
    e('message.assistant', { message_id: 'm-a-43', text_md: '抽出了 1 个操作，4 个步骤都有出处。第 2 步资料里有两种口径，我先停在这里，等你定。', anchors: [], entry_links: [], continued: true }),
    e('card', stepOutputCard(false)),
    e('card', flowchartCard(false)),
    e('question.asked', decisionQuestion()),
    e('suggestions.updated', suggestionsWaiting()),
  ];
}

export function suggestionsWaiting() {
  return {
    mode: 'waiting_decision',
    primary: { id: 'sg-wait', label: '先做上面的决定', icon: 'hand', primary: true, disabled: true, reason: '有未回答的决策卡', priority: 1, action: { type: 'clear_input' } },
    secondary: [{ id: 'sg-inbox', label: '其他待处理 17', icon: 'inbox', primary: false, disabled: false, reason: '', priority: 3, action: { type: 'navigate', page: 'inbox' } }],
    all_commands: true,
  };
}

export function suggestionsAfterMerge() {
  return {
    mode: 'normal',
    // 建模的下一步（contract-changes B4d 第 14 条）：send 的 text 为空串，命令与切片在 command、args 里
    primary: { id: 'core.model:s2:rule', label: '提取规则', icon: 'scale', primary: true, disabled: false, reason: '', priority: 4, action: { type: 'send', text: '', command: '/ontology-extract-rule-metric', args: { slice: 's2' } } },
    secondary: [
      { id: 'sg-sc', label: '看场景推演', icon: 'flask', primary: false, disabled: false, reason: '', priority: 4, action: { type: 'navigate', page: 'scenarios' } },
      { id: 'sg-pause', label: '先到这里', icon: 'pause', primary: false, disabled: false, reason: '', priority: 5, action: { type: 'clear_input' } },
    ],
    all_commands: true,
  };
}

export function suggestionsInterrupted() {
  return {
    mode: 'interrupted',
    primary: { id: 'sg-cont', label: '继续刚才的任务', icon: 'play', primary: true, disabled: false, reason: '刚被打断', priority: 1, action: { type: 'send', text: '继续刚才的任务' } },
    secondary: [
      { id: 'sg-resume', label: '从打断处继续', icon: 'redo', primary: false, disabled: false, reason: '', priority: 1, action: { type: 'resume_exec', exec_id: 'ex-0412' } },
      { id: 'sg-redirect', label: '换个方向', icon: 'refresh', primary: false, disabled: false, reason: '', priority: 1, action: { type: 'clear_input' } },
    ],
    all_commands: true,
  };
}

export function commandCatalog() {
  const item = (id, label, description, command) => ({ id, label, description, command, pack: 'core', node_id: null, args_schema: null, available: true, unavailable_reason: null });
  return {
    groups: [
      { id: 'model', label: '建模', items: [
        item('guide', '引导式建模', '从头带你走一遍', '/ontology'),
        item('research', '资料分析', '读全资料，提出切片', '/ontology-research'),
        item('objects', '提取对象', '从资料里抽出业务对象', '/ontology-extract-object'),
        item('operations', '提取操作', '抽出业务流程和步骤', '/ontology-extract-operation'),
        item('rules', '规则与指标', '抽出判断规则和统计口径', '/ontology-extract-rule-metric'),
        item('check', '校验', '规则检查 + 语义检查', '/ontology-validate'),
        item('consistency', '全局一致性', '关系、口径与切片衔接', '/ontology-consistency'),
        item('incremental', '增量更新', '资料变了，只改受影响的部分', '/ontology-update'),
      ] },
      { id: 'optional', label: '可选能力', items: [
        { ...item('trace', '场景验证', '用样例推演，看结论对不对', '/ontology-trace'), pack: 'trace' },
        { ...item('mock', '生成示例数据', '按场景需要生成', '/ontology-mock'), pack: 'trace' },
        { ...item('flowchart', '画流程图', '创建或打磨业务流程图', '/ontology-flowchart'), pack: 'diagram' },
        { ...item('scan', '扫描脚本候选', '找出适合写成脚本的操作', '/ontology-codegen-scan'), pack: 'codegen' },
      ] },
    ],
    total: 22,
  };
}

// ---------- 导入 ----------
export function importInspection(kind = 'published', name = '门店补货单编制-导入') {
  const base = {
    kind: 'published_package',
    kind_label: '已发布知识包',
    root_dir: 'retail-restock/',
    files: 86,
    total_size: 4.8 * 1024 * 1024,
    counts: { objects: 14, operations: 11, rules: 9, metrics: 1, scripts: 3, solver_operations: 1 },
    package: { name: 'retail-restock', version: '0.12.0', schema_version: 'ontology-package v0.1' },
    blockers: [],
    warnings: [],
    suggested_name: name,
    name_check: { available: true, normalized: name, package_name: 'retail-restock', conflict: null, reason: null },
    after_import: { capabilities: [{ name: 'core', label: '基础建模', version: '1.4.2' }, { name: 'industry-retail', label: '零售连锁行业包', version: '1.0.3' }], start_node: 'core.research', mark_needs_review: true },
    evidence_sources: [{ id: 'erp-prod', display_name: 'ERP 生产库', needs_reauth: true }],
  };
  if (kind === 'conflict') {
    return {
      ...base,
      suggested_name: '门店补货单编制',
      name_check: { available: false, normalized: '门店补货单编制', package_name: 'retail-restock', conflict: { original_name: '门店补货单编制', attempted_name: '门店补货单编制', candidates: ['门店补货单编制 (2)'] }, reason: 'conflict' },
    };
  }
  if (kind === 'bad') {
    return {
      ...base,
      kind: 'unknown',
      kind_label: '无法识别',
      blockers: [{ code: 'symlink', message: '包含符号链接', entry: 'resource/others/规则.md → /etc/passwd' }],
    };
  }
  return base;
}

export const EXISTING_NAMES = ['门店补货单编制', '共享单车故障诊断', '图书借阅管理', '外卖骑手排班求解', '物业报修工单处理', '外卖订单（示例）'];
export const KNOWN_PACKAGE_NAMES = { 门店补货单编制: 'retail-restock', '门店补货单编制-导入': 'retail-restock' };
