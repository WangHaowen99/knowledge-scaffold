// 用户管理、审计、错误留档、回收站的合成数据（routes-admin.js 用）。
// 人名、别人的项目取 platform-data.js；审计与错误只带路径、key、ID，不带项目内容（契约 8.1 #29）。
import { MAIN_PID } from './fixtures.js';
import { PLATFORM_USERS, platformProjects, userLabel } from './platform-data.js';

const MIN = 60 * 1000;
const DAY = 24 * 3600 * 1000;
const iso = (t) => new Date(t).toISOString();

// 最近活跃（距现在的分钟数）；null 表示服务重启后还没见过（B2 第 5 条）
const LAST_SEEN_MIN = {
  zhangsan: 0, wanggong: 3, ops01: 0.3, liming: 12, chenjing: 25, zhaolei: 41, sunyu: 6, zhoumin: 9, wuhao: 18,
  modeler01: 29 * 60, zhengyi: 9 * 60, fengxue: 2 * 1440, heqing: 34 * 60, luoyang: 5 * 1440, tangli: null,
  hanbo: 3 * 1440, caoxin: 9 * 1440, xuning: 27 * 60, majun: 12 * 1440, linfang: 20 * 1440, guotao: null,
  admin02: 9 * 60 + 20, ops02: 3 * 1440,
};

export function seedUsers(now, roleOverride) {
  return PLATFORM_USERS.map((u) => {
    const m = LAST_SEEN_MIN[u.username];
    const role = u.username === 'zhangsan' && roleOverride ? roleOverride : u.role;
    return {
      username: u.username, display_name: u.display_name, role, online: u.online,
      last_seen_at: m == null ? null : iso(now - m * MIN), locked_until: null,
    };
  });
}

// ProjectMetaView（契约 7.3）：platformProjects 的 stage 换成 stage_label
export function metaView(p) {
  return {
    id: p.id, name: p.name, owner: p.owner ?? null, stage_label: p.stage_label || p.stage, build_mode: p.build_mode,
    archived: !!p.archived, created_at: p.created_at || null, last_activity_at: p.last_activity_at || null,
    disk_bytes: p.disk_bytes,
  };
}

export const platformMetaViews = (now) => platformProjects(now).map(metaView);

// 没有所有者的项目（AD-06）：旧版数据迁移时没记所有者
export function seedUnowned(now) {
  return [
    { id: 'stock-count-5b7e2c10', name: '仓库盘点差异分析', owner: null, stage_label: '资料分析', build_mode: 'L2', archived: false, created_at: iso(now - 210 * DAY), last_activity_at: iso(now - 96 * DAY), disk_bytes: 64e6 },
    { id: 'room-booking-8d3f1a27', name: '会议室预订规则', owner: null, stage_label: '建模（按段）', build_mode: 'L2', archived: false, created_at: iso(now - 260 * DAY), last_activity_at: iso(now - 140 * DAY), disk_bytes: 132e6 },
  ];
}

// ---------- 审计（AuditEntry，契约 7.16） ----------

const IP = { zhangsan: '192.0.2.21', ops01: '192.0.2.14', admin02: '192.0.2.3', wanggong: '192.0.2.33', mcp: '192.0.2.7' };
const ipOf = (actor) => IP[actor] || `198.51.100.${10 + ([...actor].reduce((h, c) => h + c.charCodeAt(0), 0) % 80)}`;

// 别人项目里的一个条目 key（只用于审计里的路径）
const ENTRY_OF = {
  'parcel-sort-6c1f2a90': '异常类型', 'clinic-queue-9e3b7d21': '分诊规则', 'hotel-room-2d8a4c55': '清洁标准',
  'gym-member-7f04b6e3': '续费提醒', 'canteen-menu-4a9c1e08': '备餐计划', 'park-lot-8b2e5f71': '车位类型',
  'school-bus-3e7d9a46': '线路站点', [MAIN_PID]: '门店特殊需求', 'bike-fleet-7b20e4a1': '故障类型',
  'rider-shifts-52c9d0f3': '排班约束', 'library-desk-1e8f6a07': '续借规则', 'estate-repair-c4d2b915': '报修类型',
};

function entry(i, at, e) {
  const actor = e.actor;
  const kind = e.actor_kind || (actor === 'agent' ? 'agent' : actor === 'system' ? 'system' : actor.startsWith('mcp:') ? 'mcp' : 'user');
  return {
    id: `aud-${(0x3f1a2c + i * 7919).toString(16).padStart(12, '0').slice(-12)}`,
    ts: iso(at), actor, actor_kind: kind, type: e.type, action: e.action, method: e.method ?? null, path: e.path ?? null,
    status: e.status ?? (e.method ? 200 : null), project_id: e.project_id ?? null, summary: e.summary, trailer: e.trailer ?? null,
    ip: e.ip === undefined ? (kind === 'user' || kind === 'mcp' ? ipOf(kind === 'mcp' ? 'mcp' : actor) : null) : e.ip,
    trace_id: e.trace_id ?? null,
  };
}

