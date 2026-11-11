// mock 代码生成页的数据（全部合成，主示例项目「门店补货单编制」）：可代码化候选、脚本、沙箱验证、脚本原文、求解契约。
// 操作名称取 scenario.js；scenario.js 里只有 build_replenish_order.py 一个脚本条目，另外两个脚本名是这里补的。
import { entryRef } from './scenario.js';

const at = (m, d, h, mi) => new Date(2026, m - 1, d, h, mi).toISOString();
export const SCANNED_AT = at(10, 31, 15, 18);
export const VERIFIED_AT = at(10, 31, 15, 19);

// ---------- 可代码化候选（最近一份代码生成扫描报告） ----------
export function candidates() {
  return {
    scanned_at: SCANNED_AT,
    items: [
      { operation: entryRef('safety_stock_calc'), reason: '按类取标准再取严，规则明确，输入都能结构化', suitable: 'yes', selected_default: true },
      { operation: entryRef('display_qty_check'), reason: '按品类 + 到货周期查表', suitable: 'yes', selected_default: true },
      { operation: entryRef('overage_and_hint'), reason: '逐项比较申请量和标准上限', suitable: 'partial', selected_default: false },
    ],
    unsuitable: [{ operation: entryRef('intake_checks'), reason: '需要语义判断' }],
  };
}

// ---------- 脚本 ----------
const scriptRef = (key) => ({ kind: 'script', id: `script:${key}`, key, name: `${key}.py` });
export const SCRIPT_REF = {
  safety: scriptRef('safety_stock_strictest'),
  order: entryRef('build_replenish_order'),
  overage: scriptRef('overage_compare'),
};
// 「生成脚本」命令按操作产出的脚本名
export const SCRIPT_FOR_OPERATION = {
  safety_stock_calc: 'safety_stock_strictest',
  display_qty_check: 'display_qty_lookup',
  overage_and_hint: 'overage_compare',
  render_order: 'build_replenish_order',
};
export { scriptRef };

const passed = (ms) => ({ status: 'passed', duration_ms: ms, ran_at: VERIFIED_AT });
export function scripts() {
  return [
    {
      script: SCRIPT_REF.safety, operation: entryRef('safety_stock_calc'),
      contract: { status: 'passed', findings: [] }, descriptor: 'synced', sandbox: passed(1200), actions: ['regenerate'],
    },
    {
      script: SCRIPT_REF.order, operation: entryRef('render_order'),
      contract: { status: 'passed', findings: [] }, descriptor: 'stale', sandbox: passed(800), actions: ['regenerate_descriptor', 'regenerate'],
    },
    {
      script: SCRIPT_REF.overage, operation: entryRef('overage_and_hint'),
      contract: {
        status: 'failed',
        findings: [{ code: 'script_argument_shape', level: 'error', message: '头部注释声明的参数和 argparse 对不上：缺少 delivery_cycle', target: SCRIPT_REF.overage, path: 'scripts/overage_compare.py', field: 'delivery_cycle' }],
      },
      descriptor: 'none', sandbox: { status: 'not_run', duration_ms: null, ran_at: null }, actions: ['fix'],
    },
  ];
}

// ---------- 沙箱验证（SC-02） ----------
export function verification(key) {
  const base = { ran_at: VERIFIED_AT, network_access: false };
  if (key === 'safety_stock_strictest') {
    return {
      ...base, status: 'passed', duration_ms: 1200,
      cases: [
        { args: '--category 乳制品 --store_request 96', output: '{"safety_stock": 96, "rule": "R-09"}', passed: true },
        { args: '--category 乳制品 --store_request 130', output: '{"safety_stock": 120, "rule": "R-05"}', passed: true },
      ],
    };
  }
  if (key === 'build_replenish_order') {
    return {
      ...base, status: 'passed', duration_ms: 800,
      cases: [
        { args: '--store_code A001 --request_qty 96', output: '{"order_qty": 96, "overage": false, "std_max": 120}', passed: true },
        { args: '--store_code B002 --request_qty 132', output: '{"order_qty": 132, "overage": true, "std_max": 120}', passed: true },
      ],
    };
  }
  return null;
}

