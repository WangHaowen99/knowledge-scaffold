// mock：系统设置（sys）的合成数据。模型名一律合成（model-*），厂商写「厂商 A」这类；
// 域名只用 example.com，密钥只给掩码占位，服务令牌的明文是明显的占位串。
import { MAIN_PID } from './fixtures.js';

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const iso = (t) => new Date(t).toISOString();
// 今天某个时刻（本地时区）
const todayAt = (h, m) => { const d = new Date(); d.setHours(h, m, 0, 0); return iso(d); };

export const GATEWAY = 'https://gateway.example.com';
const CTX_ENV = { window: 'ONTO_WEB_CONTEXT_WINDOW', max_output: 'ONTO_WEB_CONTEXT_MAX_OUTPUT_TOKENS' };

/** 压缩触发点（%）：按窗口与最大输出推算，留出输出与余量（mock 用的近似口径） */
export const deriveCompact = (w, o) => Math.max(50, Math.min(95, Math.floor(((w - o - 8000) / w) * 100)));

// SY-01 一个引擎（B2 第 8 条：configured、context 是保存的值、context_defaults 是留空时的值与来源）
function engine(id, { installed = true } = {}) {
  const stable = id === 'claude';
  return {
    label: stable ? '稳定引擎' : '内测引擎',
    channel: stable ? 'stable' : 'beta',
    installed,
    runtime_version: installed ? (stable ? '2.1.30' : '0.4.1') : null,
    base_url: stable ? `${GATEWAY}/anthropic` : `${GATEWAY}/v1`,
    api_key_masked: stable ? 'sk-•••• •••• ••••' : 'sk-•••• ••••',
    api_key_set: true,
    model: stable ? '' : 'model-standard',
    configured: true,
    context: stable ? { window: 200000, max_output: 32000, compact_at_pct: deriveCompact(200000, 32000) } : { window: 131072, max_output: 8192, compact_at_pct: deriveCompact(131072, 8192) },
    context_defaults: {
      window: { value: 200000, source: 'default', env: CTX_ENV.window },
      max_output: { value: 32000, source: 'default', env: CTX_ENV.max_output },
      compact_at_pct: { value: stable ? deriveCompact(200000, 32000) : deriveCompact(131072, 8192), source: 'derived' },
    },
  };
}
export function modelSettings({ betaInstalled = true, personalAllowed = true } = {}) {
  return {
    engines: { claude: engine('claude'), jiuwen: engine('jiuwen', { installed: betaInstalled }) },
    default_engine: 'claude',
    personal_key_allowed: personalAllowed,
    thinking: false,
  };
}

// SY-05：内置表与自定义表（合并在 sysModel.mergePresets）
export const BUILTIN_PRESETS = [
  { model: 'model-large-256k', vendor: '厂商 A', window: 256000, max_output: 32000, note: '厂商文档 2026-06', aliases: ['large-256k'], sources: ['https://docs.example.com/models/large'] },
  { model: 'model-long-200k', vendor: '厂商 B', window: 200000, max_output: 64000, note: '厂商文档 2026-06', aliases: [], sources: [] },
  { model: 'model-fast-200k', vendor: '厂商 C', window: 200000, max_output: 16000, note: '厂商文档 2026-06', aliases: [], sources: [] },
  { model: 'model-standard', vendor: '厂商 D', window: 131072, max_output: 16384, note: '厂商文档 2026-06', aliases: [], sources: [] },
  { model: 'model-lite-128k', vendor: '厂商 E', window: 128000, max_output: 16000, note: '厂商文档', aliases: [], sources: [] },
];
export const BUILTIN_VERIFIED_AT = '2026-06-30';
export function customPresetsSeed() {
  return [
    { model: 'model-standard', vendor: '厂商 D', window: 131072, max_output: 8192, note: '实测：超过 12 万后输出变短' },
    { model: 'model-coder-256k', vendor: '厂商 F', window: 256000, max_output: 32000, note: '运维 10-20 新增' },
  ];
}
export const CUSTOM_LOADED_AT = () => iso(new Date(new Date().getFullYear(), 9, 20, 14, 2));
// sys:ctxbad 时自定义表的错误（SY-05 custom.error）
export const CUSTOM_ERROR = { row: 4, column: 'window', message: '不是数字' };

