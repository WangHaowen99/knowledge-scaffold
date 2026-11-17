// mock 数据 · 插件中心（合成场景）：目录（PL-01）、详情（PL-02）、兼容检查（PL-03）用到的包信息。
// 目录 = fixtures.packs()（新建向导第 3 步的数据，名字、版本、默认开启、说明、界面都不改）+ 一个租户包 + 3 个转换器；
// 这里只给 packs() 的卡片补 meta、badges。
import { packs as basePacks } from './fixtures.js';

// ---------- 版本比较 ----------
const parts = (v) => String(v || '0').split('+')[0].split('-')[0].split('.').map((x) => Number(x) || 0);
export function compareVersions(a, b) {
  const [x, y] = [parts(a), parts(b)];
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    const d = (x[i] || 0) - (y[i] || 0);
    if (d) return d < 0 ? -1 : 1;
  }
  return 0;
}
// 范围只认清单里用到的写法：">=1.4"、">=1.0 <2.0"、"<2.0"、"1.4.2"
export function satisfies(version, range) {
  if (!range) return true;
  return String(range).trim().split(/\s+/).every((tok) => {
    const m = tok.match(/^(>=|<=|>|<|=)?(.+)$/);
    const c = compareVersions(version, m[2]);
    return { '>=': c >= 0, '<=': c <= 0, '>': c > 0, '<': c < 0, '=': c === 0, undefined: c === 0 }[m[1]];
  });
}

// ---------- mock 状态参数 ?mock=plugins:<标记>[+<标记>] ----------
export function pluginFlags(sc) {
  return new Set(String(sc?.plugins || '').split(/[+\s]+/).filter(Boolean)); // 地址里的 + 会解码成空格
}

// ---------- 目录 ----------
const META = {
  core: '22 命令 · 18 子代理', trace: '5 命令 · 段内 1 步 + 回归', codegen: '3 命令 · 旁支节点',
  diagram: '2 命令 · 1 skill',
  'industry-retail': '附「合规检查」', 'industry-property': '参考对象 22 个', 'industry-workorder': '参考对象 9 个',
  'sys-erp-generic': '表 126 · 状态码 48', 'sys-wms-generic': '表 64', 'sys-pos-generic': '表 38',
};
const BADGES = {};

// 租户上传的行业类检查包：只对启用了物业服务行业包的项目适用（applies_to 不进响应）
export const TENANT_PACKS = [
  {
    name: 'property-compliance', label: '物业合规检查', category: 'industry', latest: '1.0.0', version: '1.0.0', channel: 'stable',
    description: '发布前检查：挂在「场景模拟」之后、发布之前，不通过就挡住发布。', publisher: { kind: 'tenant', label: '示例租户' },
    ui: null, batch: null, badges: [], meta: '发布前检查 · 人工 + 脚本', project_status: null, locked_version: null, usage_note: null, default_on: false,
  },
];
export const APPLIES_TO = { 'property-compliance': { pack: 'industry-property', label: '物业服务' } };

// 多源连接器里的转换器（ADR-0073）：随连接器网关交付，平台已安装，不按项目启用
const CONVERTER_CARDS = [
  ['convert-pdf', 'PDF 转换', '把文字版 PDF 转成带页码的文本，表格转成表格；扫描件暂不支持。', ['.pdf'], 'pdfplumber（MIT）、pdfminer.six（MIT）'],
  ['convert-docx', 'Word 转换', '把 Word 文档转成文本，保留标题层级、列表和表格。', ['.docx'], 'python-docx（MIT）'],
  ['convert-xlsx', 'Excel 转换', '把 Excel 的每个工作表转成一张表格；大表只转前 2000 行。', ['.xlsx', '.xlsm'], 'openpyxl（MIT）'],
];
function converterCards() {
  return CONVERTER_CARDS.map(([name, label, description, types, license]) => ({
    name, label, description, category: 'provider', version: '1.0.0', latest: '1.0.0', channel: 'stable', publisher: { kind: 'official', label: '平台' },
    ui: null, batch: null, badges: [], meta: null, project_status: 'platform_installed', locked_version: null, usage_note: null, default_on: false,
    converter: { types, default_for: types, license },
  }));
}

/**
 * 整个目录（不带项目视角）。flags 见 routes-plugins.js 顶部：ind = 零售连锁 1.1.0 已发布到 stable。
 * packs() 的项保持原样，只补 meta、badges。
 */
