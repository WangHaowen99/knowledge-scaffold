// 代码生成与求解页（设计稿 scripts）的纯逻辑：脚本表的状态显示、可代码化候选、页面命令（S-20）、沙箱输出，
// 求解契约表单（草稿与预检复用 src/lib/solver-contract-model.js）、检查卡、写入范围只读两态、保存失败的说法。
import {
  SIDECAR_TABS, draftFromView, isEnabled, localProblems, normalizeParams,
} from '../../lib/solver-contract-model.js';
import { relativeTime } from '../../api/errors.js';
import { isLocked, lockState } from '../../state/readonly.js';

export const SOLVER_TYPE = 'codegen.solver_contract';
export const SOLVER_DIR = 'ontology/solver/';

// 插件命令（命令名不带参数；参数放 args）
export const COMMANDS = {
  scan: '/ontology-codegen-scan',
  codegen: '/ontology-codegen',
  evaluator: '/ontology-solver-evaluator',
};

const pad = (n) => String(n).padStart(2, '0');
export function fmtStamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
// 沙箱耗时：1200 → 1.2s；10 秒以上取整
export function fmtSecs(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return '';
  const s = Number(ms) / 1000;
  return s >= 10 ? `${Math.round(s)}s` : `${s.toFixed(1)}s`;
}

// ---------- 脚本表 ----------

export const findingsText = (findings = []) => findings.map((f) => f.message).filter(Boolean).join('；');

// 三列状态：[文字, 色调, 图标]；契约没过时悬停看原因
export function contractChip(row) {
  if (row?.contract?.status === 'passed') return { text: '通过', tone: 'teal', icon: 'check' };
  return { text: '未通过', tone: 'red', icon: 'x', pop: findingsText(row?.contract?.findings) || null };
}
const DESCRIPTOR = {
  synced: { text: '已同步', tone: 'teal', icon: 'check' },
  stale: { text: '需更新', tone: 'amber', icon: 'refresh' },
  none: { text: '—', tone: 'gray', icon: null },
};
export const descriptorChip = (row) => DESCRIPTOR[row?.descriptor] || DESCRIPTOR.none;
export function sandboxChip(row) {
  const s = row?.sandbox || {};
  if (s.status === 'passed') return { text: `通过${s.duration_ms != null ? ` · ${fmtSecs(s.duration_ms)}` : ''}`, tone: 'teal', icon: 'check' };
  if (s.status === 'failed') return { text: `未通过${s.duration_ms != null ? ` · ${fmtSecs(s.duration_ms)}` : ''}`, tone: 'red', icon: 'x' };
  return { text: '未运行', tone: 'gray', icon: null };
}

// 针对一个脚本的命令：修复契约问题、重新生成描述符、整个重新生成
export function scriptCommand(mode, script) {
  const name = script?.name || script?.key || '';
  const text = {
    fix: `修复 ${name} 的契约问题`,
    descriptor: `重新生成 ${name} 的描述符`,
    regenerate: `重新生成 ${name}`,
  }[mode];
  return { command: COMMANDS.codegen, args: { script: script?.key, mode }, text };
}

// 行末按钮：契约没过先修复；描述符过期重新生成描述符；其余收进「⋯」
export function rowAction(row) {
  const acts = row?.actions || [];
  if (acts.includes('fix')) return { kind: 'fix', label: '修复', ...scriptCommand('fix', row.script) };
  if (acts.includes('regenerate_descriptor')) return { kind: 'descriptor', label: '重新生成', ...scriptCommand('descriptor', row.script) };
  return { kind: 'more' };
}

// 「⋯」菜单里的项
export function moreItems(row) {
  const items = [];
  if ((row?.actions || []).includes('regenerate')) items.push({ kind: 'regenerate', label: '重新生成脚本', icon: 'refresh', ...scriptCommand('regenerate', row.script) });
  items.push({ kind: 'studio', label: '去 Studio 编辑', icon: 'edit' });
  return items;
}

// 默认预览哪个脚本：先看要处理的（描述符过期、契约没过），没有就第一个
export function defaultScript(items = []) {
  const hit = items.find((r) => r.descriptor === 'stale' || r.contract?.status === 'failed') || items[0];
  return hit?.script?.key || null;
}

// ---------- 可代码化候选 ----------

