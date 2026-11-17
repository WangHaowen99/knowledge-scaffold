// mock 数据 · 项目设置（合成场景）：本项目的能力（PL-05）、助手可用的 MCP 服务器（P-14）、对外 MCP（M-01、M-02）
// 版本与状态取自设计稿 proj-settings：场景模拟有一个待生效的升级，代码生成可回滚，图示可升级。
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
      name: 'trace', label: '场景模拟', ui: UI.page('scenarios', '场景模拟'), locked_version: '0.9.1', latest_available: '0.10.0', status: 'pending',
      pending: { from: '0.9.1', to: '0.10.0', kind: 'upgrade', effective: 'next_message' },
    }),
    pk({ name: 'codegen', label: '代码生成与求解', ui: UI.page('scripts', '代码生成'), locked_version: '0.9.2', rollback_to: '0.9.1' }),
    pk({ name: 'diagram', label: '图示', locked_version: '0.5.0', latest_available: '0.6.0' }),
    pk({ name: 'industry-retail', label: '零售连锁', category: 'industry', ui: null, locked_version: '1.0.3' }),
    pk({ name: 'sys-erp-generic', label: 'ERP 通用表结构', category: 'system', ui: null, locked_version: '0.3.0' }),
  ];
}

// 只动了行业包、系统知识包这类纯数据包时立即生效（contract-changes B3a 第 7 条）
export const DATA_CATEGORIES = ['industry', 'system'];

export function mcpServers() {
  return [
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
      { name: 'read', scope_requirement: 'ontology.read', accepted_scopes: ['ontology.read', 'project.version.revert'], tools: READ_TOOLS },
      { name: 'change', scope_requirement: 'ontology.change.validate', accepted_scopes: ['ontology.change.validate', 'ontology.change.apply', 'project.version.revert'], tools: archived ? [] : CHANGE_TOOLS },
    ],
    pack_tools: [
      { name: 'flow.get', scope: 'ontology.read', read_only: true, pack: 'core' },
    ],
    scope_catalog: [
      { scope: 'ontology.read', description: '列出、读取对象 / 操作 / 规则' },
      { scope: 'ontology.change.validate', description: '提交改动前先校验' },
      { scope: 'ontology.change.apply', description: '真正写入，走写入路径并保存版本' },
      { scope: 'project.version.revert', description: '读取项目版本与执行版本回退' },
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
// 同后端：请求体只收 scope、advanced 两组，组内给了哪项改哪项；别的（F6 取消的 budget、已删的 production_collection、只读字段）带了也不收
const POLICY_GROUPS = ['scope', 'advanced'];

// AB-02 的请求体、AB-03 的 policy_overrides 并进当前策略
export function mergePolicy(current, body) {
  const next = { ...current };
  POLICY_GROUPS.forEach((g) => {
    const part = body?.[g];
    if (part && typeof part === 'object' && !Array.isArray(part)) next[g] = { ...(current[g] || {}), ...part };
  });
  return next;
}

// 返回 null 表示合法；否则 [status, code, message, details]
export function checkPolicy(p) {
  const t = p.advanced?.confidence_threshold;
  if (t != null && !(t >= 0.5 && t <= 1)) return [400, 'invalid_argument', '阈值在 0.5～1.0 之间', { fields: { 'advanced.confidence_threshold': 'out_of_range' } }];
  return null;
}

const BELOW = { conservative_assumption: '先按最稳妥的做法往下做，做完列出来给你复核', suspend: '先跳过这一项，留给你来定' };

// 不再分段：按段时代的 slice_ids 原样存着、不再起作用，「只跑选中的段」按全部没做完的（同后端 autobuild/policy normalize）
export function normalizeScope(scope) {
  const s = { ...(scope || {}) };
  return s.mode === 'selected' ? { ...s, mode: 'all_pending' } : s;
}

const SKIP = new Set(['core.publish', 'core.update']);
const CURRENT = { confirmed: '已确认', stale: '有过期', skipped: '已跳过', running: '进行中', pending_confirm: '待确认' };

// AB-03：一个节点一行（资料分析之后的主线节点，到 until_node 为止；同后端 autobuild/plan），没做完的在范围里
export function previewOf(policy, flow) {
  const scope = policy.scope || {};
  const nodes = flow?.nodes || [];
  const start = nodes.findIndex((n) => n.id === 'core.research') + 1;
  const rows = [];
  for (const n of nodes.slice(start)) {
    const eligible = !SKIP.has(n.id) && (n.placement || 'spine') === 'spine' && n.kind !== 'human' && !n.panel && n.autobuild !== false
      && !(n.optional && !scope.include_optional);
    if (eligible) {
      const todo = !['confirmed', 'skipped'].includes(n.state) && (n.state !== 'stale' || scope.refresh_stale_first !== false);
      rows.push({ id: n.id, name: n.title, current: CURRENT[n.state] || '未开始', included: todo, running: n.state === 'running' });
    }
    if (n.id === scope.until_node) break;
  }
  const warnings = rows.some((s) => s.included && s.running) ? ['有 1 个未决决策会被按策略决定'] : [];
  return {
    slices: rows.map(({ running, ...s }) => s),
    below_threshold_text: BELOW[policy.advanced?.below_threshold] || BELOW.conservative_assumption,
    warnings, queued: false, queue_position: null,
  };
}

// AB-04 起的新一次运行：mock 不模拟推进，停在「排队中」
export function queuedRun(n, total) {
  return {
    run_id: `run-${n}`, run_no: n, state: 'queued', current: null, elapsed_ms: 0, est_total_ms: null,
    tokens: { used: 0 }, cost: { used: { amount: 0, currency: 'CNY' } },
    slices: { done: 0, total }, decisions: 0, suspended: 0, needs_review: 0, started_at: new Date().toISOString(), ended_at: null,
    acknowledged: false, pause_reason: null,
  };
}