export function catalogCards(flags = new Set()) {
  const base = basePacks().map((p) => ({
    ...p, meta: p.meta ?? META[p.name] ?? null, badges: [...new Set([...(p.badges || []), ...(BADGES[p.name] || [])])],
    ...(flags.has('ind') && p.name === 'industry-retail' ? { version: '1.1.0', latest: '1.1.0' } : null),
  }));
  return [...base, ...TENANT_PACKS.map((p) => ({ ...p, badges: [...p.badges] })), ...converterCards()];
}

const INSTALLED = ['required', 'enabled', 'update', 'pending'];
export const isInstalledStatus = (s) => INSTALLED.includes(s);

/**
 * 本项目视角的状态（契约 PL-01 的表）：按 PL-05 的能力表算，和项目设置说的是同一件事。
 * @param card 目录里的一项；pp 是 PL-05 里同名的一项（没有就是没启用过）
 * @param o {project: bool, enabledNames: string[]（本项目生效中的包）}
 */
export function projectStatusOf(card, pp, o) {
  if (card.converter) return 'platform_installed';
  if (!o.project) return card.project_status ?? null; // 不带项目：core 必装、离线部署不可用照目录（契约变更 F1 第 3 条）
  if (pp?.status === 'required') return 'required';
  if (pp?.status === 'pending') return 'pending';
  if (pp?.status === 'enabled') return pp.latest_available && compareVersions(pp.latest_available, pp.locked_version) > 0 ? 'update' : 'enabled';
  if (card.project_status === 'offline') return 'offline';
  const need = APPLIES_TO[card.name];
  if (need && !o.enabledNames.includes(need.pack)) return 'not_applicable';
  return 'available';
}

/** 一张卡的项目视角：状态、锁定版本、可更新到的版本、说明 */
export function projectCard(card, pp, o) {
  const status = projectStatusOf(card, pp, o);
  const out = { ...card, project_status: status, locked_version: pp && pp.status !== 'disabled' ? pp.locked_version : null };
  if (status === 'update' && compareVersions(pp.latest_available, card.latest) > 0) out.latest = pp.latest_available;
  if (status === 'not_applicable') out.usage_note = `适用于${APPLIES_TO[card.name].label}项目`;
  return out;
}

/** counts：按整个目录算（不受筛选影响），enabled 含待生效的（契约变更 B3a 第 3 条）；不带项目时「已启用」「可更新」为 0 */
export function catalogCounts(cards, { project = true } = {}) {
  const counts = { all: cards.length };
  ['capability', 'industry', 'system', 'provider', 'node', 'exporter'].forEach((k) => { counts[k] = cards.filter((c) => c.category === k).length; });
  counts.enabled = project ? cards.filter((c) => isInstalledStatus(c.project_status)).length : 0;
  counts.updatable = project ? cards.filter((c) => c.project_status === 'update').length : 0;
  return counts;
}

/** PL-01 的筛选：category（多值）、q、installed_only、updatable_only */
export function filterCatalog(cards, { category = [], q = '', installedOnly = false, updatableOnly = false } = {}) {
  const needle = q.trim().toLowerCase();
  return cards.filter((c) => (!category.length || category.includes(c.category))
    && (!needle || `${c.name} ${c.label} ${c.description}`.toLowerCase().includes(needle))
    && (!installedOnly || isInstalledStatus(c.project_status))
    && (!updatableOnly || c.project_status === 'update'));
}

// 推荐（契约 PL-01：按资料、已登记的数据源和分段进度，最多 3 条）。只给主示例项目，理由对得上它的分段
const REC_REASON = {
  'industry-retail': '新增临期损耗规则 3 条，正好对应还没开始的分段「临期与损耗检查」',
  diagram: '流程图支持泳道和审批分支，「超量与审批提示」这一段画图时用得上',
};
export function recommendationsOf(cards, { limit = 2 } = {}) {
  const want = [['industry-retail', 'update', 'upgrade'], ['diagram', 'update', 'upgrade']];
  return want
    .map(([name, status, action]) => {
      const c = cards.find((x) => x.name === name && x.project_status === status);
      return c ? { pack: { name: c.name, version: c.latest, label: c.label }, reason: REC_REASON[name], action } : null;
    })
    .filter(Boolean)
    .slice(0, limit);
}

// ---------- 版本要求（兼容检查、更新记录的「需要先升级」） ----------
// 每个版本对 core 的依赖范围；?mock=plugins:held 时场景模拟 0.10.0 依赖还在 beta 的基础建模 1.5
export function requiresCore(name, version, flags = new Set()) {
  if (name === 'core') return null;
  if (name === 'trace' && version === '0.10.0' && flags.has('held')) return '>=1.5';
  return '>=1.4';
}
export const KERNEL_VERSION = '1.2.0';
export const CORE_BETA = '1.5.0';

