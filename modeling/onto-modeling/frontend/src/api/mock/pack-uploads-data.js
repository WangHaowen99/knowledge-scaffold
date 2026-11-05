// 上传与审核（PL-13～PL-18）与管理员审核申请（AD-08～AD-10）的合成数据。
// 检查项 id、标签按后端 B3a 的静态检查（contract-changes B3a 第 11 条），说明文字是合成的。

// 后端的 14 项（租户上传 core 时另有 tenant_core）
export const CHECK_LABELS = {
  manifest: '清单格式与必填字段',
  checksums: '校验和一致',
  symlinks: '不含符号链接',
  declared: '声明的命令、子代理、脚本都存在',
  compat: '兼容范围对得上',
  names: '名字不冲突',
  entity_types: '没有新增实体类型',
  write_scope: '写入范围不碰受保护目录',
  node_entry: '节点入口命令存在',
  hooks: 'hooks 只追加拒绝型',
  frontend: '页面只选用平台内置、没有前端代码',
  secrets: '不含凭据',
  syntax: '脚本语法',
  prompt_budget: '提示词体积预算',
  tenant_core: '租户包不能替换基础建模能力',
};
const ORDER = Object.keys(CHECK_LABELS).filter((k) => k !== 'tenant_core');
const check = (id, result, detail = '') => ({ id, label: CHECK_LABELS[id], result, detail });
export const summaryOf = (checks) => Object.fromEntries(['pass', 'warn', 'fail'].map((r) => [r, checks.filter((c) => c.result === r).length]));

/** 检查通过的包（行业包 industry-retail 1.1.0 那种：13 通过 · 1 警告） */
export function passedChecks(name, version, { files = 214 } = {}) {
  const detail = {
    manifest: `${name} ${version} · name、version、kind、kernel_api、engines、depends、contributes 齐全`,
    checksums: `sha256 与 checksums.sha256 全部对上（${files} 个文件）`,
    declared: `nodes/compliance-check.yaml → /${name.replace(/^industry-/, '')}-compliance-check`,
    compat: 'kernel_api >=1.0 <2.0 · depends core >=1.4',
    names: '和平台上 27 个包无重名',
    entity_types: '只加扩展字段「适用门店类型」',
    write_scope: 'produces: reports/compliance-*.md',
    hooks: '未追加 hooks',
    frontend: '没有声明页面；租户上传的包只能选用平台内置的页面',
    secrets: '未发现疑似密钥、账号、连接串',
    prompt_budget: '22.8 KB / 24 KB，建议精简 reference/',
  };
  return ORDER.map((id) => check(id, id === 'prompt_budget' ? 'warn' : 'pass', detail[id] || ''));
}

/** 租户上传、有失败项的包（demo-trace-ext 0.1.0 那种：9 通过 · 1 警告 · 4 失败） */
export function failedChecks(name) {
  const res = {
    declared: ['warn', `未声明：commands/${name}-replay.md（文件在 commands/ 下，清单里没写）`],
    entity_types: ['fail', 'artifacts/event.yaml：新增了实体类型 event —— 包只能加扩展字段和声明式规则'],
    write_scope: ['fail', `节点 ${name}.autofix 的 produces: resource/** —— 资料区只读，包不能写`],
    hooks: ['fail', 'hooks 里有放行型 PostToolUse，只能追加拒绝型守卫'],
    frontend: ['fail', '带了前端代码 client/ —— 租户上传的包只能选用平台内置的页面，不能带新页面'],
  };
  return ORDER.map((id) => check(id, res[id]?.[0] || 'pass', res[id]?.[1] || ''));
}

/** 清单摘要（契约 7.11 ManifestSummary） */
export function manifestOf(name, version, kind, extra = {}) {
  const zero = { commands: 0, agents: 0, skills: 0, scripts: 0, templates: 0, hooks: 0, nodes: [], entries: 0, pages: [], artifacts: [], views: [], reports: [], gates: 0, config: 0, metrics: 0 };
  return {
    name, version, kind, kernel_api: '>=1.0 <2.0', engines: ['claude', 'jiuwen'], depends: { core: '>=1.4' },
    contributes: { ...zero, ...(extra.contributes || {}) }, checksums_ok: true, size: extra.size || 0, files: extra.files || 0,
  };
}

/** 文件名 → 包名、版本（mock 没有真的解包，按 <名>-<x.y.z>.zip 猜） */
export function parseFilename(filename) {
  const m = /^(.+?)-(\d+\.\d+\.\d+)\.zip$/i.exec(filename || '');
  return m ? { name: m[1].toLowerCase(), version: m[2] } : { name: (filename || 'pack').replace(/\.zip$/i, '').toLowerCase(), version: '0.1.0' };
}

const MIN = 60 * 1000;
const iso = (t) => new Date(t).toISOString();

/** 一条上传记录（完整形状，PL-14 只取 PackUpload 那几个字段） */
export function uploadRecord({ id, filename, size, at, by, tenant, state, checks, manifest, checkSecs = 4 }) {
  const { name, version } = parseFilename(filename);
  const done = state !== 'checking';
  const summary = done ? summaryOf(checks) : { pass: 0, warn: 0, fail: 0 };
  const timeline = [{ at: iso(at), actor: by, text: '上传', state: 'checking' }];
  if (done) timeline.push({ at: iso(at + checkSecs * 1000), actor: 'system', text: summary.fail ? '静态检查未通过' : '静态检查通过', state: summary.fail ? 'failed' : 'passed' });
  return {
    id, filename, size, uploaded_at: iso(at), uploaded_by: by, tenant_label: tenant || null, state,
    summary, manifest_ok: done, checksums_ok: done && checks.find((c) => c.id === 'checksums')?.result === 'pass',
    name: done ? name : null, version: done ? version : null,
    manifest: done ? manifest : null, checks: done ? checks : [], timeline,
  };
}

