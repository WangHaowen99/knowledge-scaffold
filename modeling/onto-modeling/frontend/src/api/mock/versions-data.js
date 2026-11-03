// mock · 版本历史的静态部分（合成场景）。
// 主示例项目「门店补货单编制」：v0.1 导入 → v0.12 第一段生效（a71e03c9，F1 的主线）→ 自主构建运行 #3 的各段（v0.13～v0.19，
// 与 run3.js 的按段结果一致：安全库存测算 v0.14、陈列量核定 v0.15、临期与损耗检查 v0.17、超量与审批提示 v0.19），
// 运行中途用户在 Studio 给「陈列量标准」加了属性「到货周期」（v0.18，版本对比的示例）。
// 每条改动可带 undo：从「这个版本之后」的内容推回「这个版本之前」的写法，mock 用它按需还原历史文件（不存整份快照）。
import { RUN_ID, RUN_NO, todayAt } from './run3.js';
import { ENTRIES, SLICES, entryRef } from './scenario.js';

const HOUR = 3600 * 1000;
const dayAt = (daysAgo, h, m) => {
  const d = new Date(Date.now() - daysAgo * 24 * HOUR);
  d.setHours(h, m, 0, 0);
  return d.toISOString();
};
const SLICE_NAME = Object.fromEntries(SLICES.map((s) => [s.id, s.name]));
const R = (key) => entryRef(key);
const SCN = (key, title) => ({ type: 'trace.scenario', key, title });
// change：add / mod / del；review：needs_review / accepted / null
const ch = (target, change, detail, review = null, undo = null) => ({ target, change, detail, review, undo });
const run = (slice) => ({ run_id: RUN_ID, run_no: RUN_NO, slice_id: slice, slice_name: SLICE_NAME[slice] });

export const SOURCE_LABEL = {
  assistant: '助手', web: '网页', studio: 'Studio', mcp: 'MCP', autobuild: '自主构建', import: '导入', plugin: '插件', revert: '回退',
};
export const TRAILER_SOURCE = { assistant: 'agent' };

// 门店特殊需求：v0.9 网页编辑补的一节，v0.10 回退时撤掉了
const COLD_CHAIN = ['', '## 冷链', '乳制品到货时核对冷链温度记录，超过 4℃ 拒收。'];
const DESC_DISPLAY = ['按商品品类和到货周期确定的陈列量上下限。', '按商品品类确定的陈列量上下限。'];