// ---------- 详情（PL-02） ----------
const KB = 1024;
const at = (d) => `${d}T10:00:00.000Z`;
const log = (version, date, notes) => ({ version, date: at(date), notes });
const MAIN_UI = '左栏页面';

// 每个包的详情。flow_insertions 的 at 是插在主线哪一段（research | model | validate | publish）；
// kind、short 是兼容检查「流程组装」那一行与统计卡里的叫法；flow_note、config_note、contents[].note、contents[].total 是 mock 补的说明，真实后端没有时页面不显示
const DETAILS = {
  core: {
    description: '主入口、资料分析、抽取对象 / 操作 / 规则、校验、查询、更新与影响分析，含主流程节点。主线的几个阶段由它定义，其他包往里加步骤。',
    size: 612 * KB, files: 96, kernel: true,
    flow_note: '主线的 5 个阶段由基础建模定义；能力包、行业包往里加步骤',
    contents: [
      { group: '命令', items: ['主入口', '资料分析', '抽取对象', '抽取操作', '抽取规则', '校验', '查询', '增量更新', '影响分析'] },
      { group: '助手角色', items: ['资料分析', '对象抽取', '操作抽取', '规则抽取', '语义校验', '影响分析'] },
      { group: '模板', items: ['对象', '操作', '规则', '指标'] },
      { group: '检查规则', items: ['写入即查'], note: '写入即查在每次改条目后跑' },
      { group: '界面入口', items: ['工作台', 'Studio', '资料', '发布'], note: '内核页面，不能停用' },
    ],
    config: [{ id: 'research_mode', label: '资料分析方式', value: '本地' }],
    changelog: [
      log('1.4.2', '2026-10-09', '资料分析按用途分组；写入即查的报错带出处'),
      log('1.4.1', '2026-09-25', '修复增量更新时影响面漏算规则'),
      log('1.4.0', '2026-09-10', '主流程改由 flow.yaml 定义，能力包可以往里加步骤'),
    ],
  },
  trace: {
    description: '从样例资料生成验收场景，逐条推演操作是否走得通；示例数据按场景需要生成。装上后校验之后多出「场景模拟」一步；不装就没有。',
    size: 186 * KB, files: 23,
    flow_insertions: [
      { id: 'trace.trace', kind: '串入节点', title: '场景模拟', at: 'validate', note: '串在「校验」之后', short: '场景模拟' },
    ],
    flow_note: '示例数据按场景需要生成，不单独占一步',
    contents: [
      { group: '加进流程', items: ['场景模拟'], note: '串在「校验」之后。对象或操作改了，相关场景会标为过期' },
      { group: '命令', items: ['场景模拟', '单个场景模拟', '批量验证', '交互模拟', '生成示例数据'] },
      { group: '助手角色', items: ['场景推演', '示例数据生成'] },
      { group: '模板', items: ['验收场景', '示例数据'] },
      { group: '检查规则', items: ['场景推演通过率 ≥ 80% 才能发布'], note: '不满足时在发布检查里列出' },
      { group: '界面入口', items: [`${MAIN_UI}「场景模拟」`, '对话快捷操作：批量验证', '对话快捷操作：交互模拟'], note: '启用后项目左栏「插件」分组出现「场景模拟」页面；停用后页面消失，已产生的场景留在知识包里' },
      { group: '报告格式', items: ['模拟报告'], note: '报告审阅里逐条展示' },
    ],
    config: [{ id: 'max_per_slice', label: '每段最多场景数', value: 6 }, { id: 'regression_mode', label: '回归模式', value: '开' }],
    changelog: [
      log('0.10.0', '2026-10-28', '场景输入写明类型，推演时显式传入；回归时自动加上「守护老路径」的场景'),
      log('0.9.1', '2026-10-12', '默认回归模式（107 秒对 360 秒）；示例数据跳过「数据来源：无」的对象'),
      log('0.9.0', '2026-09-30', '交互模拟支持分支选择卡；模拟报告按包声明的格式逐条审阅'),
      log('0.8.2', '2026-09-08', '修复批量模拟时子代理超时不重试'),
    ],
  },
  codegen: {
    description: '把复杂计算写成脚本并在沙箱跑通；维护求解契约。旁支，不挡主线。',
    size: 148 * KB, files: 19,
    flow_insertions: [
      { id: 'codegen.codegen', kind: '旁支节点', title: '代码生成', at: 'model', note: '旁支 · 不挡主线', short: '代码生成' },
      { id: 'codegen.solver', kind: '旁支节点', title: '求解适配', at: 'validate', note: '旁支 · 在页面里配置', short: '求解适配' },
    ],
    flow_note: '两步都是旁支，跳过也不挡发布',
    contents: [
      { group: '命令', items: ['扫描脚本候选', '生成脚本', '生成评估器'] },
      { group: '助手角色', items: ['脚本生成'] },
      { group: '模板', items: ['脚本描述符', '求解契约'] },
      { group: '检查规则', items: ['求解契约要有权威回执才能发布'], note: '发布检查里的「求解契约回执」' },
      { group: '界面入口', items: [`${MAIN_UI}「代码生成」`], note: '启用后项目左栏「插件」分组出现「代码生成」页面' },
    ],
    config: [{ id: 'sandbox_timeout', label: '沙箱超时', value: '120 秒' }],
    changelog: [
      log('0.9.2', '2026-10-18', '求解契约表单按模板参数还原；评估器失败时给出约束位置'),
      log('0.9.1', '2026-10-06', '修复沙箱里中文路径的脚本读不到'),
      log('0.9.0', '2026-09-22', '新增求解契约与评估器'),
    ],
  },
  diagram: {
    description: '流程图创建与校验、drawio 转换、mermaid 流程图 skill。启用后对话里出现流程图卡，Studio 多出「流程图」条目类型；未启用时都不出现。',
    size: 76 * KB, files: 11,
    flow_insertions: [{ id: 'diagram.flowchart', kind: '旁支节点', title: '流程图', at: 'model', note: '旁支 · 按操作画图', short: '流程图' }],
    contents: [
      { group: '命令', items: ['画流程图', '校验流程图'] },
      { group: '技能', items: ['mermaid 流程图'] },
      { group: '模板', items: ['drawio 流程图'] },
      { group: '检查规则', items: ['流程图节点要对得上操作步骤'] },
      { group: '界面入口', items: ['对话里的流程图卡', 'Studio 条目类型「流程图」'], note: '没有独立页面；停用后流程图卡和条目类型都不出现，已画的图保留' },
    ],
    config: [],
    changelog: [
      log('0.6.0', '2026-10-27', '支持泳道和审批分支；drawio 导出保留泳道'),
      log('0.5.0', '2026-10-10', '流程图校验对上操作步骤；mermaid 转 drawio'),
      log('0.4.2', '2026-09-18', '修复中文节点名的换行'),
    ],
  },
  'industry-retail': {
    description: '零售连锁行业知识：术语、规则、参考对象和示例项目，附「合规检查」关卡。行业知识现读即生效，不用重连。',
    size: 412 * KB, files: 58,
    flow_insertions: [{ id: 'retail.compliance', kind: '关卡', title: '合规检查', at: 'publish', note: '关卡 · 发布之前', short: '合规检查' }],
    flow_note: '挂在「场景模拟」之后、发布之前，不通过就挡住发布',
    contents: [
      { group: '术语', total: 312, items: ['补货单', '安全库存', '陈列量', '到货周期', '临期损耗', '门店等级'] },
      { group: '规则', total: 48, items: ['按类取标准', '门店申请取严', '临时加量须审批', '临期分级'] },
      { group: '参考对象', total: 36, items: ['门店', '商品品类', '补货单', '补货标准表'] },
      { group: '示例项目', items: ['门店补货（示例）', '促销备货（示例）'] },
      { group: '检查规则', items: ['合规检查'], note: '发布前检查，不通过就挡住发布' },
    ],
    config: [],
    changelog: [
      log('1.0.3', '2026-10-05', '补充乳制品陈列标准 2024 版的术语'),
      log('1.0.2', '2026-09-20', '修订到货周期的取值口径'),
      log('1.0.0', '2026-09-01', '首个版本'),
    ],
    upgrade_brings: '- 临期损耗规则 3 条，对应你还没开始的分段「临期与损耗检查」\n- 样例补货单 2 份（乳制品陈列标准 2024 版），可作场景来源\n- 「陈列量标准」参考对象新增到货周期维度',
    upgrade_impact: { needs_review_after_upgrade: 0, affected_artifacts: 4, note: '「合规检查」节点的完成判定没有变化，已确认的产物不会被标为需复核。用到行业知识的 4 个产物会在下次校验时按新规则检查。' },
  },
  'industry-property': {
    contents: [
      { group: '术语', total: 268, items: ['报修单', '工单', '维修班组', '紧急程度'] },
      { group: '规则', total: 31, items: ['紧急报修 30 分钟内响应', '同一部位重复报修合并'] },
      { group: '参考对象', total: 22, items: ['业主', '房屋', '报修单', '维修工单'] },
    ],
    changelog: [log('1.2.0', '2026-10-11', '补充公共区域报修的术语'), log('1.1.0', '2026-09-15', '首个版本')],
  },
  'industry-workorder': {
    contents: [{ group: '术语', total: 96, items: ['工单', '派单', '回访'] }, { group: '参考对象', total: 9, items: ['工单', '处理人', '回访记录'] }],
    changelog: [log('0.8.1', '2026-10-02', '首个公开版本')],
  },
  'sys-erp-generic': {
    contents: [{ group: '表字典', total: 126, items: ['goods_category', 'replenish_std', 'store', 'replenish_order'] }, { group: '状态码', total: 48, items: ['订单状态', '库存状态'] }],
    changelog: [log('0.3.0', '2026-10-07', '补充补货与门店相关表'), log('0.2.0', '2026-09-14', '首个版本')],
  },
  'sys-wms-generic': {
    contents: [{ group: '表字典', total: 64, items: ['location', 'stock', 'inbound', 'outbound'] }],
    changelog: [log('0.4.0', '2026-10-01', '补充库位与盘点表')],
  },
  'sys-pos-generic': {
    contents: [{ group: '表字典', total: 38, items: ['sale', 'sale_item', 'refund'] }],
    changelog: [log('0.2.1', '2026-09-27', '常用收银表结构')],
  },
  'property-compliance': {
    flow_insertions: [{ id: 'property.compliance', kind: '关卡', title: '物业合规检查', at: 'publish', note: '关卡 · 发布之前', short: '物业合规检查' }],
    contents: [{ group: '检查规则', items: ['报修闭环时限', '收费项目公示'], note: '人工确认加脚本检查，不通过就挡住发布' }],
    changelog: [log('1.0.0', '2026-10-21', '租户上传，静态检查通过后发布')],
  },
};

