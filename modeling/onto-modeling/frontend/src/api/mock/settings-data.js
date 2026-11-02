// mock 数据 · 项目设置（合成场景）：本项目的能力（PL-05）、助手可用的 MCP 服务器（P-14）、对外 MCP（M-01、M-02）
// 版本与状态取自设计稿 proj-settings：场景走查有一个待生效的升级，代码生成可回滚，图示可升级，迁移复用停用过。
import { buildClientConfigs } from '../../lib/mcpClientConfig.js';

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const iso = (ms) => new Date(ms).toISOString();
const dayAt = (daysAgo, h, m) => {
  const d = new Date(Date.now() - daysAgo * DAY);
  d.setHours(h, m, 0, 0);
  return d.toISOString();
};

const UI = {
  kernel: { kind: 'kernel', note: '内核：工作台 · Studio · 资料 · 发布' },
  page: (id, title) => ({ kind: 'page', pages: [{ id, title }], note: `左栏页面：${title}` }),
  reverse: {
    kind: 'subpages',
    subpages: [
      { parent: 'resources', id: 'sources', title: '业务系统数据源' },
      { parent: 'resources', id: 'snapshots', title: '采集与快照' },
      { parent: 'resources', id: 'schema', title: '证据浏览器' },
    ],
    note: '资料页多出数据源、快照、证据 3 个子页',
  },
  none: { kind: 'none', note: '无独立页面' },
};

const pk = (o) => ({
  category: 'capability', ui: UI.none, channel: 'stable', status: 'enabled', pending: null, enabled_at: dayAt(31, 10, 0),
  latest_available: o.locked_version, rollback_to: null, actions: [], ...o,
});

// 第一份是主示例项目的；其他项目沿用同一份
export function projectPacks() {
  return [
    pk({ name: 'core', label: '基础建模', ui: UI.kernel, locked_version: '1.4.2', status: 'required' }),
    pk({
      name: 'trace', label: '场景走查', ui: UI.page('scenarios', '场景走查'), locked_version: '0.9.1', latest_available: '0.10.0', status: 'pending',
      pending: { from: '0.9.1', to: '0.10.0', kind: 'upgrade', effective: 'next_message' },
    }),
    pk({ name: 'knowledge', label: '知识沉淀', ui: UI.page('knowledge', '知识沉淀'), locked_version: '0.6.0' }),
    pk({ name: 'solver', label: '代码生成与求解', ui: UI.page('scripts', '代码生成'), locked_version: '0.7.2', rollback_to: '0.7.1' }),
    pk({ name: 'reverse', label: '系统逆向建模', ui: UI.reverse, locked_version: '0.3.0' }),
    pk({ name: 'diagram', label: '图示', locked_version: '0.5.0', latest_available: '0.6.0' }),
    pk({ name: 'evolution', label: '反馈演进', locked_version: '0.3.0' }),
    pk({ name: 'industry-retail', label: '零售连锁', category: 'industry', ui: null, locked_version: '1.0.3' }),
    pk({ name: 'sys-erp-generic', label: 'ERP 通用表结构', category: 'system', ui: null, locked_version: '0.3.0' }),
    pk({ name: 'migrate', label: '迁移复用', locked_version: '0.2.0', status: 'disabled', enabled_at: null }),
  ];
}

// 只动了行业包、系统知识包这类纯数据包时立即生效（contract-changes B3a 第 7 条）
export const DATA_CATEGORIES = ['industry', 'system'];

// 「上次锁定版本」之外，PL-06 新启用时用目录里的最新版；离线部署不可用的包拒绝启用
export const OFFLINE_PACKS = ['web-research'];

export function mcpServers() {
  return [
    { name: 'evidence', transport: 'stdio', target: 'onto evidence-mcp', origin: 'builtin', read_only: true, status: 'running', tools: ['evidence.search', 'evidence.show', 'evidence.where-used', 'evidence.lineage', 'evidence.diff'], enabled: true, enabled_globally: true },
    { name: 'flow', transport: 'stdio', target: 'onto flow-mcp', origin: 'builtin', read_only: true, status: 'running', tools: ['flow.get'], enabled: true, enabled_globally: true },
    { name: 'replenish-std-lookup', transport: 'http', target: 'https://mcp.example.com/std', origin: 'global', read_only: false, status: 'running', tools: ['std_clause_search', 'std_clause_get', 'std_version_diff'], enabled: true, enabled_globally: true },
    { name: 'mermaid-render', transport: 'stdio', target: 'npx mermaid-mcp', origin: 'global', read_only: false, status: 'stopped', tools: [], enabled: false, enabled_globally: true },
  ];
}