// [n, 提交, 时间, 来源, 说明, 改动, 其他]
function rows() {
  return [
    [1, '3b9e1f04', dayAt(6, 9, 30), 'import', '导入已发布知识包 retail-restock-0.0.9.zip', [
      ch(R('replenish_order'), 'add', '新增'), ch(R('store'), 'add', '新增'), ch(R('goods_category'), 'add', '新增'), ch(R('display_qty_std'), 'add', '新增'),
    ]],
    [2, '6d20a8c1', dayAt(5, 10, 12), 'assistant', '资料分析确认：7 份资料，2 份作为建模目标依据', [
      ch({ type: 'file', key: 'research/material-map.json', title: '', label: '' }, 'add', '新增'),
    ], { changeset_id: 'cs-21' }],
    [3, '91c47e2a', dayAt(5, 16, 40), 'assistant', '建模蓝图确认：5 段，先做「补货申请与门店需求」', [
      ch({ type: 'file', key: 'research/blueprint.json', title: '建模蓝图', label: '建模蓝图' }, 'add', '新增'),
    ], { changeset_id: 'cs-22' }],
    [4, 'b5e0d913', dayAt(4, 11, 5), 'assistant', '助手：对象「补货单」「门店」，新增「门店特殊需求」', [
      ch(R('store_request'), 'add', '新增', null, { add: true }), ch(R('replenish_order'), 'mod', '修改 · 2 处'), ch(R('store'), 'mod', '修改 · 1 处'),
    ], { changeset_id: 'cs-23' }],
    [5, '2fa6c7d8', dayAt(4, 15, 30), 'studio', 'Studio 编辑：补货单 增加属性「申请量」', [
      ch(R('replenish_order'), 'mod', '修改 · 1 处', null, { row: '申请量' }),
    ]],
    [6, '7c1e5b02', dayAt(3, 10, 20), 'assistant', '助手：操作「受理三道校验」，规则 R-01、R-02', [
      ch(R('intake_checks'), 'add', '新增', null, { add: true }), ch(R('r01_intake_scope'), 'add', '新增', null, { add: true }), ch(R('r02_required'), 'add', '新增', null, { add: true }),
    ], { changeset_id: 'cs-24' }],
    [7, 'e48d0a6f', dayAt(3, 16, 8), 'assistant', '质量检查 0 错误', [
      ch(R('r03_required_hint'), 'add', '新增', null, { add: true }),
    ], { changeset_id: 'cs-25', tags: [{ name: 'mark-1761811680-3a7c', label: '校验通过的里程碑' }] }],
    [8, '0a9f3c71', dayAt(2, 17, 32), 'mcp', 'MCP 写入：新增对象 补货台账（Token「业务 Agent 读写联调」）', [
      ch(R('replenish_ledger'), 'add', '新增', null, { add: true }),
    ]],
    [9, 'd2b7e185', dayAt(2, 22, 15), 'web', '网页编辑：门店特殊需求.md 补充「冷链」一节', [
      ch(R('store_request'), 'mod', '修改 · 1 处', null, { cut: COLD_CHAIN }),
    ]],
    [10, '5e81a4c3', dayAt(1, 10, 40), 'revert', '回退到 v0.8：撤回 v0.9 的网页编辑', [
      ch(R('store_request'), 'mod', '恢复到 v0.8', null, { append: COLD_CHAIN }),
    ]],
    [11, 'f07c2d96', dayAt(1, 11, 5), 'plugin', '能力升级：零售行业包更新到 1.0.3', [], { packs: 'industry-retail@1.0.3', packs_from: '1.0.2' }],
    [12, 'a71e03c9', todayAt(15, 8), 'assistant', '助手：分段「补货申请与门店需求」，3 个验收场景通过', [
      ch(R('intake_checks'), 'mod', '修改 · 2 处'), ch(R('replenish_order'), 'mod', '修改 · 1 处'),
      ch(SCN('s-01', '场景 1'), 'add', '新增'), ch(SCN('s-02', '场景 2'), 'add', '新增'),
    ], { changeset_id: 'cs-29' }],
    [13, 'c0ffee13', todayAt(15, 18), 'autobuild', '运行 #3：安全库存测算 补齐 1 条规则（中途保存）', [
      ch(R('r05_by_category'), 'add', '新增', 'accepted', { add: true }),
    ], { run: run('s2') }],
    [14, '4be9d27a', todayAt(15, 24), 'autobuild', '运行 #3：安全库存测算 自主完成，2 项需复核', [
      ch(R('safety_stock_std'), 'mod', '修改 · 2 处', 'needs_review', {
        lines: [['按品类确定的安全库存下限，门店申请严于标准时按门店执行。', '按品类确定的安全库存下限。'], ['| 安全库存 | safety_qty | 数值 · 箱 |', '| 安全库存 | safety_qty | 数值 |']],
      }),
      ch(R('safety_stock_calc'), 'mod', '修改 · 1 处', 'needs_review'),
      ch(R('r09_store_stricter'), 'add', '新增', 'accepted', { add: true }),
    ], { run: run('s2') }],
    [15, '8d3f6a10', todayAt(15, 36), 'autobuild', '运行 #3：陈列量核定 完成，先刷新 09 修订通知带来的 3 项过期', [
      ch(R('display_qty_std'), 'mod', '修改 · 1 处', 'needs_review', { row: '生效日期' }),
      ch(R('display_qty_check'), 'mod', '修改 · 1 处', 'needs_review'),
      ch(R('r12_region_quota'), 'add', '新增', 'accepted', { add: true }),
      ch(SCN('s-03', '场景 3'), 'mod', '刷新'),
    ], { run: run('s3'), tags: [{ name: 'mark-1761983760-b1e2', label: '自主构建 #3 · 陈列量核定' }] }],
    [16, '1c5a9e47', todayAt(15, 46), 'autobuild', '运行 #3：临期与损耗检查 生成 2 个验收场景（中途保存）', [
      ch(R('expiry_loss_std'), 'add', '新增', 'accepted', { add: true }), ch(R('check_item'), 'add', '新增', 'accepted', { add: true }),
      ch(R('expiry_check'), 'add', '新增', 'needs_review', { add: true }),
      ch(SCN('s-07', '场景 7'), 'add', '新增'), ch(SCN('s-08', '场景 8'), 'add', '新增'),
    ], { run: run('s4') }],
    [17, '6f2b0c85', todayAt(15, 55), 'autobuild', '运行 #3：临期与损耗检查 部分完成，挂起 2 项', [
      ch(R('r08_expiry_grade'), 'add', '新增', 'needs_review', { add: true }),
      ch(R('expiry_check'), 'mod', '修改 · 1 处', 'needs_review'),
      ch(R('expiry_loss_std'), 'mod', '修改 · 1 处', 'accepted'),
    ], { run: run('s4') }],
    [18, '9e4d71b3', todayAt(16, 20), 'studio', 'Studio 编辑：陈列量标准 增加属性「到货周期」', [
      ch(R('display_qty_std'), 'mod', '修改 · 2 处', null, { row: '到货周期', lines: [DESC_DISPLAY], link: '到货周期' }),
    ]],
    [19, 'b2c8f5e0', todayAt(16, 38), 'autobuild', '运行 #3：超量与审批提示 自主完成，1 个场景未通过已挂起', [
      ch(R('overage_list'), 'mod', '修改 · 2 处', 'needs_review', { row: '申请值', lines: [['申请量超出标准的商品清单，注明标准值与申请值。', '申请量超出标准的商品清单。']] }),
      ch(R('approval_hint'), 'mod', '修改 · 1 处', 'accepted', { lines: [['超出标准时生成的审批提示，挂到补货台账，交区域审批。', '超出标准时生成的审批提示，交区域审批。']] }),
      ch(R('r11_temp_increase'), 'add', '新增', 'needs_review', { add: true }),
      ch(R('overage_and_hint'), 'mod', '修改 · 2 处', 'accepted'),
    ], { run: run('s5') }],
  ];
}