// SY-09
export function sessionSettings() {
  return {
    permission_mode: 'bypassPermissions',
    allowed: ['bypassPermissions', 'default', 'acceptEdits', 'plan'],
    pool: { max_sessions: 12, idle_ttl_s: 600, permission_timeout_s: 600, source: 'env' },
  };
}

// SY-10（B2 第 11 条：detail 是未连接原因；sys:p1 时 purpose 为 null）
const PACKAGES = [
  ['pandas', '2.2.3', '表格计算'], ['numpy', '2.1.2', '数值计算'], ['openpyxl', '3.1.5', '读写 Excel'], ['pydantic', '2.9.2', '入参校验'],
  ['ortools', '9.11.4213', '求解评估器'], ['scipy', '1.14.1', '统计'], ['python-dateutil', '2.9.0', '日期处理'], ['jsonschema', '4.23.0', '契约校验'],
  ['pulp', null, '线性规划'],
];
export function sandbox({ connected = true, p1 = false } = {}) {
  return {
    connected,
    gateway: 'sandbox-gateway:8110',
    detail: connected ? null : '连不上 sandbox-gateway:8110（连接被拒绝）。脚本运行验证暂时改在 onto-modeling 容器内执行。',
    isolation: { non_root: true, readonly_root: true, memory_mb: 256, max_procs: 64, network: 'denied' },
    python: connected ? '3.11.9' : null,
    checked_at: todayAt(21, 40),
    packages: connected ? PACKAGES.map(([name, version, purpose]) => ({ name, version, purpose: p1 ? null : purpose })) : [],
  };
}

// SY-12：全局 MCP 服务器。前两个与项目设置（P-14）里的全局服务器同名同址
export const MCP_TOOLS = {
  'replenish-std-lookup': [
    { name: 'std_clause_search', description: '按关键词检索补货标准条款', read_only: true },
    { name: 'std_clause_get', description: '读取一条标准条款', read_only: true },
    { name: 'std_version_diff', description: '比较两版标准的差异', read_only: true },
  ],
  'mermaid-render': [
    { name: 'render_diagram', description: '把 Mermaid 文本渲染成图片', read_only: true },
    { name: 'list_styles', description: '列出可用的配色主题', read_only: true },
  ],
};
export function mcpServers({ p1 = false } = {}) {
  const at = todayAt(21, 40);
  const base = [
    { name: 'replenish-std-lookup', transport: 'http', url: 'https://mcp.example.com/std', command: null, args: [], enabled: true, auth: { kind: 'none' }, status: 'connected', status_detail: null, tools: MCP_TOOLS['replenish-std-lookup'], last_test_at: at, latency_ms: 86 },
    { name: 'mermaid-render', transport: 'stdio', url: null, command: 'npx', args: ['mermaid-mcp', '--stdio'], enabled: true, auth: { kind: 'none' }, status: 'connected', status_detail: null, tools: MCP_TOOLS['mermaid-render'], last_test_at: at, latency_ms: 140 },
    { name: 'kb-search', transport: 'http', url: 'https://mcp.example.com/search', command: null, args: [], enabled: true, auth: { kind: p1 ? 'none' : 'bearer', ...(p1 ? {} : { secret_ref: 'cred://mcp/kb-search' }) }, status: 'failed', status_detail: '401 未授权，请检查 Token', tools: [], last_test_at: at, latency_ms: null },
  ];
  if (!p1) return base;
  return base.map((s) => ({ ...s, status: 'unknown', status_detail: null, tools: [], last_test_at: null, latency_ms: null }));
}