// 设计稿那 10 条（距最新一条的秒数），换成合成场景
const RECENT = [
  [0, { actor: 'ops01', type: 'ops', action: 'ops.diagnostics_create', method: 'POST', path: '/api/v1/ops/diagnostics', summary: '导出诊断包 diag-20261101-1544.zip', trace_id: '5c2e9a14' }],
  [1442, { actor: 'ops01', type: 'ops', action: 'ops.maintenance_enter', method: 'POST', path: '/api/v1/ops/maintenance', summary: '进入维护模式：拒绝新会话' }],
  [2725, { actor: 'agent', type: 'commit', action: 'agent.commit', project_id: MAIN_PID, summary: '门店补货单编制', trailer: { 'Onto-Source': 'agent', 'Onto-Changeset': 'cs-31', 'Onto-Packs': 'core@1.4.2', 'Onto-Node': 'core.operation@1.4.2' } }],
  [3173, { actor: 'admin02', type: 'plugin', action: 'pack.install', method: 'POST', path: '/api/v1/admin/packs/uploads/up-7c1e/publish', summary: '安装 core@1.5.0 到包仓库 · 通道 beta · 校验和通过' }],
  [3820, { actor: 'zhangsan', type: 'plugin', action: 'pack.enable', method: 'POST', path: `/api/v1/projects/${MAIN_PID}/packs/industry-retail/enable`, project_id: MAIN_PID, summary: '项目「门店补货单编制」启用 industry-retail@1.0.3（packs.lock 更新）', trace_id: '31d7f2c0' }],
  [5477, { actor: 'mcp:业务 Agent 联调', type: 'mcp', action: 'ontology.change.apply', project_id: MAIN_PID, summary: 'ontology.change.apply · 修改对象「门店特殊需求」' }],
  [6359, { actor: 'wanggong', type: 'write', action: 'http.put', method: 'PUT', path: '/api/v1/projects/parcel-sort-6c1f2a90/file', project_id: 'parcel-sort-6c1f2a90', summary: 'PUT /api/v1/projects/parcel-sort-6c1f2a90/file · 保存「异常类型.md」', trace_id: 'b90c6e25' }],
  [15826, { actor: 'system', type: 'plugin', action: 'pack.breaker', summary: '熔断 industry-retail@1.0.3 节点 retail.compliance（失败率 12%）' }],
  [20471, { actor: 'admin02', type: 'plugin', action: 'pack.rollback', method: 'POST', path: '/api/v1/ops/packs/trace/rollback', summary: '回滚 trace 0.9.2 → 0.9.1' }],
];

// 更早的记录：按别人的项目轮着生成
function older(i, p) {
  const owner = p.owner;
  const key = ENTRY_OF[p.id] || '对象';
  const base = `/api/v1/projects/${p.id}`;
  const k = i % 8;
  if (k === 0) return { actor: owner, type: 'write', action: 'http.put', method: 'PUT', path: `${base}/file`, project_id: p.id, summary: `PUT ${base}/file · 保存「${key}.md」` };
  if (k === 1) return { actor: 'agent', type: 'commit', action: 'agent.commit', project_id: p.id, summary: p.name, trailer: { 'Onto-Source': 'agent', 'Onto-Packs': 'core@1.4.2', 'Onto-Node': 'core.object@1.4.2' } };
  if (k === 2) return { actor: owner, type: 'plugin', action: 'pack.enable', method: 'POST', path: `${base}/packs/trace/enable`, project_id: p.id, summary: `项目「${p.name}」启用 trace@0.9.1（packs.lock 更新）` };
  if (k === 3) return { actor: owner, type: 'publish', action: 'publish.release', method: 'POST', path: `${base}/releases`, project_id: p.id, summary: `发布 ${p.name} 0.${3 + (i % 5)}.0` };
  if (k === 4) return { actor: 'ops01', type: 'ops', action: 'ops.session_recycle', method: 'POST', path: `/api/v1/ops/sessions/${p.id}/recycle`, project_id: p.id, summary: `回收会话 ${p.id}（空闲超时）` };
  if (k === 5) return { actor: 'mcp:业务 Agent 联调', type: 'mcp', action: 'ontology.change.validate', project_id: p.id, summary: `ontology.change.validate · 校验对象「${key}」` };
  if (k === 6) return { actor: owner, type: 'write', action: 'http.post', method: 'POST', path: `${base}/resources`, project_id: p.id, summary: `POST ${base}/resources · 上传「0${1 + (i % 7)}-资料.docx」` };
  return { actor: 'admin02', type: 'user', action: 'user.password_reset', method: 'POST', path: `/api/v1/admin/users/${owner}/reset-password`, summary: `重置 ${owner} 的密码` };
}