// running：自主构建运行 #3 还在跑（?mock=stage:l4），只有到 15:48 为止的版本（v0.16）
export function mainHistory({ running = false } = {}) {
  const list = rows().map(([n, commit, at, source, summary, changes, extra = {}]) => ({
    commit, n, version_label: `v0.${n}`, at, source, source_label: SOURCE_LABEL[source], author: 'zhangsan', summary, changes,
    tags: extra.tags || [], changeset_id: extra.changeset_id || (extra.run ? `cs-ab3-${extra.run.slice_id}` : null), run: extra.run || null,
    needs_review: changes.filter((c) => c.review === 'needs_review').length,
    trailer: {
      source: TRAILER_SOURCE[source] || source, author: 'zhangsan',
      ...(extra.changeset_id ? { changeset: extra.changeset_id } : {}), ...(extra.packs ? { packs: extra.packs } : {}),
      ...(extra.run ? { node: 'core.model' } : {}),
    },
    packs_from: extra.packs_from || null,
  }));
  return running ? list.filter((v) => v.n <= 16) : list;
}

// 稳定的 8 位十六进制（mock 的提交号）
export function hex8(s) {
  let h = 2166136261;
  for (const ch of String(s)) h = Math.imul(h ^ ch.codePointAt(0), 16777619) >>> 0;
  return h.toString(16).padStart(8, '0');
}