// SY-14：配置注册表（B3b 第 3 条：每项带 description、precedence，来自插件的带 pack）
const UI_FIRST = ['data_dir', 'env', 'pack', 'default'];
const DEFAULT_ORDER = ['env', 'data_dir', 'pack', 'default'];
export function registryItems(state) {
  const item = (key, value, source, validation, effect, extra = {}) => ({
    key, value, source, validation, effect, secret: false, edit_page: null, description: '', precedence: DEFAULT_ORDER, ...extra,
  });
  return [
    item('ONTO_WEB_MAX_SESSIONS', '12', 'env', '整数 1–64', 'new_session', { edit_page: 'ops-ctl', precedence: UI_FIRST, description: '活跃会话上限（运维面可调整）' }),
    item('ONTO_WEB_AUTH_URL', 'https://auth.example.com', 'env', 'URL', 'restart', { description: 'auth-server 地址，空为本地模式' }),
    item('ANTHROPIC_BASE_URL', state.stableGateway, 'data_dir', 'URL，可达性探测', 'next_message', { edit_page: 'sys', precedence: UI_FIRST, description: '稳定引擎模型网关' }),
    item('ANTHROPIC_API_KEY', 'cred://data_dir/ANTHROPIC_API_KEY', 'data_dir', '非空，只存引用', 'next_message', { secret: true, edit_page: 'sys', precedence: UI_FIRST, description: '稳定引擎模型 Key' }),
    item('ONTO_WEB_PERMISSION_MODE', state.permissionMode, 'data_dir', '枚举 4 选 1', 'new_session', { edit_page: 'sys', precedence: UI_FIRST, description: '会话权限模式' }),
    item('context.presets', state.presetsText, 'data_dir', '表结构 + 数值', 'immediate', { edit_page: 'sys', description: '模型上下文推荐表（只做推荐，不参与运行时取值）' }),
    item('trace.max_cases_per_slice', '6', 'pack', '整数 1–20', 'next_message', { pack: 'trace@0.9.1', description: '每段最多推演的场景数' }),
    item('l4.max_concurrent_runs', String(state.maxRuns), 'data_dir', '整数 0–16', 'immediate', { edit_page: 'sys', description: '自主构建同时运行上限' }),
    item('backup.keep', '3', 'default', '整数 ≥ 1', 'immediate', { description: '保留的备份份数' }),
  ];
}

// SY-15、SY-16
export function quotas() {
  return {
    per_user: { concurrent_sessions: 2, daily_tokens: 2000000 },
    per_project: { online_engines: 1, monthly_tokens: 30000000 },
    not_enforced: [],
    overrides: [
      { id: 'qo-seed-1', subject: { kind: 'user', id: 'zhangsan', label: '张工' }, item: 'daily_tokens', value: 5000000, reason: '补货场景培训周', created_at: iso(Date.now() - 3 * DAY), created_by: 'zhangsan' },
      { id: 'qo-seed-2', subject: { kind: 'project', id: 'bike-fleet-7b20e4a1', label: '共享单车故障诊断' }, item: 'monthly_tokens', value: 60000000, reason: 'L4 夜间批量构建', created_at: iso(Date.now() - 6 * DAY), created_by: 'zhangsan' },
    ],
  };
}

// SY-17：自主构建的平台上限
export function autobuildLimits() {
  return {
    max_concurrent_runs: 4,
    night_priority: { enabled: true, start: '20:00', end: '08:00' },
    running: 2,
    // 与后端一致：夜间优先本期只存不用
    not_enforced: ['night_priority'],
  };
}

// SY-18（设计稿没画入口）
export function pricing() {
  return {
    currency: 'CNY',
    items: [
      { engine: 'claude', model: 'model-standard', input_per_mtok: 21.6, output_per_mtok: 108, cache_read_per_mtok: 2.16 },
      { engine: 'claude', model: 'model-large-256k', input_per_mtok: 28.8, output_per_mtok: 144, cache_read_per_mtok: 2.88 },
      { engine: 'jiuwen', model: 'model-standard', input_per_mtok: 4, output_per_mtok: 12, cache_read_per_mtok: null },
    ],
  };
}

// X-06（设计稿没画入口）：只存摘要，明文只在创建响应里出现一次
export function serviceTokens() {
  const now = Date.now();
  return [
    { id: 'svc-1a2b', name: '评测服务 · 拉包', scopes: ['releases.read'], project_ids: null, created_at: iso(now - 30 * DAY), created_by: 'zhangsan', expires_at: iso(now + 60 * DAY), last_used_at: iso(now - 2 * HOUR) },
  ];
}
/** mock 生成的明文：明显的占位串（格式 onto_svc_<urlsafe>） */
export const placeholderServiceToken = (n) => `onto_svc_mock-placeholder-${String(n).padStart(4, '0')}`;