export function seedAudit(now) {
  const top = now - 2 * MIN;
  const list = RECENT.map(([s, e], i) => entry(i, top - s * 1000, e));
  const others = platformProjects(now).filter((p) => !p.archived);
  let t = top - 20471 * 1000;
  for (let i = 0; i < 52; i += 1) {
    t -= (31 + ((i * 17) % 53)) * MIN;
    list.push(entry(RECENT.length + i, t, older(i, others[i % others.length])));
  }
  return list;
}

// ---------- 错误留档（ErrorRecord + O-51 的 stack、context、auto_action） ----------

const ERR_TEMPLATES = [
  {
    origin: 'session', pack: 'industry-retail@1.0.3', node: 'retail.compliance',
    raw: 'RuleSchemaError: rules.yaml 第 18 行 when 字段缺失', friendly: '合规检查暂时不可用，已通知管理员',
    stack: (p) => `RuleSchemaError: rules.yaml:18 missing required field "when"\n  at rule_loader.load (industry-retail/1.0.3/rules.yaml)\n  at ontology_checker.load_pack_rules (core/1.4.2)\n  at gate_chain.run (kernel/write_path)\ncontext: project=${p} changeset=cs-88 pack=industry-retail@1.0.3`,
    auto: '包已熔断，节点暂时跳过，发布关卡解除', exec: 'ex-0533', session: 'd2f8', changeset: 'cs-88',
  },
  {
    origin: 'publish', pack: 'core@1.4.2', node: 'core.publish', older: true,
    raw: (p) => (p === MAIN_PID ? 'package_mapping: 对象「补货台账」声明的表 erp.replenish_ledger 不存在' : `package_mapping: 对象「${ENTRY_OF[p] || '对象'}」声明的表 ${tableOf(p)} 不存在`),
    friendly: '发布检查发现 1 个对象的数据来源对不上',
    stack: (p) => `PackageMappingError: object maps to missing table ${p === MAIN_PID ? 'erp.replenish_ledger' : tableOf(p)}\n  at package_mapping.resolve (core/1.4.2/publish/package_mapping.py)\n  at publish.render (kernel/publish)\ncontext: project=${p}`,
    auto: null, exec: null, session: null,
  },
  {
    origin: 'assembly', pack: 'industry-property@1.2.0', node: null,
    raw: 'assembly: 命令名冲突 ontology-check（core 与 industry-property）', friendly: '能力包启用失败，已保持原来的版本',
    stack: (p) => `AssemblyError: command name conflict "ontology-check" (core@1.4.2, industry-property@1.2.0)\n  at assembler.merge_commands (kernel/packs/assembler.py)\n  at assembler.build (kernel/packs/assembler.py)\ncontext: project=${p} lock=packs.lock`,
    auto: '保持原来的装配，锁文件没有改动', exec: null, session: null,
  },
  {
    origin: 'session', pack: 'codegen@0.9.2', node: 'codegen.codegen', older: true,
    raw: 'model gateway timeout after 120s', friendly: '模型响应超时，请稍后重试',
    stack: (p) => `TimeoutError: model gateway timeout after 120s\n  at engine.stream (claude)\n  at session.turn (kernel/session_host)\ncontext: project=${p} session=7a0c`,
    auto: null, exec: 'ex-0498', session: '7a0c',
  },
  {
    origin: 'gate', pack: 'core@1.4.2', node: 'core.model', older: true,
    raw: 'guard: 写入受保护路径 resource/01-场景说明.docx 被拒绝', friendly: '助手试图改动资料原文，已拦下',
    stack: (p) => `GuardDenied: write to protected path resource/01-场景说明.docx\n  at guard.check_write (kernel/guards)\n  at tool_proxy.bash (kernel/session_host)\ncontext: project=${p}`,
    auto: '已拒绝这次写入，对话继续', exec: 'ex-0477', session: 'c41e',
  },
  {
    origin: 'kernel', pack: null, node: null, older: true,
    raw: 'ChangeTx: .git/index.lock 被占用超过 2 秒，没有提交', friendly: '保存没有成功，请稍后重试',
    stack: (p) => `ChangeTxError: git index locked for more than 2s\n  at changetx.commit (kernel/write_path)\n  at files.write (kernel/workspace)\ncontext: project=${p}`,
    auto: null, exec: null, session: null,
  },
  {
    origin: 'session', pack: 'codegen@0.9.2', node: 'codegen.solver', older: true,
    raw: 'sandbox-gateway: check_rules.py 运行超过 60s 被终止', friendly: '脚本运行超时，已停止',
    stack: (p) => `SandboxTimeout: check_rules.py exceeded 60s\n  at sandbox_gateway.run (sandbox)\n  at script_runner.exec (core/1.4.2)\ncontext: project=${p}`,
    auto: null, exec: 'ex-0460', session: '19bd',
  },
];