/**
 * 种子：默认两个（检查通过的官方行业包、有失败项的租户包）；
 * checking：再加一个正在检查的；empty：一个都没有。
 */
export function seedUploads(now = Date.now(), { empty = false, checking = false } = {}) {
  if (empty) return [];
  const list = [
    uploadRecord({
      id: 'up-7c2e91a4d0', filename: 'industry-retail-1.1.0.zip', size: 412 * 1024, at: now - 18 * MIN, by: 'zhangsan', state: 'passed',
      checks: passedChecks('industry-retail', '1.1.0'),
      manifest: manifestOf('industry-retail', '1.1.0', 'knowledge', { size: 1_268_000, files: 214, contributes: { commands: 1, templates: 3, nodes: ['retail.compliance'], gates: 1, config: 2 } }),
    }),
    uploadRecord({
      id: 'up-3b8f05e6a1', filename: 'demo-trace-ext-0.1.0.zip', size: 88 * 1024, at: now - 34 * MIN, by: 'zhangsan', tenant: '示例租户', state: 'failed', checkSecs: 3,
      checks: failedChecks('demo-trace-ext'),
      manifest: manifestOf('demo-trace-ext', '0.1.0', 'node', { size: 241_000, files: 37, contributes: { commands: 2, scripts: 3, hooks: 1, nodes: ['demo-trace-ext.autofix'], artifacts: ['artifacts/event.yaml'] } }),
    }),
  ];
  if (checking) {
    list.unshift(uploadRecord({ id: 'up-e41a6c09b7', filename: 'sys-wms-generic-0.5.0.zip', size: 236 * 1024, at: now - 40 * 1000, by: 'zhangsan', state: 'checking', checks: [] }));
  }
  return list;
}

/** 检查报告（PL-16，text/markdown），和后端 report_markdown 一个形状 */
export function reportMarkdown(rec) {
  const marks = { pass: '通过', warn: '警告', fail: '失败' };
  const title = rec.name ? `${rec.name} ${rec.version}` : rec.filename;
  const rows = (rec.checks || []).map((c) => `| ${c.label} | ${marks[c.result] || c.result} | ${String(c.detail || '').replace(/\|/g, '\\|')} |`);
  return [`# 静态检查报告：${title}`, '', '| 检查项 | 结果 | 说明 |', '|---|---|---|', ...rows].join('\n') + '\n';
}

// ---------- 管理员审核申请（AD-08） ----------

const HOUR = 60 * MIN;
/** 三类申请各一条待处理，另有一条昨天通过的 */
export function seedRequests(now = Date.now()) {
  return [
    {
      id: 'req-5d21c8', kind: 'pack_install', state: 'pending', requested_by: 'wanggong', requested_at: iso(now - 25 * MIN),
      project_id: 'parcel-sort-6c1f2a90', summary: 'HTTP 接口连接器 0.3.0',
      payload: { pack: { name: 'provider-http-api', label: 'HTTP 接口连接器' }, version: '0.3.0', project_name: '快递分拣异常处理', note: '要从分拣系统的接口拉异常件记录，平台上还没有装这个连接器' },
    },
    {
      id: 'req-9a47e3', kind: 'source_registration', state: 'pending', requested_by: 'liming', requested_at: iso(now - 2 * HOUR),
      project_id: 'clinic-queue-9e3b7d21', summary: '分诊系统测试库（PostgreSQL · 测试环境）',
      payload: {
        id: 'triage-test', display_name: '分诊系统测试库', provider: 'db-postgres', environment: 'test',
        connection: { host: '198.51.100.24', port: 5432, database: 'triage' }, project_name: '社区诊所排队分诊',
        note: '想对一下挂号表和分诊表的字段口径，只读测试库',
      },
    },
    {
      id: 'req-c06b12', kind: 'knowledge_nomination', state: 'pending', requested_by: 'chenjing', requested_at: iso(now - 5 * HOUR),
      project_id: 'hotel-room-2d8a4c55', summary: '退房后先登记再派单 → 物业服务',
      payload: {
        industry_pack: 'industry-property', industry_pack_label: '物业服务', knowledge_id: 'domain-rule-checkout-first', title: '退房后先登记再派单',
        sanitized_text: '房间退房后先登记清洁需求和紧急程度，再派给保洁；紧急程度缺失时按「一般」处理并提示补填。', project_name: '酒店客房清洁排程',
        note: '各家酒店都这么排，适合放进行业包',
      },
    },
    {
      id: 'req-71f0d4', kind: 'source_registration', state: 'approved', requested_by: 'zhaolei', requested_at: iso(now - 26 * HOUR),
      project_id: 'gym-member-7f04b6e3', summary: '会员系统只读库（MySQL · 测试环境）', reviewed_by: 'zhangsan', reviewed_at: iso(now - 22 * HOUR), note: '已写入凭据并测试通过',
      payload: { id: 'member-ro', display_name: '会员系统只读库', provider: 'db-mysql', environment: 'test', project_name: '健身房会员续费提醒', note: '续费提醒要读会员到期日' },
    },
  ];
}