const BASE_URL = 'http://127.0.0.1:8100';
const READ_TOOLS = ['object_list', 'object_get', 'operation_list', 'operation_get', 'rule_list', 'rule_get'];
const CHANGE_TOOLS = ['object_change_validate', 'object_change_apply', 'object_delete', 'operation_change_validate', 'operation_change_apply', 'operation_delete', 'rule_change_validate', 'rule_change_apply', 'rule_delete'];
export const SCOPES = ['ontology.read', 'ontology.change.validate', 'ontology.change.apply'];

export function mcpEndpoints(pid) {
  const at = (p) => `${BASE_URL}/api/v1/onto-mcp/projects/${pid}${p}`;
  return [
    { toolset: 'read', transport: 'sse', url: at('/sse') },
    { toolset: 'read', transport: 'streamable-http', url: at('/mcp') },
    { toolset: 'change', transport: 'sse', url: at('/change/sse') },
    { toolset: 'change', transport: 'streamable-http', url: at('/change/mcp') },
  ];
}

// 按本次 token 的权限范围生成三份客户端配置（M-01 的模板用占位符，M-03 填入明文）
export function clientConfigs(pid, token, scopes) {
  const runtime = { urls: mcpEndpoints(pid) };
  const read = buildClientConfigs(runtime, token || '<READ_TOKEN>', scopes || ['ontology.read']).read;
  const change = buildClientConfigs(runtime, token || '<CHANGE_TOKEN>', scopes || ['ontology.change.validate', 'ontology.change.apply']).change;
  const full = buildClientConfigs(runtime, token || '<FULL_TOKEN>', scopes || SCOPES).combined;
  return { read, change, full };
}

export function mcpOverview(pid, { archived = false } = {}) {
  return {
    status: 'running',
    base_url: BASE_URL,
    local_only: true,
    endpoints: mcpEndpoints(pid),
    toolsets: [
      { name: 'read', scope_requirement: 'ontology.read', accepted_scopes: ['ontology.read'], tools: READ_TOOLS },
      { name: 'change', scope_requirement: 'ontology.change.validate', accepted_scopes: ['ontology.change.validate', 'ontology.change.apply'], tools: archived ? [] : CHANGE_TOOLS },
    ],
    pack_tools: [
      { name: 'flow.get', scope: 'ontology.read', read_only: true, pack: 'core' },
      ...['search', 'show', 'where-used', 'lineage', 'diff', 'er'].map((t) => ({ name: `evidence.${t}`, scope: 'ontology.read', read_only: true, pack: 'reverse' })),
    ],
    scope_catalog: [
      { scope: 'ontology.read', description: '列出、读取对象 / 操作 / 规则' },
      { scope: 'ontology.change.validate', description: '提交改动前先校验' },
      { scope: 'ontology.change.apply', description: '真正写入，走写入路径并保存版本' },
    ],
    token_policy: { min_ttl_s: 300, default_ttl_s: 86400, max_ttl_s: 2592000, supports_non_expiring: true },
    protocol_versions: ['2025-06-18', '2025-03-26'],
    client_config_templates: clientConfigs(pid),
    recommended_order: ['server/discover', 'tools/list', 'tools/call'],
    limits: { page_size_max: 200, body_bytes: 1048576 },
  };
}

// 剩余有效期的说法：「30 天 · 剩 21 天」「1 天 · 剩 6 小时」「永不过期」
export function remainingText(createdAt, expiresAt, now = Date.now()) {
  if (!expiresAt) return '永不过期';
  const total = new Date(expiresAt).getTime() - new Date(createdAt).getTime();
  const left = new Date(expiresAt).getTime() - now;
  const span = (ms) => (ms >= DAY ? `${Math.round(ms / DAY)} 天` : ms >= HOUR ? `${Math.round(ms / HOUR)} 小时` : `${Math.max(1, Math.round(ms / MIN))} 分钟`);
  if (left <= 0) return `${span(total)} · 已过期`;
  return `${span(total)} · 剩 ${span(left)}`;
}

export function mcpTokens() {
  const now = Date.now();
  const tok = (id, name, scopes, createdAt, ttl, lastUsed) => {
    const expires = ttl ? iso(new Date(createdAt).getTime() + ttl) : null;
    return { id, name, scopes, subject: 'zhangsan', issued_by: '张工', created_at: createdAt, expires_at: expires, last_used_at: lastUsed, remaining_text: remainingText(createdAt, expires, now) };
  };
  return [
    tok('tok-31c0', '临时验证', ['ontology.read', 'ontology.change.validate'], iso(now - 18 * HOUR), DAY, null),
    tok('tok-2a7e', '业务 Agent 读写联调', SCOPES, iso(now - 9 * DAY), 30 * DAY, iso(now - 2 * HOUR)),
    tok('tok-19b4', '只读巡检脚本', ['ontology.read'], iso(now - 15 * DAY), null, iso(now - 6 * HOUR)),
  ];
}