// 更早的错误只用不绑定具体行业包的几类（行业包规则、装配冲突只出现在设计稿那几条）
const OLDER_TEMPLATES = ERR_TEMPLATES.filter((t) => t.older);
// 合成的表名（只是 key）：parcel-sort-6c1f2a90 → ext.parcel_sort_ledger
function tableOf(pid) {
  return `ext.${pid.split('-').slice(0, -1).join('_')}_ledger`;
}

// 设计稿前 5 条落在这几个项目上
const TOP_PROJECTS = ['bike-fleet-7b20e4a1', MAIN_PID, 'library-desk-1e8f6a07', 'estate-repair-c4d2b915', 'rider-shifts-52c9d0f3'];
const TOP_GAPS = [0, 4717, 9355, 14049, 22167];

export function seedErrors(now, total = 100) {
  const metas = Object.fromEntries(platformMetaViews(now).map((p) => [p.id, p]));
  const others = Object.keys(metas);
  const top = now - 3 * MIN;
  const out = [];
  let t = top - TOP_GAPS[TOP_GAPS.length - 1] * 1000;
  for (let i = 0; i < total; i += 1) {
    const tpl = i < 5 ? ERR_TEMPLATES[i] : OLDER_TEMPLATES[i % OLDER_TEMPLATES.length];
    let at;
    if (i < 5) at = top - TOP_GAPS[i] * 1000;
    else { t -= (43 + ((i * 29) % 97)) * MIN; at = t; }
    const pid = i < 5 ? TOP_PROJECTS[i] : others[(i * 3) % others.length];
    const id = `err-${(0x7f3a + i * 0x1d3).toString(16).slice(-4)}`;
    const trace = i % 3 === 2 ? null : (0xa71e03c9 - i * 0x10f1).toString(16).slice(-8);
    out.push({
      id, at: new Date(at).toISOString(), project: metas[pid] || null, project_id: pid, origin: tpl.origin,
      source: tpl.origin, pack: tpl.pack, node: tpl.node, raw_message: typeof tpl.raw === 'function' ? tpl.raw(pid) : tpl.raw, friendly_message: tpl.friendly, trace_id: trace,
      // O-51 才给的字段
      _detail: {
        stack: tpl.stack(pid), auto_action: tpl.auto,
        context: { project_id: pid, changeset_id: tpl.changeset || null, pack: tpl.pack, node: tpl.node, exec_id: tpl.exec, session_id: tpl.session },
      },
    });
  }
  return out;
}

// ---------- 回收站 ----------

export function seedTrash(now, { legacy = false } = {}) {
  const rows = [
    ['trash-parcel-sort-old', 'parcel-sort-0d4b7a12', '快递分拣异常处理（旧）', 'wanggong', '资料分析', 4, 420],
    ['trash-retail-restock-r2', 'retail-restock-r2-6e19c3b8', '门店补货单编制 r2', 'zhangsan', '建模（按段）', 8, 612],
    ['trash-canteen-pilot-a', 'canteen-pilot-2f7e5a90', '食堂菜单试点 A', 'sunyu', '校验', 17, 188],
    ['trash-test-0919', 'test-0919-9a1c4e63', '测试项目 0919', 'liming', '资料分析', 25, 402],
  ];
  return rows.map(([entryId, id, name, owner, stage, daysAgo, mb]) => {
    const deleted = now - daysAgo * DAY - 3 * 3600 * 1000;
    return {
      entry: entryId,
      project: { id, name, owner, stage_label: stage, build_mode: 'L2', archived: false, created_at: iso(deleted - 40 * DAY), last_activity_at: iso(deleted - DAY) },
      deleted_at: iso(deleted), size: mb * 1024 * 1024, purge_at: legacy ? null : iso(deleted + 30 * DAY),
    };
  });
}

export { userLabel };