/** 详情的静态部分（不含项目视角）；没有专门写的包按目录卡片生成一份 */
export function detailOf(card, flags = new Set()) {
  const d = DETAILS[card.name] || {};
  let changelog = d.changelog || [log(card.latest, '2026-10-01', '首个版本')];
  if (flags.has('ind') && card.name === 'industry-retail') {
    changelog = [log('1.1.0', '2026-10-30', '新增临期损耗规则 3 条；样例补货单 2 份；「陈列量标准」参考对象新增到货周期维度'), ...changelog];
  }
  if (flags.has('held') && card.name === 'core') {
    changelog = [log(CORE_BETA, '2026-10-29', 'beta 通道小范围试用：节点完成判定按段计算；场景模拟 0.10.0 依赖这一版'), ...changelog];
  }
  return {
    description: d.description || card.description,
    size: d.size ?? 48 * KB,
    files: d.files ?? 8,
    kernel: !!d.kernel,
    flow_insertions: d.flow_insertions || [],
    flow_note: d.flow_note ?? null,
    contents: d.contents || [],
    config: d.config || [],
    changelog,
    brings: d.brings || null,
    upgrade_brings: d.upgrade_brings || null,
    upgrade_impact: d.upgrade_impact || null,
  };
}

// 本项目里的产出统计（PL-02 project_stats；真实后端 P1 恒为 null）。只给主示例项目
export function projectStatsOf(name, { entries, scripts, flowcharts }) {
  const S = {
    core: {
      produced: { value: entries.total, unit: '个条目', note: entries.note },
      metric: { label: '分段进度', value: '1/5', note: '已确认 1 段 · 进行中 1 段' },
    },
    trace: {
      produced: { value: 23, unit: '个场景', note: '推演通过 19 · 过期 3' },
      metric: { label: '推演通过率', value: '83%', note: '近 7 天 · 回归模式' },
    },
    codegen: {
      produced: { value: scripts, unit: '个脚本', note: '求解契约 1 份' },
      metric: { label: '沙箱验证', value: '通过', note: '最近一次 10-31' },
    },
    diagram: {
      produced: { value: flowcharts.total, unit: '张流程图', note: flowcharts.note },
      metric: { label: '流程图校验', value: '通过', note: '门店补货主流程' },
    },
    'industry-retail': {
      produced: { value: 24, unit: '条行业知识', note: '行业层 · 只读' },
      metric: { label: '合规检查', value: '未运行', note: '场景模拟确认后才能运行' },
    },
  };
  return S[name] || null;
}