export function candidateView(candidates) {
  const items = candidates?.items || [];
  const unsuitable = candidates?.unsuitable || [];
  return {
    scanned: Boolean(candidates?.scanned_at),
    items,
    title: `可代码化的操作 ${items.length} 个`,
    sub: candidates?.scanned_at ? `${fmtStamp(candidates.scanned_at)} 扫描` : '还没扫描',
    unsuitableText: unsuitable.length
      ? `不建议代码化：${unsuitable.map((u) => `${u.operation?.name || u.operation?.key}（${u.reason}）`).join('；')}`
      : '',
  };
}
export const defaultPicked = (candidates) => (candidates?.items || []).filter((c) => c.selected_default).map((c) => c.operation.key);
export function togglePicked(picked, key) {
  return picked.includes(key) ? picked.filter((k) => k !== key) : [...picked, key];
}
export function pickedOperations(candidates, picked) {
  return (candidates?.items || []).filter((c) => picked.includes(c.operation.key)).map((c) => c.operation);
}

export const scanCommand = () => ({ command: COMMANDS.scan, text: '扫描可代码化的操作' });
export function generateCommand(operations) {
  return { command: COMMANDS.codegen, args: { operations: operations.map((o) => o.key) }, text: `生成 ${operations.length} 个脚本并在沙箱里跑通` };
}

// ---------- 沙箱 ----------

export function sandboxView(s) {
  if (!s) return null;
  if (s.available) return { text: '脚本验证沙箱可用', tone: 'teal', icon: 'check', note: null };
  return { text: '脚本验证沙箱不可用', tone: 'red', icon: 'alert', note: '暂时不能验证和保存脚本，请联系管理员' };
}

// 沙箱验证输出卡：命令行、输出、结论
export function verificationView(v, scriptName) {
  const name = scriptName || v?.script?.name || '';
  if (!v || v.status === 'not_run') {
    return { status: 'not_run', chip: { text: '未运行', tone: 'gray', icon: null }, sub: null, lines: [], summary: null };
  }
  const cases = v.cases || [];
  const ok = cases.filter((c) => c.passed).length;
  const passed = v.status === 'passed';
  return {
    status: v.status,
    chip: passed ? { text: '通过', tone: 'teal', icon: 'check' } : { text: '未通过', tone: 'red', icon: 'x' },
    sub: fmtStamp(v.ran_at) || null,
    lines: cases.flatMap((c) => [{ kind: 'cmd', text: `$ ${name} ${c.args}`.trimEnd() }, { kind: c.passed ? 'out' : 'err', text: c.output }]),
    summary: {
      ok: passed,
      text: `${passed ? '✓' : '✗'} ${ok}/${cases.length} 用例通过${v.duration_ms != null ? ` · ${fmtSecs(v.duration_ms)}` : ''} · ${v.network_access ? '有网络访问' : '无网络访问'}`,
    },
  };
}

// ---------- 求解契约：SV-01 → 表单草稿 ----------

// SV-01 没带 binding（契约 7.12 只列了表单字段）时按字段拼一份
export function bindingFromView(op) {
  return {
    solver_key: op.solver_key,
    operation: op.operation?.name || op.operation?.key || '',
    decision_model: `${op.solver_key}_model`,
    declaration: { engine_requirements: { problem_family: op.problem_family || '', capabilities: [...(op.capabilities || [])] } },
    operation_frontmatter: op.enabled === false ? { execution: { enabled: false } } : {},
    units: (op.field_units || []).map((u) => ({ ...u })),
  };
}
// 模板参数：后端带 template_params（原样回传）；没有时按契约字段拼
export function paramsFromView(op) {
  if (op.template_params) return op.template_params;
  const [o0 = '', o1 = ''] = op.option_labels || [];
  const [process = '', stream = ''] = op.result_texts || [];
  return { display_mapping: { OPTION_0: o0, OPTION_1: o1 }, title: op.result_title || '', evaluator_path: '', process_name: process, stream_name: stream };
}
// solver-contract-model 按旧面板的形状读：evaluator 是路径，managed 是布尔
export function modelOperation(op) {
  return {
    ...op,
    binding: op.binding || bindingFromView(op),
    template_params: paramsFromView(op),
    evaluator: op.evaluator?.path || '',
    managed: op.mode === 'template',
  };
}
export const draftOf = (view) => draftFromView({ contract: { operations: (view?.operations || []).map(modelOperation) } });
export const baseOperations = (view) => (view?.operations || []).map(modelOperation);
export const operationNames = (view) => (view?.available_operations || []).map((o) => o.name);