// 生成、修复后的沙箱输出（合成输入）
export function verificationAfterRun(key, ms) {
  return {
    ran_at: new Date().toISOString(), network_access: false, status: 'passed', duration_ms: ms,
    cases: [
      { args: '--category 乳制品 --delivery_cycle 隔日', output: '{"ok": true, "std_max": 120}', passed: true },
      { args: '--category 乳制品 --delivery_cycle 每日', output: '{"ok": true, "std_max": 150}', passed: true },
    ],
  };
}

// ---------- 脚本原文（Studio 条目文件；F-02 经 db.files 读） ----------
export const scriptPath = (name) => `scripts/${name}`;
export function scriptSource(key) {
  if (key === 'build_replenish_order') {
    return [
      '"""生成补货单',
      '输入: store_code (文本) 门店编码；request_qty (数值, 箱) 申请量',
      '输出: order_qty (数值, 箱)；overage (布尔) 是否超量',
      '依据: [[补货单]]、[[超量清单]]、规则 R-09',
      '"""',
      'import argparse, json',
      'ap = argparse.ArgumentParser()',
      "ap.add_argument('--store_code', required=True)",
      "ap.add_argument('--request_qty', type=int, required=True)",
      'args = ap.parse_args()',
      'std_max = 120',
      'order_qty = args.request_qty',
      "print(json.dumps({'order_qty': order_qty, 'overage': order_qty > std_max, 'std_max': std_max}))",
      '',
    ].join('\n');
  }
  if (key === 'safety_stock_strictest') {
    return [
      '"""安全库存取严',
      '输入: category (文本) 品类；store_request (数值, 箱) 门店申请量',
      '输出: safety_stock (数值, 箱)；rule (文本) 采用的规则',
      '依据: [[安全库存标准]]、规则 R-05、R-09',
      '"""',
      'import argparse, json',
      'ap = argparse.ArgumentParser()',
      "ap.add_argument('--category', required=True)",
      "ap.add_argument('--store_request', type=int, required=True)",
      'args = ap.parse_args()',
      'std = 120',
      "rule = 'R-09' if args.store_request <= std else 'R-05'",
      "print(json.dumps({'safety_stock': min(args.store_request, std), 'rule': rule}))",
      '',
    ].join('\n');
  }
  if (key === 'overage_compare') {
    return [
      '"""超量项比较',
      '输入: category (文本) 品类；delivery_cycle (枚举: 每日/隔日/每周) 到货周期',
      '输出: overage (布尔) 是否超量；hint (文本) 审批提示',
      '依据: [[超量清单]]、[[审批提示]]、规则 R-11',
      '"""',
      'import argparse, json',
      'ap = argparse.ArgumentParser()',
      "ap.add_argument('--category', required=True)",
      'args = ap.parse_args()',
      '',
    ].join('\n');
  }
  return [
    `"""${key}`,
    '输入: category (文本) 品类；delivery_cycle (枚举: 每日/隔日/每周) 到货周期',
    '输出: std_max (数值, 箱)',
    '依据: [[陈列量标准]]、规则 R-07',
    '"""',
    'import argparse, json',
    'ap = argparse.ArgumentParser()',
    "ap.add_argument('--category', required=True)",
    "ap.add_argument('--delivery_cycle', choices=['每日', '隔日', '每周'], required=True)",
    '',
  ].join('\n');
}

// ---------- 求解契约（SV-01） ----------
// 问题族目录与模板：口径同后端注册目录（candidate_selection.v1）；label 是界面上的中文名
export const FAMILIES = [
  { id: 'candidate_selection.v1', label: '批次二选一规划', capabilities: ['domain_candidate_evaluation', 'lexicographic_objective'] },
];
export const PROFILES = ['ordered_binary_route.v1'];
export const ROLES = ['entry', 'fact_snapshot_schema', 'input_schema', 'presentation', 'result_schema'];
export function template() {
  return {
    declaration: { engine_requirements: { problem_family: FAMILIES[0].id, capabilities: [...FAMILIES[0].capabilities] } },
    params: { display_mapping: { OPTION_0: '', OPTION_1: '' }, title: '', evaluator_path: '', process_name: '', stream_name: '' },
  };
}