// 其他项目没有条目文件：按卡片的版本号生成几条通用历史，只用来演示列表、标记和回退
export function genericHistory(card) {
  const m = /^v(\d+)\.(\d+)$/.exec(card.version_label || 'v0.1');
  const major = m ? Number(m[1]) : 0;
  const total = Math.max(1, m ? Number(m[2]) : 1);
  const end = Date.parse(card.last_activity_at) || Date.now();
  const cycle = ['assistant', 'studio', 'assistant', 'web', 'assistant'];
  return Array.from({ length: total }, (_, i) => {
    const n = i + 1;
    let source = n === 1 ? (card.created_from === 'import' ? 'import' : 'plugin') : cycle[(n - 2) % cycle.length];
    if (n === total && card.build_mode === 'L4' && n > 1) source = 'autobuild';
    const summary = n === 1 ? `新建项目：${card.name}` : {
      assistant: `助手：第 ${n - 1} 轮修改`, studio: 'Studio 编辑：调整对象属性', web: '网页编辑：补充说明', autobuild: '自主构建：一段自主完成',
    }[source];
    return {
      commit: hex8(`${card.id}:${n}`),
      n, version_label: `v${major}.${n}`, at: new Date(end - (total - n) * 7 * HOUR).toISOString(),
      source, source_label: SOURCE_LABEL[source], author: card.owner || 'zhangsan', summary, changes: [], tags: [], changeset_id: null, run: null,
      needs_review: 0, trailer: { source: TRAILER_SOURCE[source] || source, author: card.owner || 'zhangsan' }, packs_from: null,
    };
  });
}

// 条目第一次出现的版本（之前不存在）；其余条目视为 v0.1 起就有
export function addedAt(list) {
  const out = new Map();
  list.forEach((v) => v.changes.forEach((c) => {
    if (c.undo?.add && c.target?.id && !out.has(c.target.id)) out.set(c.target.id, v.n);
  }));
  return out;
}

// 主示例项目里有文件的条目（V-03 文件列表）
export const FILE_ENTRIES = ENTRIES;

/**
 * 把一处改动「撤回去」：从这个版本之后的内容推回之前的内容。
 * undo：{add} 之前不存在；{row} 去掉属性表里这一行；{lines: [[新, 旧]]} 逐行换回；{link} 去掉关联对象里的链接；
 * {cut: 行[]} 去掉这几行；{append: 行[]} 补回这几行。没有 undo 的修改：去掉属性表最后一行，没有表时去掉最后一行。
 */
export function undoChange(content, change) {
  if (content == null) return content;
  const u = change.undo;
  if (u?.add || change.change === 'add') return null;
  let lines = content.split('\n');
  if (!u) {
    const rowsAt = lines.map((l, i) => (/^\|/.test(l) && !/^\|\s*-/.test(l) ? i : -1)).filter((i) => i >= 0);
    const drop = rowsAt.length > 1 ? rowsAt[rowsAt.length - 1] : lines.map((l, i) => (l.trim() ? i : -1)).filter((i) => i >= 0).pop();
    if (drop != null && drop > 0) lines.splice(drop, 1);
    return lines.join('\n');
  }
  if (u.row) lines = lines.filter((l) => !(/^\|/.test(l) && l.split('|')[1]?.trim() === u.row));
  (u.lines || []).forEach(([now, before]) => { lines = lines.map((l) => (l === now ? before : l)); });
  if (u.link) lines = lines.map((l) => l.replace(`、[[${u.link}]]`, '').replace(`[[${u.link}]]、`, ''));
  if (u.cut) {
    const i = lines.findIndex((l, k) => u.cut.every((c, j) => lines[k + j] === c));
    if (i >= 0) lines.splice(i, u.cut.length);
  }
  if (u.append) lines = [...lines, ...u.append];
  return lines.join('\n');
}