// 卡片标题：绑定的操作名（找不到时用求解标识）
export function contractTitle(item, operations = []) {
  const op = String(item?.binding?.operation || '').trim();
  const hit = operations.find((o) => o.name === op || o.key === op);
  return hit?.name || op || String(item?.binding?.solver_key || '').trim() || '新的求解契约';
}
export const itemKey = (item) => String(item?.origin ?? item?.binding?.solver_key ?? '').trim();

// 问题族下拉：「id · 中文名」（后端暂时 label 就是 id）
export function familyOptions(families = [], current = '') {
  const opts = families.map((f) => ({ value: f.id, label: f.label && f.label !== f.id ? `${f.id} · ${f.label}` : f.id }));
  if (current && !families.some((f) => f.id === current)) opts.push({ value: current, label: `${current}（不在注册目录里）` });
  return opts;
}

// 评估器下拉：留空 = 自动查找
export function evaluatorOptions(view, item) {
  const found = item?.evaluator || '';
  const opts = [{ value: '', label: found ? `自动查找 → ${found}` : '自动查找' }];
  (view?.evaluators || []).forEach((e) => { if (e.path) opts.push({ value: e.path, label: e.path }); });
  const cur = item?.params?.evaluator_path || '';
  if (cur && !opts.some((o) => o.value === cur)) opts.push({ value: cur, label: `${cur}（不存在或不是评估器）` });
  return opts;
}

// 附属文件：五份按 role 排，模板模式只读
export const SIDECAR_FILES = SIDECAR_TABS.map((t) => ({ role: t.role, file: t.file }));

// ---------- 写入范围只读 ----------

export function contractLocked(lock, item) {
  const key = itemKey(item);
  if (!key) return false;
  return isLocked(lock, { type: SOLVER_TYPE, key }) || isLocked(lock, { path: `${SOLVER_DIR}${key}/binding.yaml` });
}

// 锁定条的文字（设计稿 scripts mount 里的三态切换）：只有标题，旁边是「回到对话」「去确认」「查看运行」
export function lockText(lock, title) {
  if (lockState(lock) === 'autobuild_slice') return { running: [`自主构建正在改「${title}」，暂时只读`] };
  return {
    running: [`助手正在为「${title}」生成评估器，暂时只读`],
    pending: ['评估器已生成，待确认'],
  };
}

// ---------- 检查卡 ----------

export function revisionTag(view, dirty) {
  const n = view?.revision_no ?? 0;
  if (dirty) return `r${n + 1} 草稿`;
  return n ? `r${n}` : '';
}

const EVALUATOR_KIND = { declarative: '声明式', script: '脚本' };

/**
 * 检查卡的几行：[{ icon, tone, text }]，tone 为 ok / warn / err / wait
 * live：SV-03 的结果 { key, rejected, findings, evaluator }（key 不是当前草稿时算「检查中」）
 */
export function checkLines({ view, item, dirty, live, draftKey, problems = [] }) {
  const lines = [];
  const n = view?.revision_no ?? 0;
  const fresh = live && live.key === draftKey ? live : null;
  // 契约问题：本地预检 → 即时检查（草稿）/ 已保存修订的检查
  const local = problems.filter((p) => p.severity !== 'warning');
  if (local.length) local.forEach((p) => lines.push({ icon: 'x', tone: 'err', text: p.message }));
  else if (dirty && !fresh) lines.push({ icon: 'spin', tone: 'wait', text: '正在检查草稿…' });
  else if (dirty && fresh.failed) lines.push({ icon: 'info', tone: 'warn', text: '暂时检查不了草稿' });
  else {
    const rejected = dirty ? fresh.rejected || [] : [];
    const findings = dirty ? fresh.findings || [] : view?.findings || [];
    rejected.forEach((f) => lines.push({ icon: 'x', tone: 'err', text: f.message }));
    findings.forEach((f) => lines.push({ icon: 'alert', tone: f.level === 'error' ? 'err' : 'warn', text: f.message }));
    if (!rejected.length && !findings.length) lines.push({ icon: 'check', tone: 'ok', text: dirty ? '草稿未发现契约问题' : `r${n} 未发现契约问题` });
  }
  // 评估器：即时检查解析出的约束数只在这份已有评估器时才说
  const ev = item?.evaluator ? (fresh || live)?.evaluator : null;
  if (ev) lines.push({ icon: 'check', tone: 'ok', text: `评估器已解析：${EVALUATOR_KIND[ev.kind] || ev.kind}${ev.constraints != null ? ` · ${ev.constraints} 条约束` : ''}` });
  else if (item?.evaluator) lines.push({ icon: 'check', tone: 'ok', text: `评估器：${String(item.evaluator).split('/').pop()}` });
  else if (item && isEnabled(item.binding)) lines.push({ icon: 'info', tone: 'warn', text: '还没有评估器' });
  return lines;
}