// 模板参数 → 五份附属文件（按 role）；只为 mock 自洽，不是后端的真实模板
export function renderSidecars(solverKey, params) {
  const p = params || {};
  const map = p.display_mapping || {};
  const model = [
    `id: ${solverKey}_model`,
    `profile: ${PROFILES[0]}`,
    'display_mapping:',
    `  OPTION_0: ${map.OPTION_0 || ''}`,
    `  OPTION_1: ${map.OPTION_1 || ''}`,
    ...(p.evaluator_path ? [`evaluator_path: ${p.evaluator_path}`] : []),
    ...(p.process_name || p.stream_name ? ['text_presentation:', '  kind: ordered_route_plan_zh.v1', `  process_name: ${p.process_name || ''}`, `  stream_name: ${p.stream_name || ''}`] : []),
  ];
  const schema = (required, properties) => `${JSON.stringify({ type: 'object', required, properties }, null, 2)}\n`;
  return {
    entry: `${model.join('\n')}\n`,
    input_schema: schema(['plan_month', 'items'], { plan_month: { type: 'string' }, items: { type: 'array', items: { type: 'object', required: ['item_id', 'batch_qty'] } } }),
    result_schema: schema(['candidates'], { candidates: { type: 'array' } }),
    fact_snapshot_schema: schema(['route_metrics'], { route_metrics: { type: 'array' } }),
    presentation: `title: ${p.title || ''}\n`,
  };
}

const MAIN_KEY = 'restock_route';
export const MAIN_SOLVER = { key: MAIN_KEY, operation: '补货编制主流程' };
export const evaluatorPath = (key) => `ontology/scripts/${key}_evaluator.py`;

// 内部记录：{ solver_key, binding, template_params, mode, sidecars, evaluator: 路径|null }
export function solverOperations() {
  const mainParams = {
    display_mapping: { OPTION_0: '门店直送', OPTION_1: '仓库中转' },
    title: '补货调拨方案', evaluator_path: '', process_name: '门店补货规划', stream_name: '到货方式',
  };
  const otherParams = {
    display_mapping: { OPTION_0: '打折促销', OPTION_1: '退回仓库' },
    title: '临期处理方案', evaluator_path: '', process_name: '', stream_name: '',
  };
  return [
    {
      solver_key: MAIN_KEY,
      binding: {
        solver_key: MAIN_KEY,
        operation: '补货编制主流程',
        decision_model: `${MAIN_KEY}_model`,
        declaration: { engine_requirements: { problem_family: FAMILIES[0].id, capabilities: [...FAMILIES[0].capabilities] } },
        operation_frontmatter: {},
        units: [
          { field: 'batch_qty', unit: '箱', note: '批次量' },
          { field: 'days', unit: '天', note: '到货天数' },
          { field: 'daily_sales', unit: '箱/天', note: '日均销量' },
          { field: 'shelf_capacity', unit: '箱', note: '陈列容量' },
        ],
      },
      template_params: mainParams,
      mode: 'template',
      sidecars: renderSidecars(MAIN_KEY, mainParams),
      evaluator: evaluatorPath(MAIN_KEY),
    },
    {
      solver_key: 'expiry_disposal',
      binding: {
        solver_key: 'expiry_disposal',
        operation: '临期损耗编制',
        decision_model: 'expiry_disposal_model',
        declaration: { engine_requirements: { problem_family: FAMILIES[0].id, capabilities: [FAMILIES[0].capabilities[0]] } },
        operation_frontmatter: { execution: { enabled: false } },
        units: [{ field: 'batch_qty', unit: '箱', note: '批次量' }],
      },
      template_params: otherParams,
      mode: 'template',
      sidecars: renderSidecars('expiry_disposal', otherParams),
      evaluator: null,
    },
  ];
}