// ---------- 自主构建策略（AB-01～AB-04，路由在 routes-autobuild.js） ----------
export const PLATFORM_LIMITS = { max_duration_min: 240, max_tokens: 10000000, max_cost: { amount: 300, currency: 'CNY' }, allow_production_collection: true };
export const QUOTA_REMAINING = { tokens: 42000000, cost: { amount: 1240, currency: 'CNY' } };
const READONLY_POLICY_KEYS = ['boundary_text', 'platform_limits', 'quota_remaining', 'last_run', 'revision', 'base_revision'];

// AB-02 的请求体并进当前策略（只读字段不收）
export function mergePolicy(current, body) {
  const next = { ...current };
  Object.keys(body || {}).forEach((k) => { if (!READONLY_POLICY_KEYS.includes(k)) next[k] = body[k]; });
  return next;
}

// 返回 null 表示合法；否则 [status, code, message, details]
export function checkPolicy(p, sourceIds) {
  const b = p.budget || {};
  const L = PLATFORM_LIMITS;
  if (b.max_duration_min > L.max_duration_min) return [422, 'budget_exceeds_quota', '时长超出平台上限', { dimension: 'duration', max: L.max_duration_min }];
  if (b.max_tokens > Math.min(L.max_tokens, QUOTA_REMAINING.tokens)) return [422, 'budget_exceeds_quota', 'token 超出平台上限', { dimension: 'tokens', max: L.max_tokens }];
  if (b.max_cost?.amount > Math.min(L.max_cost.amount, QUOTA_REMAINING.cost.amount)) return [422, 'budget_exceeds_quota', '费用超出平台上限', { dimension: 'cost', max: L.max_cost.amount }];
  if (!(b.max_duration_min > 0 && b.max_tokens > 0 && b.max_cost?.amount > 0)) return [400, 'invalid_argument', '预算要大于 0', { fields: { budget: 'required' } }];
  const t = p.advanced?.confidence_threshold;
  if (t != null && !(t >= 0.5 && t <= 1)) return [400, 'invalid_argument', '阈值在 0.5～1.0 之间', { fields: { 'advanced.confidence_threshold': 'out_of_range' } }];
  const bad = (p.production_collection || []).find((x) => !sourceIds.includes(x.source_id));
  if (bad) return [422, 'source_not_authorized', '本项目没有获授权使用这个数据源', { source_id: bad.source_id }];
  if (p.scope?.mode === 'selected' && !(p.scope.slice_ids || []).length) return [400, 'invalid_argument', '至少选一片', { fields: { 'scope.slice_ids': 'required' } }];
  return null;
}

const BELOW = { conservative_assumption: '采用保守选项并记为假设', suspend: '挂起这一项，跳到下一片' };

// AB-03：按策略算这次的范围（flow 里没完成的切片）、生产库、警告
export function previewOf(policy, flowSlices, sourceName = (id) => id) {
  const scope = policy.scope || {};
  const slices = (flowSlices || []).map((s) => {
    const open = s.group !== 'done';
    return { id: s.id, name: s.name, current: open ? '未完成' : '已确认', included: open && (scope.mode !== 'selected' || (scope.slice_ids || []).includes(s.id)), group: s.group };
  });
  const allowed = (policy.production_collection || []).filter((x) => x.allowed);
  const production = allowed.length
    ? `允许采集 ${allowed.map((x) => `${sourceName(x.source_id)}${x.window ? `（${x.window.start} – ${x.window.end}）` : ''}`).join('、')}`
    : '不采集';
  const warnings = slices.some((s) => s.included && s.group === 'now') ? ['有 1 个未决决策会被按策略决定'] : [];
  return {
    slices: slices.map(({ group, ...s }) => s),
    budget: policy.budget, production, below_threshold_text: BELOW[policy.advanced?.below_threshold] || BELOW.conservative_assumption,
    warnings, queued: false, queue_position: null,
  };
}

// AB-04 起的新一次运行：mock 不模拟推进，停在「排队中」
export function queuedRun(n, policy, total) {
  return {
    run_id: `run-${n}`, run_no: n, state: 'queued', current: null, elapsed_ms: 0, est_total_ms: null,
    tokens: { used: 0, limit: policy.budget.max_tokens }, cost: { used: { amount: 0, currency: policy.budget.max_cost.currency }, budget: policy.budget.max_cost },
    slices: { done: 0, total }, decisions: 0, suspended: 0, needs_review: 0, started_at: new Date().toISOString(), ended_at: null,
  };
}