// 本地预检：整份契约一起保存，所有求解契约的问题都算；不是当前这份的前面加名字
export function contractProblems(items, view, current) {
  const ops = view?.available_operations || [];
  return localProblems(items, operationNames(view), view?.problem_families || []).map((p) => {
    if (!current || p.uid === current.uid) return p;
    const it = items.find((x) => x.uid === p.uid);
    return { ...p, message: `「${contractTitle(it, ops)}」：${p.message}` };
  });
}

// 表单字段旁边的提示：当前这份的第一条问题
export function fieldProblem(problems, uid, field) {
  return problems.find((p) => p.uid === uid && p.field === field) || null;
}

// 保存按钮：不可用时给原因
export function saveState({ dirty, locked, problems = [], live, draftKey, saving }) {
  if (saving) return { disabled: true, title: '正在保存…' };
  if (!dirty) return { disabled: true, title: '没有要保存的修改' };
  if (locked) return { disabled: true, title: '这份求解契约在助手写入范围里，这一轮结束后才能保存' };
  const errors = problems.filter((p) => p.severity !== 'warning');
  if (errors.length) return { disabled: true, title: `还有 ${errors.length} 个问题要先改` };
  if (live && live.key === draftKey && live.rejected?.length) return { disabled: true, title: `保存会被拒绝：${live.rejected[0].message}` };
  return { disabled: false, title: '' };
}

// 保存后发「生成评估器」：这次改到的、启用中的求解契约，优先当前这份
export function evaluatorTarget(diff, items, current) {
  const touched = new Set([...diff.added.map((x) => x.uid), ...diff.changed.map((x) => x.uid)]);
  const enabled = items.filter((it) => touched.has(it.uid) && isEnabled(it.binding));
  return enabled.find((it) => it.uid === current?.uid) || enabled[0] || null;
}
export function evaluatorCommand(solverKey, title) {
  return { command: COMMANDS.evaluator, args: { solver_key: solverKey }, text: `为「${title}」生成评估器` };
}
export function templateCommand(solverKey, title) {
  return { command: COMMANDS.evaluator, args: { solver_key: solverKey, mode: 'template' }, text: `按模板重新生成「${title}」的附属文件和评估器` };
}

const WHO = { assistant: '助手', web: '网页', studio: 'Studio', mcp: 'MCP 客户端', autobuild: '自主构建', import: '导入', plugin: '插件', revert: '回退' };

// 保存失败：冲突、被拒、被锁给一句话（不弹冲突框）
export function saveErrorView(e, now = Date.now()) {
  const code = e?.code || '';
  const d = e?.details || {};
  if (code === 'revision_conflict') {
    const who = WHO[d.changed_by] || '别处';
    const when = [relativeTime(d.changed_at, now), d.version_label ? `版本 ${d.version_label}` : ''].filter(Boolean).join('，');
    return { kind: 'conflict', text: `求解契约已被${who}修改${when ? `（${when}）` : ''}，这次没有保存。刷新看最新内容后再改。` };
  }
  if (code === 'solver_contract_rejected' || code === 'solver_contract_invalid') {
    return { kind: 'rejected', text: '检查没通过，没有保存：', findings: d.findings || [] };
  }
  if (code === 'write_scope_locked') return { kind: 'locked', text: '这份求解契约刚进了助手的写入范围，这一轮结束后才能保存。' };
  return { kind: 'error', text: e?.message || '保存失败' };
}

// 保存成功的提示
export const savedToast = (res) => `已保存 r${res?.revision_no ?? ''}`;

// 字段单位表：改一格；最后一行留空可以继续加
export function setUnit(units, index, key, value) {
  const list = (Array.isArray(units) ? units : []).map((u) => ({ ...u }));
  while (list.length <= index) list.push({ field: '', unit: '', note: '' });
  list[index] = { ...list[index], [key]: value };
  return list;
}
export function removeUnit(units, index) {
  return (Array.isArray(units) ? units : []).filter((_, i) => i !== index);
}

// 引擎能力：点 chip 勾选或取消（只能选问题族目录列出的）
export function toggleCapability(current = [], cap) {
  return current.includes(cap) ? current.filter((c) => c !== cap) : [...current, cap];
}

// 模板参数（只读预览五份附属文件用）
export const templateParams = (item) => normalizeParams(item?.params);
